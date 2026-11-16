from datetime import date

import httpx
import pytest

from investor_leads.collectors import CollectorAuthError, CollectorError
from investor_leads.collectors.linkedin import LinkedInCollector, dig, fill_input
from investor_leads.config import LinkedInSettings

ITEMS = [
    {
        "type": "post",
        "content": "We just closed a new seed fund for gaming founders. Send pitches!",
        "post_url": "https://www.linkedin.com/posts/ana_1",
        "posted_at": "2026-11-12",
        "engagement": {"total_reactions": 88},
        "author": {"name": "Ana Ruiz", "username": "ana", "headline": "GP at Pixel Ventures",
                   "linkedinUrl": "https://www.linkedin.com/in/ana"},
        "_metadata": {"post_id": "1", "search_term": "gaming seed fund"},
    },
    {"type": "post", "content": "Old post", "post_url": "https://www.linkedin.com/posts/old", "posted_at": "2026-10-01"},
    {"type": "author_profile", "author_slug": "ana"},
    {"type": "error", "message": "Free tier limit reached"},
    {"type": "post", "content": "", "post_url": "https://www.linkedin.com/posts/empty"},
]


def make(handler, settings=None, logs=None):
    return LinkedInCollector(
        "token",
        settings or LinkedInSettings(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda s: None,
        log=(logs.append if logs is not None else lambda m: None),
    )


def test_runs_actor_polls_and_maps_items():
    seen = {}
    polls = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/v2/acts/atomus~linkedin-post-search/runs":
            import json

            seen["input"] = json.loads(request.content)
            seen["params"] = dict(request.url.params)
            seen["auth"] = request.headers["authorization"]
            return httpx.Response(201, json={"data": {"id": "run1", "status": "RUNNING", "defaultDatasetId": "ds1"}})
        if path == "/v2/actor-runs/run1":
            polls.append(1)
            status = "SUCCEEDED" if len(polls) > 1 else "RUNNING"
            return httpx.Response(200, json={"data": {"id": "run1", "status": status, "defaultDatasetId": "ds1",
                                                      "usageTotalUsd": 0.0416}})
        if path == "/v2/datasets/ds1/items":
            return httpx.Response(200, json=ITEMS)
        return httpx.Response(500)

    logs = []
    collector = make(handler, logs=logs)
    posts = collector.search(["gaming seed fund", "investing in games"], date(2026, 11, 7), 50)

    assert seen["input"] == {"search": ["gaming seed fund", "investing in games"], "maxPosts": 50, "postedAfter": "2026-11-07"}
    assert seen["params"] == {"maxTotalChargeUsd": "3.0"}
    assert seen["auth"] == "Bearer token"
    assert len(polls) == 3  # two while waiting, one more to read the final cost
    assert (collector.runs, collector.items, collector.cost_usd, collector.cost_known) == (1, 5, 0.0416, True)
    assert len(posts) == 1  # old, empty, profile and error rows are dropped
    post = posts[0]
    assert post.platform == "linkedin" and post.post_id == "1"
    assert post.author_name == "Ana Ruiz" and post.author_headline == "GP at Pixel Ventures"
    assert post.engagement == 88 and post.matched_query == "gaming seed fund"
    assert any("Free tier limit reached" in line for line in logs)


def test_long_keyword_lists_are_split_across_runs():
    import json

    inputs, logs = [], []

    def handler(request):
        if request.method == "POST":
            inputs.append(json.loads(request.content)["search"])
            run = len(inputs)
            if run == 3:
                return httpx.Response(402, json={})  # credit ran out on the third run
            return httpx.Response(201, json={"data": {"id": f"r{run}", "status": "SUCCEEDED", "defaultDatasetId": f"d{run}"}})
        run = request.url.path.split("/")[3][1:]
        return httpx.Response(200, json=[{"type": "post", "content": f"post {run}", "post_url": f"https://l/{run}",
                                          "posted_at": "2026-11-12"}])

    settings = LinkedInSettings(max_keywords_per_run=2)
    collector = make(handler, settings, logs)
    posts = collector.search(["a", "b", "c", "d", "e"], date(2026, 11, 7), 10)

    assert inputs == [["a", "b"], ["c", "d"], ["e"]]
    assert [p.url for p in posts] == ["https://l/1", "https://l/2"]  # earlier runs are kept
    assert collector.runs == 2 and not collector.cost_known  # this fake Apify reports no cost
    assert any("run 2 of 3" in line for line in logs) and any("stopped after 2 of 3 runs" in line for line in logs)


def test_failed_run_raises():
    def handler(request):
        return httpx.Response(201, json={"data": {"id": "r", "status": "FAILED", "defaultDatasetId": "d"}})

    with pytest.raises(CollectorError, match="FAILED"):
        make(handler).search(["k"], date(2026, 11, 7), 10)


def test_bad_token_raises_auth_error():
    with pytest.raises(CollectorAuthError, match="APIFY_API_TOKEN"):
        make(lambda r: httpx.Response(401, json={})).search(["k"], date(2026, 11, 7), 10)


def test_custom_actor_mapping():
    settings = LinkedInSettings(
        actor_id="someone/other-actor",
        max_charge_usd=None,
        input={"query": "$keywords", "limit": "$max_posts", "nested": {"from": "$since_date"}},
        field_map={"url": "link", "text": "body.text", "author_name": "who"},
        item_filter={},
    )

    def handler(request):
        if request.method == "POST":
            import json

            assert request.url.path == "/v2/acts/someone~other-actor/runs"
            assert json.loads(request.content) == {"query": ["k"], "limit": 5, "nested": {"from": "2026-11-07"}}
            assert "maxTotalChargeUsd" not in request.url.params
            return httpx.Response(201, json={"data": {"id": "r", "status": "SUCCEEDED", "defaultDatasetId": "d"}})
        return httpx.Response(200, json=[{"link": "https://l/1", "body": {"text": "hello"}, "who": "Bo"}])

    posts = make(handler, settings).search(["k"], date(2026, 11, 7), 5)
    assert [(p.url, p.text, p.author_name) for p in posts] == [("https://l/1", "hello", "Bo")]


def test_helpers():
    assert dig({"a": {"b": 1}}, "a.b") == 1
    assert dig({"a": None}, "a.b") is None
    assert fill_input({"x": "$keywords", "y": ["$max_posts", "plain"]}, {"keywords": ["k"], "max_posts": 3}) == {
        "x": ["k"], "y": [3, "plain"],
    }
