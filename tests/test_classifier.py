import json

from conftest import FakeLLM, make_post

from investor_leads.classifier import (
    RESULT_SCHEMA,
    author_confidence,
    classify_posts,
    meets,
    named_confidence,
)
from investor_leads.llm import AnthropicJSON, LLMError
from investor_leads.models import Classification, NamedInvestor
from investor_leads.planner import plan_queries


def test_classifies_in_batches_and_reports_each_batch(fake_llm):
    posts = [make_post(i) for i in range(1, 6)]
    saved = []
    results = classify_posts(fake_llm, posts, ["gaming"], ["pre-seed", "seed"], batch_size=2, on_batch=saved.append, log=lambda m: None)

    assert [c.url for c in results] == [p.url for p in posts]
    assert len(fake_llm.calls) == 3 and [len(b) for b in saved] == [2, 2, 1]
    assert all(c.intent_score == 95 and c.firm == "Pixel Ventures" for c in results)
    sent = json.loads(fake_llm.calls[0]["user"])
    assert sent["target"] == {"industries": ["gaming"], "stages": ["pre-seed", "seed"]}
    assert sent["posts"][0]["author_bio"] == "Partner at Pixel Ventures"


def test_parallel_classification_keeps_order_and_saves_from_the_calling_thread(fake_llm):
    import threading

    posts = [make_post(i) for i in range(1, 126)]
    saved_on, logs = [], []
    results = classify_posts(fake_llm, posts, ["gaming"], ["seed"], batch_size=10, workers=4,
                             on_batch=lambda items: saved_on.append(threading.current_thread()), log=logs.append)

    assert [c.url for c in results] == [p.url for p in posts]
    assert len(fake_llm.calls) == 13 and len(saved_on) == 13
    assert set(saved_on) == {threading.current_thread()}  # the database is only touched from one thread
    assert logs[-1] == "  125 of 125 posts read" and len(logs) > 3


def test_post_skipped_by_model_is_retried_alone():
    llm = FakeLLM(skip_ids={"2"})
    posts = [make_post(i) for i in range(1, 4)]
    results = classify_posts(llm, posts, ["gaming"], ["seed"], batch_size=3, log=lambda m: None)
    assert len(results) == 3 and len(llm.calls) == 2


def test_failed_post_is_left_unclassified():
    class Broken:
        def json(self, **kwargs):
            raise LLMError("boom")

    logs = []
    assert classify_posts(Broken(), [make_post(1)], ["gaming"], ["seed"], log=logs.append) == []
    assert any("could not classify" in line for line in logs)


def judged(**changes):
    values = dict(url="u", is_investor=True, author_type="vc", firm="", role="", intent="seeking_deals",
                  intent_score=80, industry_match="yes", stage_match="unknown")
    values.update(changes)
    return Classification(**values)


def test_author_confidence_levels():
    assert author_confidence(judged()) == "high"
    # Individuals and angels rank exactly like funds: a firm or title is not required.
    individual = dict(author_type="angel", firm="", role="")
    assert author_confidence(judged(**individual)) == "high"
    assert author_confidence(judged(intent_score=55, intent="thesis_or_commentary", **individual)) == "medium"
    assert author_confidence(judged(industry_match="generalist", intent_score=90, **individual)) == "medium"
    for changes in ({}, {"intent_score": 55}, {"industry_match": "unknown"}, {"stage_match": "no"}):
        assert author_confidence(judged(**changes)) == author_confidence(judged(**changes, **individual))
    assert author_confidence(judged(intent_score=55, intent="thesis_or_commentary")) == "medium"
    # A generalist needs an explicit ask to reach medium.
    assert author_confidence(judged(industry_match="generalist", intent_score=90)) == "medium"
    assert author_confidence(judged(industry_match="generalist", intent_score=55)) == "low"
    # Partial fits are kept as "low" rather than dropped.
    assert author_confidence(judged(intent_score=20)) == "low"
    assert author_confidence(judged(industry_match="unknown")) == "low"           # signal, focus unknown
    assert author_confidence(judged(stage_match="no")) == "low"
    assert author_confidence(judged(intent="not_investing_signal", intent_score=5)) == "low"  # in the industry, no signal
    # Not a lead: not an investor, a focus that excludes the industries, or neither fit nor signal.
    assert author_confidence(judged(is_investor=False)) is None
    assert author_confidence(judged(industry_match="no")) is None
    assert author_confidence(judged(industry_match="unknown", intent="not_investing_signal")) is None
    assert author_confidence(judged(industry_match="generalist", intent="not_investing_signal")) is None


def test_named_investor_confidence_levels():
    assert named_confidence(NamedInvestor("Merak Capital", industry_match="yes", stage_match="yes")) == "medium"
    assert named_confidence(NamedInvestor("Merak Capital", industry_match="yes", stage_match="unknown")) == "medium"
    assert named_confidence(NamedInvestor("Big Growth Fund", industry_match="yes", stage_match="no")) == "low"
    assert named_confidence(NamedInvestor("An Angel", industry_match="generalist", action="seeking_deals")) == "low"
    # A deal in some other or unknown industry says nothing about the target industries.
    assert named_confidence(NamedInvestor("Someone", industry_match="unknown")) is None
    assert named_confidence(NamedInvestor("Someone", industry_match="unknown", stage_match="yes")) is None
    # A studio or publisher that funds projects is kept, but only as low.
    assert named_confidence(NamedInvestor("Big Studio", industry_match="yes", kind="strategic")) == "low"
    # Dropped: wrong industry, no investing action in the post, or a fund that only backs other funds.
    assert named_confidence(NamedInvestor("Biotech Fund", industry_match="no")) is None
    assert named_confidence(NamedInvestor("A Guest", industry_match="yes", action="mentioned_only")) is None
    assert named_confidence(NamedInvestor("Fund of Funds", industry_match="yes", kind="fund_of_funds")) is None


