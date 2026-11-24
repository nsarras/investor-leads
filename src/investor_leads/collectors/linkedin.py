"""LinkedIn collector that runs a keyword post-search Actor on Apify.

LinkedIn has no public API for searching posts, so this goes through a third-party
provider. The Actor, its input and the mapping of its output fields all come from
config, so a different Actor can be swapped in without code changes.

Apify API docs: https://docs.apify.com/api/v2
"""

from __future__ import annotations

import time
from datetime import date, datetime
from typing import Any, Callable

import httpx

from ..config import LinkedInSettings
from ..models import Post
from . import CollectorAuthError, CollectorError

API = "https://api.apify.com/v2"
TERMINAL = {"SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"}
POLL_WAIT_SECONDS = 30
# With less than this left of the budget, another Actor run is not worth starting.
MIN_RUN_BUDGET_USD = 0.05


def dig(item: Any, path: str) -> Any:
    """Read a dotted path such as "author.name" from nested dicts; None when absent."""
    current = item
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def fill_input(template: Any, values: dict[str, Any]) -> Any:
    """Replace "$name" placeholders in an Actor input template."""
    if isinstance(template, dict):
        return {k: fill_input(v, values) for k, v in template.items()}
    if isinstance(template, list):
        return [fill_input(v, values) for v in template]
    if isinstance(template, str) and template.startswith("$") and template[1:] in values:
        return values[template[1:]]
    return template


