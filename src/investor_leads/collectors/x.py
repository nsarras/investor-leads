"""X (Twitter) collector using the official API's recent search endpoint.

Docs: https://docs.x.com/x-api/posts/search-recent-posts
Recent search covers the last 7 days. Reads are billed per post returned.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import httpx

from ..config import X_MAX_DAYS, X_PRICE_PER_POST, X_PRICE_PER_USER
from ..models import Post
from . import CollectorAuthError, CollectorError

SEARCH_URL = "https://api.x.com/2/tweets/search/recent"
POST_FIELDS = "created_at,public_metrics,lang,author_id"
USER_FIELDS = "name,username,description,public_metrics,verified"
PAGE_MIN, PAGE_MAX = 10, 100
MAX_RATE_LIMIT_WAIT_SECONDS = 90


def estimate_cost(queries: int, max_posts_per_query: int, include_author_profile: bool) -> float:
    """Worst-case USD cost of one run: every query returns the maximum number of posts."""
    posts = queries * _billable_posts(max_posts_per_query)
    per_post = X_PRICE_PER_POST + (X_PRICE_PER_USER if include_author_profile else 0.0)
    return round(posts * per_post, 2)


def _billable_posts(max_posts: int) -> int:
    """Posts the API can return for a cap: the smallest page is 10, so a cap of 5 can cost 10."""
    return max(PAGE_MIN, max_posts)


def clamp_start(since: datetime, now: datetime | None = None) -> datetime:
    """Keep the start time inside X's 7-day window (with a small safety margin)."""
    now = now or datetime.now(timezone.utc)
    earliest = now - timedelta(days=X_MAX_DAYS) + timedelta(minutes=2)
    return max(since, earliest)


class XCollector:
    def __init__(
        self,
        bearer_token: str,
        *,
        lang: str = "en",
        include_author_profile: bool = True,
        exclude_terms: list[str] | None = None,
        exclude_replies: bool = True,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client or httpx.Client(timeout=30)
        self._headers = {"Authorization": f"Bearer {bearer_token}"}
        self._lang = lang
        self._include_author = include_author_profile
        self._exclude_terms = [t.strip() for t in (exclude_terms or []) if t.strip()]
        self._exclude_replies = exclude_replies
        # What this run has read, for the cost summary. X bills each post and user once per day.
        self.post_ids: set[str] = set()
        self.user_ids: set[str] = set()
        self._sleep = sleep
        # X's reference spec names this parameter `post.fields`; its guides use `tweet.fields`.
        # Start with the long-standing name and switch if the API rejects it.
        self._fields_param = "tweet.fields"

    def build_query(self, keywords: str) -> str:
        parts = [f"({keywords})", "-is:retweet"]
        if self._exclude_replies:
            parts.append("-is:reply")
        if self._lang:
            parts.append(f"lang:{self._lang}")
        for term in self._exclude_terms:
            parts.append(f'-"{term}"' if " " in term else f"-{term}")
        return " ".join(parts)

    def search(self, keywords: str, since: datetime, max_posts: int) -> list[Post]:
        """Return up to roughly `max_posts` posts matching `keywords` since `since`, newest first."""
        query = self.build_query(keywords)
        start_time = clamp_start(since).strftime("%Y-%m-%dT%H:%M:%SZ")
        posts: list[Post] = []
        next_token: str | None = None

        while len(posts) < max_posts:
            remaining = max_posts - len(posts)
            params: dict[str, Any] = {
                "query": query,
                "start_time": start_time,
                "max_results": max(PAGE_MIN, min(PAGE_MAX, remaining)),
                self._fields_param: POST_FIELDS,
            }
            if self._include_author:
                params["expansions"] = "author_id"
                params["user.fields"] = USER_FIELDS
            if next_token:
                params["next_token"] = next_token

            body = self._get(params)
            users = {u.get("id"): u for u in (body.get("includes") or {}).get("users", [])}
            self.user_ids.update(str(user_id) for user_id in users if user_id)
            for item in body.get("data") or []:
                if item.get("id"):
                    self.post_ids.add(str(item["id"]))
                post = self._to_post(item, users, keywords)
                if post:
                    posts.append(post)

            next_token = (body.get("meta") or {}).get("next_token")
            if not next_token:
                break
        return posts

    def _get(self, params: dict[str, Any]) -> dict[str, Any]:
        first_bad_request: str | None = None
        for attempt in range(4):
            try:
                response = self._client.get(SEARCH_URL, params=params, headers=self._headers)
            except httpx.HTTPError as exc:
                raise CollectorError(f"X request failed: {exc}") from exc

            if response.status_code == 200:
                if "post.fields" in params:
                    self._fields_param = "post.fields"  # remember what worked for later pages and queries
                return response.json()

            detail = _error_detail(response)
            if response.status_code == 400:
                if first_bad_request is None and "tweet.fields" in params:
                    # Possibly the parameter name; retry once with the newer spelling.
                    first_bad_request = detail
                    params["post.fields"] = params.pop("tweet.fields")
                    continue
                raise CollectorError(f"X rejected the query (400): {first_bad_request or detail}")
            if response.status_code in (401, 403):
                raise CollectorAuthError(
                    f"X rejected the request ({response.status_code}): {detail}. "
                    "Check X_BEARER_TOKEN and that the app has credits in console.x.com."
                )
            if response.status_code == 429 and attempt < 3:
                wait = _rate_limit_wait(response)
                if wait is not None and wait <= MAX_RATE_LIMIT_WAIT_SECONDS:
                    self._sleep(wait)
                    continue
                raise CollectorError("X rate limit reached. Try again in about 15 minutes.")
            raise CollectorError(f"X returned {response.status_code}: {detail}")
        raise CollectorError("X request did not succeed after retries.")

    @staticmethod
    def _to_post(item: dict[str, Any], users: dict[Any, dict[str, Any]], keywords: str) -> Post | None:
        post_id = str(item.get("id") or "")
        text = str(item.get("text") or "").strip()
        if not post_id or not text:
            return None
        user = users.get(item.get("author_id")) or {}
        handle = str(user.get("username") or "")
        # /i/status/<id> resolves without the handle, for runs that skip author profiles.
        url = f"https://x.com/{handle}/status/{post_id}" if handle else f"https://x.com/i/status/{post_id}"
        return Post(
            platform="x",
            post_id=post_id,
            url=url,
            text=text,
            posted_at=str(item.get("created_at") or ""),
            author_name=str(user.get("name") or ""),
            author_handle=handle,
            author_headline=str(user.get("description") or ""),
            author_url=f"https://x.com/{handle}" if handle else "",
            author_followers=(user.get("public_metrics") or {}).get("followers_count"),
            engagement=(item.get("public_metrics") or {}).get("like_count"),
            matched_query=keywords,
        )


def _error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:300]
    if isinstance(body, dict):
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0] if isinstance(errors[0], dict) else {}
            return str(first.get("message") or first.get("detail") or errors[0])[:300]
        return str(body.get("detail") or body.get("title") or body.get("message") or body)[:300]
    return str(body)[:300]


def _rate_limit_wait(response: httpx.Response) -> float | None:
    reset = response.headers.get("x-rate-limit-reset")
    if not reset or not reset.isdigit():
        return None
    return max(1.0, int(reset) - time.time() + 1)