def test_meets():
    assert meets("high", "low") and meets("low", "low") and meets("medium", "medium")
    assert not meets("low", "medium") and not meets("medium", "high") and not meets(None, "low")


def test_named_investors_are_parsed(fake_llm):
    from conftest import make_news

    result = classify_posts(fake_llm, [make_news(1)], ["gaming"], ["seed"], log=lambda m: None)[0]
    assert not result.is_investor and author_confidence(result) is None
    assert [(n.name, n.industry_match, n.stage_match) for n in result.named_investors] == [("Merak Capital", "yes", "yes")]
    assert Classification.from_dict(result.to_dict()) == result  # survives storage as JSON


def test_schema_uses_only_supported_keywords():
    """Anthropic structured outputs reject numeric and length constraints and open objects."""
    text = json.dumps(RESULT_SCHEMA)
    for keyword in ("minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems"):
        assert keyword not in text
    item = RESULT_SCHEMA["properties"]["results"]["items"]
    assert item["additionalProperties"] is False and RESULT_SCHEMA["additionalProperties"] is False
    assert set(item["required"]) == set(item["properties"])
    named = item["properties"]["named_investors"]["items"]
    assert named["additionalProperties"] is False and set(named["required"]) == set(named["properties"])


def test_planner_cleans_and_trims():
    class Messy:
        def json(self, **kwargs):
            return {
                "x_queries": ["  a   b ", "A B", "", "x" * 500, "c d", "e f"],
                "linkedin_keywords": ["one two", "one two", "three four"],
            }

    plan = plan_queries(Messy(), ["gaming"], ["seed"], max_x=2, max_linkedin=5)
    assert plan.x_queries == ["a b", "c d"]
    assert plan.linkedin_keywords == ["one two", "three four"]


def test_planner_sends_every_industry_and_stage(fake_llm):
    plan_queries(fake_llm, ["gaming", "entertainment"], ["pre-seed", "seed"], max_x=2, max_linkedin=2)
    user = fake_llm.calls[0]["user"]
    assert "Target industries (any of): gaming, entertainment" in user
    assert "Target funding stages (any of): pre-seed, seed" in user


class _Block:
    def __init__(self, type_, text=""):
        self.type, self.text = type_, text


class _Response:
    def __init__(self, content, stop_reason="end_turn"):
        self.content, self.stop_reason = content, stop_reason


class _Client:
    """Mimics `client.messages.stream(...)` as a context manager. Replies are returned in order."""

    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []
        self.messages = self

    @property
    def kwargs(self):
        return self.calls[-1]

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


def test_anthropic_wrapper_sends_schema_and_reads_text_block():
    client = _Client(_Response([_Block("thinking"), _Block("text", '{"ok": true}')]))
    llm = AnthropicJSON("key", "claude-haiku-5-5", client=client)
    assert llm.json(system="s", user="u", schema={"type": "object"}) == {"ok": True}
    assert client.kwargs["model"] == "claude-haiku-5-5"
    assert client.kwargs["output_config"] == {"format": {"type": "json_schema", "schema": {"type": "object"}}}
    assert "temperature" not in client.kwargs


def test_cut_off_reply_is_retried_once_with_more_room():
    import pytest

    cut = _Response([], "max_tokens")
    client = _Client(cut, _Response([_Block("text", '{"ok": true}')]))
    assert AnthropicJSON("k", "m", client=client).json(system="s", user="u", schema={}, max_tokens=4000) == {"ok": True}
    assert [c["max_tokens"] for c in client.calls] == [4000, 8000]

    always_cut = _Client(cut)
    with pytest.raises(LLMError, match="cut off at 8000 tokens"):
        AnthropicJSON("k", "m", client=always_cut).json(system="s", user="u", schema={}, max_tokens=4000)
    assert len(always_cut.calls) == 2

    # Large limits are capped, and the planner asks for room in proportion to the searches wanted.
    big = _Client(_Response([_Block("text", "{}")]))
    AnthropicJSON("k", "m", client=big).json(system="s", user="u", schema={}, max_tokens=500000)
    assert big.kwargs["max_tokens"] == 64000


def test_planner_asks_for_room_in_proportion_to_the_searches():
    class Recorder:
        def json(self, **kwargs):
            self.max_tokens = kwargs["max_tokens"]
            return {"x_queries": [], "linkedin_keywords": []}

    small, large = Recorder(), Recorder()
    plan_queries(small, ["gaming"], ["seed"], max_x=2, max_linkedin=2)
    plan_queries(large, ["gaming"], ["seed"], max_x=30, max_linkedin=30)
    assert small.max_tokens == 9000 and large.max_tokens == 23000


def test_anthropic_wrapper_raises_on_refusal_or_bad_json():
    import pytest

    with pytest.raises(LLMError, match="refusal"):
        AnthropicJSON("k", "m", client=_Client(_Response([], "refusal"))).json(system="s", user="u", schema={})
    with pytest.raises(LLMError, match="invalid JSON"):
        AnthropicJSON("k", "m", client=_Client(_Response([_Block("text", "nope")]))).json(system="s", user="u", schema={})