class LinkedInCollector:
    def __init__(
        self,
        api_token: str,
        settings: LinkedInSettings,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] = print,
    ) -> None:
        self._client = client or httpx.Client(timeout=POLL_WAIT_SECONDS + 30)
        self._headers = {"Authorization": f"Bearer {api_token}"}
        self._settings = settings
        self._sleep = sleep
        self._log = log
        # For the cost summary: runs made, items returned, and the cost Apify reports.
        self.runs = 0
        self.items = 0
        self.cost_usd = 0.0
        self.cost_known = True
        # How far down the keyword list the budget (linkedin.max_cost_usd) let us go.
        self.keywords_total = 0
        self.keywords_searched = 0
        self._spent = 0.0  # counted against the budget; a run of unknown cost counts as its limit

    def search(self, keywords: list[str], since: date, max_posts_per_keyword: int) -> list[Post]:
        """Return posts dated on or after `since` for all keywords.

        Actors limit how many keywords one run accepts, so long lists are split across
        several runs. If a later run fails, the posts already collected are kept.

        Keywords are searched in the order given. With `linkedin.max_cost_usd` set, searching
        stops once that much has been spent, so the keywords at the end of the list are the
        ones left out: put the most useful first.
        """
        size = max(1, self._settings.max_keywords_per_run)
        chunks = [keywords[i : i + size] for i in range(0, len(keywords), size)]
        budget = self._settings.max_cost_usd
        self.keywords_total += len(keywords)
        posts: list[Post] = []
        for number, chunk in enumerate(chunks, start=1):
            remaining = None if budget is None else budget - self._spent
            if remaining is not None and remaining < MIN_RUN_BUDGET_USD:
                left = sum(len(c) for c in chunks[number - 1 :])
                self._log(f"  Apify budget of ${budget:.2f} reached: {left} of {len(keywords)} keywords were not searched")
                break
            if len(chunks) > 1:
                self._log(f"  run {number} of {len(chunks)}: {len(chunk)} keywords")
            try:
                posts.extend(self._search_once(chunk, since, max_posts_per_keyword, remaining))
            except CollectorError as exc:
                if not posts:
                    raise
                self._log(f"  ! stopped after {number - 1} of {len(chunks)} runs: {exc}")
                break
            self.keywords_searched += len(chunk)
        return posts

    def _search_once(
        self, keywords: list[str], since: date, max_posts_per_keyword: int, remaining: float | None = None
    ) -> list[Post]:
        if not keywords:
            return []
        actor_input = fill_input(
            self._settings.input,
            {"keywords": keywords, "max_posts": max_posts_per_keyword, "since_date": since.isoformat()},
        )
        limit = self._run_limit(remaining)
        run = self._start(actor_input, limit)
        run = self._wait(run)
        dataset = str(run.get("defaultDatasetId") or "")
        if run.get("status") == "SUCCEEDED":
            items = self._items(dataset)
        else:
            # A run stopped early (by its spending limit, say) has still been paid for, so
            # keep whatever it returned.
            items = self._items_if_any(dataset)
            if not items:
                raise CollectorError(
                    f"Apify run {run.get('id')} ended with status {run.get('status')}. "
                    "Open the run in the Apify Console to see its log."
                )
            self._log(
                f"  ! Apify run {run.get('id')} ended with status {run.get('status')}; "
                f"keeping the {len(items)} items it returned"
            )
        self._record_usage(run, len(items), limit)
        return self._to_posts(items, since)

    def _run_limit(self, remaining: float | None) -> float | None:
        """Spending limit for one Actor run: the per-run cap or what is left of the budget, whichever is lower."""
        limits = [value for value in (self._settings.max_charge_usd, remaining) if value is not None]
        return round(min(limits), 4) if limits else None

    def _items_if_any(self, dataset_id: str) -> list[dict[str, Any]]:
        if not dataset_id:
            return []
        try:
            return self._items(dataset_id)
        except CollectorError:
            return []

    def _record_usage(self, run: dict[str, Any], items: int, limit: float | None = None) -> None:
        """Read the run's cost from Apify. The figure is final once the run has finished."""
        self.runs += 1
        self.items += items
        cost = run.get("usageTotalUsd")
        try:
            body = self._request("GET", f"{API}/actor-runs/{run.get('id')}")
            if isinstance(body, dict):
                cost = (body.get("data") or {}).get("usageTotalUsd", cost)
        except CollectorError:
            pass
        if isinstance(cost, (int, float)):
            self.cost_usd += float(cost)
            self._spent += float(cost)
        else:
            self.cost_known = False
            self._spent += limit or 0.0

    def _request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any] | list[Any]:
        try:
            response = self._client.request(method, url, headers=self._headers, **kwargs)
        except httpx.HTTPError as exc:
            raise CollectorError(f"Apify request failed: {exc}") from exc
        if response.status_code in (401, 403):
            raise CollectorAuthError(
                f"Apify rejected the request ({response.status_code}). Check APIFY_API_TOKEN "
                f"and that your account can run {self._settings.actor_id}."
            )
        if response.status_code == 402:
            raise CollectorAuthError("Apify reports no remaining credit for this billing cycle.")
        if response.status_code == 404:
            raise CollectorError(f"Apify could not find {self._settings.actor_id}. Check linkedin.actor_id.")
        if response.status_code >= 400:
            raise CollectorError(f"Apify returned {response.status_code}: {response.text[:300]}")
        return response.json()

    def _start(self, actor_input: dict[str, Any], limit: float | None = None) -> dict[str, Any]:
        actor = self._settings.actor_id.replace("/", "~")
        params: dict[str, Any] = {}
        if limit is not None:
            params["maxTotalChargeUsd"] = limit
        body = self._request("POST", f"{API}/acts/{actor}/runs", params=params, json=actor_input)
        return dict(body.get("data") or {}) if isinstance(body, dict) else {}

    def _wait(self, run: dict[str, Any]) -> dict[str, Any]:
        run_id = run.get("id")
        if not run_id:
            raise CollectorError("Apify did not return a run id.")
        deadline = time.monotonic() + self._settings.run_timeout_seconds
        while run.get("status") not in TERMINAL:
            if time.monotonic() > deadline:
                raise CollectorError(
                    f"Apify run {run_id} was still running after "
                    f"{self._settings.run_timeout_seconds}s. Raise linkedin.run_timeout_seconds or lower the caps."
                )
            body = self._request("GET", f"{API}/actor-runs/{run_id}", params={"waitForFinish": POLL_WAIT_SECONDS})
            run = dict(body.get("data") or {}) if isinstance(body, dict) else {}
            if run.get("status") not in TERMINAL:
                self._sleep(1)
        return run

    def _items(self, dataset_id: str) -> list[dict[str, Any]]:
        body = self._request("GET", f"{API}/datasets/{dataset_id}/items", params={"format": "json", "clean": "true"})
        return [item for item in body if isinstance(item, dict)] if isinstance(body, list) else []

    def _to_posts(self, items: list[dict[str, Any]], since: date) -> list[Post]:
        fmap = self._settings.field_map
        posts: list[Post] = []
        for item in items:
            if item.get("type") == "error":
                self._log(f"  ! LinkedIn provider reported: {str(item.get('message') or item)[:200]}")
                continue
            if any(key in item and item[key] != value for key, value in self._settings.item_filter.items()):
                continue

            def get(name: str) -> Any:
                path = fmap.get(name)
                return dig(item, path) if path else None

            # Drop tracking parameters so the same post always has the same URL.
            url = str(get("url") or "").strip().split("?")[0].split("#")[0]
            text = str(get("text") or "").strip()
            if not url or not text:
                continue
            posted_at = str(get("posted_at") or "")
            if posted_at and _before(posted_at, since):
                continue
            engagement = get("engagement")
            posts.append(
                Post(
                    platform="linkedin",
                    post_id=str(get("post_id") or url),
                    url=url,
                    text=text,
                    posted_at=posted_at,
                    author_name=str(get("author_name") or ""),
                    author_handle=str(get("author_handle") or ""),
                    author_headline=str(get("author_headline") or ""),
                    author_url=str(get("author_url") or ""),
                    engagement=engagement if isinstance(engagement, int) else None,
                    matched_query=str(get("matched_query") or ""),
                )
            )
        return posts


def _before(posted_at: str, since: date) -> bool:
    """True when an ISO date or datetime string is earlier than `since`; False if unparseable."""
    try:
        return datetime.fromisoformat(posted_at.replace("Z", "+00:00")).date() < since
    except ValueError:
        return False
