from datetime import datetime, timedelta, timezone

import httpx
import pytest

from investor_leads.collectors import CollectorAuthError
from investor_leads.collectors.x import XCollector, clamp_start, estimate_cost

SINCE = datetime.now(timezone.utc) - timedelta(days=3)


def page(ids, next_token=None):
    meta = {"result_count": len(ids)}
    if next_token:
        meta["next_token"] = next_token
    return {
        "data": [
            {"id": i, "text": f"post {i}", "author_id": "u1", "created_at": "2026-11-12T10:00:00.000Z",
             "public_metrics": {"like_count": 7}}
            for i in ids
        ],
        "includes": {"users": [{"id": "u1", "name": "Ana", "username": "ana_vc",
                                "description": "Partner at Pixel Ventures", "public_metrics": {"followers_count": 42}}]},
        "meta": meta,
    }


def collector(handler, **kwargs):
    return XCollector("token", client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None, **kwargs)


def test_search_paginates_and_normalizes():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "next_token" in request.url.params:
            return httpx.Response(200, json=page(["3"]))
        return httpx.Response(200, json=page(["1", "2"], next_token="abc"))

    xc = collector(handler)
    posts = xc.search("gaming fund", SINCE, max_posts=50)

    assert [p.post_id for p in posts] == ["1", "2", "3"]
    assert (len(xc.post_ids), len(xc.user_ids)) == (3, 1)  # what the cost summary is built from
    first = requests[0].url.params
    assert first["query"] == "(gaming fund) -is:retweet -is:reply lang:en"
    assert first["max_results"] == "50"
    assert first["expansions"] == "author_id"
    assert requests[0].headers["authorization"] == "Bearer token"
    post = posts[0]
    assert post.url == "https://x.com/ana_vc/status/1"
    assert post.author_headline == "Partner at Pixel Ventures"
    assert post.author_followers == 42 and post.engagement == 7
    assert post.matched_query == "gaming fund"


def test_stops_at_max_posts():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=page([str(len(calls) * 10 + i) for i in range(10)], next_token="more"))

    posts = collector(handler).search("q", SINCE, max_posts=20)
    assert len(posts) == 20 and len(calls) == 2


def test_falls_back_to_post_fields_when_tweet_fields_is_rejected():
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        if "tweet.fields" in request.url.params:
            return httpx.Response(400, json={"errors": [{"message": "The query parameter [tweet.fields] is not valid"}]})
        return httpx.Response(200, json=page(["1"]))

    posts = collector(handler).search("q", SINCE, max_posts=10)
    assert len(posts) == 1
    assert "post.fields" in seen[1] and "tweet.fields" not in seen[1]


def test_bad_query_reports_the_original_error():
    from investor_leads.collectors import CollectorError

    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(400, json={"errors": [{"message": f"Invalid query (attempt {len(calls)})"}]})

    with pytest.raises(CollectorError, match=r"attempt 1"):
        collector(handler).search("q", SINCE, 10)
    assert len(calls) == 2  # one retry with the other parameter name, then give up


def test_rate_limit_waits_then_retries():
    import time

    waits, calls = [], []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"x-rate-limit-reset": str(int(time.time()) + 5)}, json={})
        return httpx.Response(200, json=page(["1"]))

    xc = XCollector("t", client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=waits.append)
    assert len(xc.search("q", SINCE, 10)) == 1
    assert len(waits) == 1 and 1 <= waits[0] <= 10


def test_auth_failure_is_reported_clearly():
    with pytest.raises(CollectorAuthError, match="X_BEARER_TOKEN"):
        collector(lambda r: httpx.Response(401, json={"title": "Unauthorized"})).search("q", SINCE, 10)


def test_no_author_expansion_when_disabled():
    def handler(request):
        assert "expansions" not in request.url.params
        return httpx.Response(200, json={"data": [{"id": "9", "text": "hi"}], "meta": {}})

    posts = collector(handler, include_author_profile=False).search("q", SINCE, 10)
    assert posts[0].url == "https://x.com/i/status/9"


def test_spam_terms_are_excluded_from_every_query():
    xc = XCollector("t", exclude_terms=["airdrop", "free mint", " "], client=httpx.Client(transport=httpx.MockTransport(lambda r: None)))
    assert xc.build_query("gaming fund") == '(gaming fund) -is:retweet -is:reply lang:en -airdrop -"free mint"'
    keep_replies = XCollector("t", exclude_replies=False, lang="", client=httpx.Client(transport=httpx.MockTransport(lambda r: None)))
    assert keep_replies.build_query("gaming fund") == "(gaming fund) -is:retweet"


def test_start_time_is_clamped_to_seven_days():
    now = datetime(2026, 11, 14, tzinfo=timezone.utc)
    clamped = clamp_start(now - timedelta(days=30), now)
    assert now - clamped < timedelta(days=7)
    recent = now - timedelta(days=2)
    assert clamp_start(recent, now) == recent


def test_cost_estimate():
    assert estimate_cost(12, 25, include_author_profile=True) == 4.50  # the default caps
    assert estimate_cost(6, 50, include_author_profile=True) == 4.50
    assert estimate_cost(6, 50, include_author_profile=False) == 1.50
    assert estimate_cost(1, 5, include_author_profile=False) == 0.05  # smallest page is 10 posts
