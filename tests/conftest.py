"""Shared test doubles. No test calls a real API."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from investor_leads.models import Post


class FakeLLM:
    """Stands in for AnthropicJSON. Returns a canned plan and judges posts by simple keyword rules."""

    def __init__(self, skip_ids: set[str] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.skip_ids = skip_ids or set()
        # Same counters the real client keeps for the cost summary.
        self.call_count = self.input_tokens = self.output_tokens = 0

    def json(self, *, system: str, user: str, schema: dict[str, Any], max_tokens: int = 8000) -> dict[str, Any]:
        self.calls.append({"system": system, "user": user, "schema": schema})
        self.call_count += 1
        self.input_tokens += 100_000
        self.output_tokens += 20_000
        if "x_queries" in schema["properties"]:
            return {
                "x_queries": ['(raised OR "seed round") (gaming OR games)', "(investor OR VC) (gaming OR games)"],
                "linkedin_keywords": ["investing in gaming startups", "gaming startup raises seed"],
            }
        posts = json.loads(user)["posts"]
        results = []
        for post in posts:
            if post["id"] in self.skip_ids and len(posts) > 1:
                continue  # simulate the model dropping a post from a batch
            bio, text = post["author_bio"].lower(), post["text"]
            investor = "partner" in bio or "investor" in bio
            seeking = "pitch" in text.lower()
            led_by = re.search(r"led by ([A-Z][\w ]+?)\.", text)
            results.append(
                {
                    "id": post["id"],
                    "is_investor": investor,
                    "author_type": "vc" if investor else "media" if led_by else "founder",
                    "firm": "Pixel Ventures" if investor else "",
                    "role": "Partner" if investor else "",
                    "intent": "seeking_deals" if investor and seeking else "not_investing_signal",
                    "intent_score": 95 if investor and seeking else 10,
                    "sectors": ["gaming"] if investor else [],
                    "stages": ["seed"] if investor else ["unspecified"],
                    "industry_match": "yes" if investor else "unknown",
                    "stage_match": "yes" if investor else "unknown",
                    "reason": "Asks founders to send pitches." if seeking else "No investing signal.",
                    "named_investors": [
                        {
                            "name": led_by.group(1),
                            "firm": "",
                            "kind": "vc_firm",
                            "action": "invested",
                            "evidence": f"Led the round described in the post: {text[:60]}",
                            "industry_match": "yes",
                            "stage_match": "yes",
                        }
                    ]
                    if led_by
                    else [],
                }
            )
        return {"results": results}


def make_post(n: int, *, investor: bool = True, seeking: bool = True, platform: str = "x") -> Post:
    return Post(
        platform=platform,
        post_id=str(n),
        url=f"https://example.com/{platform}/{n}",
        text=("Send me your pitch, we are funding gaming studios." if seeking else "Great panel today.") + f" #{n}",
        posted_at="2026-11-12T10:00:00.000Z",
        author_name=f"Person {n}",
        author_handle=f"person{n}",
        author_headline="Partner at Pixel Ventures" if investor else "Founder building a game",
        author_url=f"https://example.com/u/{n}",
        author_followers=1000 + n,
        engagement=n,
        matched_query="gaming seed fund",
    )


def make_news(n: int, investor: str = "Merak Capital", likes: int = 0) -> Post:
    """A funding announcement written by a news account, naming the lead investor."""
    return Post(
        platform="linkedin",
        post_id=f"news{n}",
        url=f"https://example.com/linkedin/news{n}",
        text=f"Lambda Studios raised a $1.3M seed round led by {investor}. Outlet {n} reports.",
        posted_at=f"2026-11-{10 + n:02d}",
        author_name=f"Startup News {n}",
        author_handle=f"news{n}",
        author_headline="12,000 followers",
        engagement=likes,
        matched_query="gaming startup raises seed",
    )


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()
