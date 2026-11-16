import csv
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from conftest import FakeLLM, make_news, make_post
from openpyxl import load_workbook

from investor_leads.classifier import CLASSIFIER_VERSION
from investor_leads.collectors import CollectorAuthError
from investor_leads.config import ConfigError, Keys, Settings, load_settings
from investor_leads.export import AUTHOR, LEAD_COLUMNS, NAMED
from investor_leads.pipeline import export_only, run
from investor_leads.planner import PLAN_VERSION
from investor_leads.store import Store

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
KEYS = Keys(anthropic="a", x_bearer="x", apify="l")
HEADERS = [header for header, _ in LEAD_COLUMNS]


class FakeX:
    def __init__(self, posts, fail=None):
        self.posts, self.fail, self.queries = posts, fail, []

    def search(self, query, since, max_posts):
        self.queries.append(query)
        if self.fail:
            raise self.fail
        return self.posts if len(self.queries) == 1 else []


class FakeLinkedIn:
    def __init__(self, posts):
        self.posts, self.calls = posts, []

    def search(self, keywords, since, max_posts):
        self.calls.append((keywords, since, max_posts))
        return self.posts


def settings(tmp_path, **kwargs):
    return Settings(industry="Gaming, Entertainment", stage="Seed", data_dir=tmp_path / "data",
                    output_dir=tmp_path / "out", **kwargs)


def do_run(tmp_path, x, li, llm=None, s=None, **kwargs):
    logs = []
    result = run(s or settings(tmp_path), KEYS, llm=llm or FakeLLM(), x_collector=x, linkedin_collector=li,
                 now=NOW, log=logs.append, **kwargs)
    return result, logs


def read_csv(path):
    with path.open(encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def cell(sheet, row, header, headers=HEADERS):
    return sheet.cell(row=row, column=headers.index(header) + 1)


def test_end_to_end_writes_leads(tmp_path):
    x = FakeX([make_post(1), make_post(2, investor=False), make_post(3, seeking=False)])
    li = FakeLinkedIn([make_post(4, platform="linkedin"), make_post(1)])  # second is a duplicate URL
    result, logs = do_run(tmp_path, x, li)

    assert result.collected == {"x": 3, "linkedin": 2}
    assert result.new_posts == 4 and result.classified == 4
    # Two investors asking for pitches are high. The investor with no signal is low, which is
    # left out by default; the founder is not a lead.
    assert result.leads == 2 and result.leads_by_confidence == {"high": 2, "medium": 0}
    assert any("2 leads (2 high, 0 medium)" in line for line in logs)
    assert result.csv_path.name == "leads-gaming-entertainment-seed-2026-11-14.csv"
    assert li.calls[0][0] == ["investing in gaming startups", "gaming startup raises seed"]

    rows = read_csv(result.csv_path)
    assert [(r["Name"], r["Confidence"]) for r in rows] == [("Person 4", "high"), ("Person 1", "high")]
    assert rows[0]["Firm"] == "Pixel Ventures" and rows[0]["Lead type"] == AUTHOR
    assert {r["Platform"] for r in rows} == {"X", "LinkedIn"}

    workbook = load_workbook(result.xlsx_path)
    assert workbook.sheetnames == ["Leads", "All classified"]
    assert workbook["Leads"].max_row == 3 and workbook["All classified"].max_row == 5
    audit = workbook["All classified"]
    assert sorted(audit.cell(row=r, column=1).value for r in range(2, 6)) == ["high", "high", "low, not listed", "not a lead"]
    link = cell(workbook["Leads"], 2, "Post link")
    assert link.hyperlink.target == link.value == "https://example.com/linkedin/4"


def test_investors_named_in_news_become_leads_on_the_same_sheet(tmp_path):
    news = [make_news(1, likes=2), make_news(2, likes=40), make_news(3, likes=5),
            make_news(4, investor="Play Ventures")]
    result, _ = do_run(tmp_path, FakeX([make_post(1)]), FakeLinkedIn(news))

    rows = read_csv(result.csv_path)
    assert [(r["Name"], r["Lead type"], r["Confidence"]) for r in rows] == [
        ("Person 1", AUTHOR, "high"),
        ("Merak Capital", NAMED, "medium"),   # three outlets, one row
        ("Play Ventures", NAMED, "medium"),
    ]
    merak = rows[1]
    assert merak["Mentions"] == "3"
    assert merak["Post link"] == "https://example.com/linkedin/news2"  # the most-liked post leads
    assert merak["Other posts"].split("\n") == ["https://example.com/linkedin/news3", "https://example.com/linkedin/news1"]
    assert merak["Posted by"] == "Startup News 2"
    assert (merak["Signal"], merak["Investor type"]) == ("made an investment", "vc firm")
    assert "Led the round" in merak["Why"] and merak["Intent score"] == ""

    audit = load_workbook(result.xlsx_path)["All classified"]
    named_col = [c.value for c in audit[1]].index("Named investors") + 1
    assert "Merak Capital (medium)" in {audit.cell(row=r, column=named_col).value for r in range(2, audit.max_row + 1)}


def test_min_confidence_sets_what_is_listed(tmp_path):
    posts = [make_post(1), make_post(3, seeking=False)]
    s = settings(tmp_path)
    assert s.min_confidence == "medium"  # low-confidence leads are left out unless asked for
    result, _ = do_run(tmp_path, FakeX(posts), FakeLinkedIn([make_news(1)]), s=s)
    assert [r["Name"] for r in read_csv(result.csv_path)] == ["Person 1", "Merak Capital"]

    s.min_confidence = "low"
    result = export_only(s, now=NOW, log=lambda m: None)
    assert result.leads_by_confidence == {"high": 1, "medium": 1, "low": 1}
    assert [r["Name"] for r in read_csv(result.csv_path)] == ["Person 1", "Merak Capital", "Person 3"]

    s.min_confidence = "high"
    result = export_only(s, now=NOW, log=lambda m: None)
    assert result.leads == 1 and result.leads_by_confidence == {"high": 1}


def test_same_author_with_two_posts_is_one_row(tmp_path):
    first, second = make_post(1), make_post(2)
    second.author_name, second.author_handle = first.author_name, first.author_handle
    result, _ = do_run(tmp_path, FakeX([first, second]), FakeLinkedIn([]))
    rows = read_csv(result.csv_path)
    assert len(rows) == 1 and rows[0]["Mentions"] == "2"


def test_second_run_reuses_plan_and_skips_classified_posts(tmp_path):
    llm = FakeLLM()
    do_run(tmp_path, FakeX([make_post(1)]), FakeLinkedIn([]), llm=llm)
    calls_after_first = len(llm.calls)  # one plan call + one classification call
    result, logs = do_run(tmp_path, FakeX([make_post(1), make_post(2)]), FakeLinkedIn([]), llm=llm)

    assert calls_after_first == 2
    assert len(llm.calls) == 3  # no second plan call, only the new post is classified
    assert result.plan.source == "cache" and result.new_posts == 1 and result.classified == 1
    assert result.leads == 2


def test_outdated_plan_and_classifications_are_redone(tmp_path):
    llm = FakeLLM()
    s = settings(tmp_path)
    do_run(tmp_path, FakeX([make_post(1)]), FakeLinkedIn([]), llm=llm, s=s)

    # Pretend both were produced by an older release.
    db = sqlite3.connect(s.data_dir / "leads.db")
    db.execute("UPDATE classifications SET version = ?", (CLASSIFIER_VERSION - 1,))
    old_plan = json.dumps({"x_queries": ["old query"], "linkedin_keywords": ["old keyword"]})  # no version = v1
    db.execute("UPDATE plans SET plan = ?", (old_plan,))
    db.commit(); db.close()

    x = FakeX([])
    result, _ = do_run(tmp_path, x, FakeLinkedIn([]), llm=llm, s=s)
    assert result.plan.source == "model" and "old query" not in x.queries
    assert result.classified == 1 and result.leads == 1
    assert Store(s.data_dir / "leads.db").get_plan(s.target, PLAN_VERSION) is not None


def test_asking_for_more_searches_replans(tmp_path):
    llm = FakeLLM()
    s = settings(tmp_path)
    s.x.max_queries = s.linkedin.max_keywords = 2
    do_run(tmp_path, FakeX([]), FakeLinkedIn([]), llm=llm, s=s)
    result, _ = do_run(tmp_path, FakeX([]), FakeLinkedIn([]), llm=llm, s=s)
    assert result.plan.source == "cache" and len(llm.calls) == 1   # same request: plan reused

    s.x.max_queries = s.linkedin.max_keywords = 6
    result, _ = do_run(tmp_path, FakeX([]), FakeLinkedIn([]), llm=llm, s=s)
    assert result.plan.source == "model" and "Write 6 x_queries and 6 linkedin_keywords" in llm.calls[-1]["user"]

    s.x.max_queries = s.linkedin.max_keywords = 3                  # fewer: the cached plan is trimmed
    result, _ = do_run(tmp_path, FakeX([]), FakeLinkedIn([]), llm=llm, s=s)
    assert result.plan.source == "cache"


def test_database_from_first_release_is_upgraded(tmp_path):
    path = tmp_path / "data" / "leads.db"
    path.parent.mkdir()
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE classifications (url TEXT, target TEXT, is_investor INTEGER, intent_score INTEGER)")
    db.execute("INSERT INTO classifications VALUES ('u', 't', 1, 80)")
    db.commit(); db.close()

    result, _ = do_run(tmp_path, FakeX([make_post(1)]), FakeLinkedIn([]))
    assert result.classified == 1 and result.leads == 1


def test_dry_run_collects_nothing(tmp_path):
    x, li = FakeX([make_post(1)]), FakeLinkedIn([make_post(2)])
    result, logs = do_run(tmp_path, x, li, dry_run=True)
    assert x.queries == [] and li.calls == []
    assert result.dry_run and result.csv_path is None
    assert result.x_cost_estimate == 0.75  # 2 planned queries x 25 posts x $0.015
    assert any("Dry run" in line for line in logs)


def test_x_is_skipped_when_estimate_exceeds_cap(tmp_path):
    s = settings(tmp_path)
    s.x.max_cost_usd = 0.5
    x, li = FakeX([make_post(1)]), FakeLinkedIn([make_post(2, platform="linkedin")])
    result, _ = do_run(tmp_path, x, li, s=s)
    assert x.queries == [] and result.leads == 1
    assert any("max_cost_usd" in w for w in result.warnings)


def test_x_auth_failure_does_not_stop_linkedin(tmp_path):
    x = FakeX([], fail=CollectorAuthError("X rejected the request (401)"))
    result, _ = do_run(tmp_path, x, FakeLinkedIn([make_post(2, platform="linkedin")]))
    assert len(x.queries) == 1  # stops after the first rejected query
    assert result.leads == 1 and any("401" in w for w in result.warnings)


def test_missing_keys(tmp_path):
    with pytest.raises(ConfigError, match="No platform"):
        run(settings(tmp_path), Keys(anthropic="a"), log=lambda m: None)
    # Only one platform key: the other platform is skipped, not an error.
    li = FakeLinkedIn([make_post(2, platform="linkedin")])
    result = run(settings(tmp_path), Keys(anthropic="a", apify="l"), llm=FakeLLM(), linkedin_collector=li,
                 now=NOW, log=lambda m: None)
    assert result.plan.x_queries == [] and result.leads == 1


def test_manual_queries_skip_the_planner(tmp_path):
    llm = FakeLLM()
    s = settings(tmp_path, x_queries=["my query"], linkedin_keywords=["my keyword"])
    x, li = FakeX([]), FakeLinkedIn([])
    result = run(s, KEYS, llm=llm, x_collector=x, linkedin_collector=li, now=NOW, log=lambda m: None)
    assert result.plan.source == "config" and x.queries == ["my query"] and llm.calls == []


def test_export_only_and_window(tmp_path):
    old = make_post(9)
    old.posted_at = "2026-10-01T00:00:00.000Z"
    do_run(tmp_path, FakeX([make_post(1), old]), FakeLinkedIn([]))
    result = export_only(settings(tmp_path), now=NOW, log=lambda m: None)
    assert result.leads == 1  # the October post is outside the 7-day window


def test_formula_like_text_is_not_executable(tmp_path):
    evil = make_post(1)
    evil.author_name = "=HYPERLINK(\"http://evil\")"
    result, _ = do_run(tmp_path, FakeX([evil]), FakeLinkedIn([]))
    assert read_csv(result.csv_path)[0]["Name"].startswith("'=")
    name = cell(load_workbook(result.xlsx_path)["Leads"], 2, "Name")
    assert name.data_type == "s" and name.value.startswith("=HYPERLINK")


def test_multiple_stages(tmp_path):
    llm = FakeLLM()
    s = Settings(industry="Gaming, Entertainment", stage=" Pre-seed,seed , SEED,, ", data_dir=tmp_path / "d",
                 output_dir=tmp_path / "o")
    assert s.industries == ["Gaming", "Entertainment"]
    assert s.stages == ["Pre-seed", "seed"]  # trimmed, blanks and repeats dropped

    logs = []
    result = run(s, KEYS, llm=llm, x_collector=FakeX([make_post(1)]), linkedin_collector=FakeLinkedIn([]),
                 now=NOW, log=logs.append)
    assert "Stages:     Pre-seed, seed" in logs
    assert "Target funding stages (any of): Pre-seed, seed" in llm.calls[0]["user"]  # planner
    assert json.loads(llm.calls[1]["user"])["target"] == {            # classifier
        "industries": ["Gaming", "Entertainment"], "stages": ["Pre-seed", "seed"],
    }
    assert result.csv_path.name == "leads-gaming-entertainment-pre-seed-seed-2026-11-14.csv"
    assert result.leads == 1


def test_target_ignores_order_and_case():
    a = Settings(industry="gaming, entertainment", stage="pre-seed, seed")
    b = Settings(industry="Entertainment,Gaming", stage="Seed,  Pre-Seed")
    assert a.target == b.target == "entertainment,gaming|pre-seed,seed"
    assert Settings(industry="gaming", stage="seed").target != a.target


def test_stage_and_industry_accept_yaml_lists(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("industry: [gaming, entertainment]\nstage:\n  - pre-seed\n  - seed\n")
    s = load_settings(path)
    assert (s.industry, s.stages) == ("gaming, entertainment", ["pre-seed", "seed"])
    assert load_settings(path, {"stage": "series A, series B"}).stages == ["series A", "series B"]

    path.write_text("industry: gaming\nstage: [seed, 3]\n")
    with pytest.raises(ConfigError, match="stage"):
        load_settings(path)
    with pytest.raises(ConfigError, match="funding stage"):
        Settings(industry="gaming", stage=" , ").validate()


def test_config_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("industry: fintech\nstage: seed\nmin_confidence: medium\nx:\n  max_queries: 3\n"
                    "  exclude_terms: [airdrop]\nlinkedin:\n  max_charge_usd: null\n")
    s = load_settings(path, {"stage": "series A", "days": None})
    assert (s.industry, s.stage, s.days, s.x.max_queries, s.linkedin.max_charge_usd) == ("fintech", "series A", 7, 3, None)
    assert (s.min_confidence, s.x.exclude_terms, s.target) == ("medium", ["airdrop"], "fintech|series a")

    path.write_text("industry: fintech\nx:\n  max_querys: 3\n")
    with pytest.raises(ConfigError, match="x.max_querys"):
        load_settings(path)
    with pytest.raises(ConfigError, match="funding stage"):
        Settings(industry="x").validate()
    with pytest.raises(ConfigError, match="min_confidence"):
        Settings(industry="x", stage="seed", min_confidence="certain").validate()
    for bad in ("days: seven\n", "x:\n  enabled: 1\n", "x_queries: just one string\n", "linkedin:\n  max_charge_usd: lots\n"):
        path.write_text(bad)
        with pytest.raises(ConfigError, match="must be"):
            load_settings(path)


def test_example_config_matches_defaults():
    example = Path(__file__).parent.parent / "config.example.yaml"
    assert load_settings(example) == Settings()


def test_store_roundtrip(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    assert store.add_posts([make_post(1), make_post(1), make_post(2)]) == 2
    assert len(store.unclassified("t", "2026-11-01", CLASSIFIER_VERSION)) == 2
    assert store.unclassified("t", "2026-12-01", CLASSIFIER_VERSION) == []


# ---- merging leads into one row per investor -------------------------------------------------

def _news_with(named, n=1, likes=0):
    from investor_leads.models import Classification

    post = make_news(n, likes=likes)
    return post, Classification(url=post.url, is_investor=False, author_type="media", firm="", role="",
                                intent="not_investing_signal", intent_score=0, named_investors=named)


def _named(name, firm="", kind="vc_firm", action="invested"):
    from investor_leads.models import NamedInvestor

    return NamedInvestor(name, firm=firm, kind=kind, action=action, evidence=f"{name} did a deal.",
                         industry_match="yes", stage_match="yes")


def test_entity_key_groups_spellings_of_one_name():
    from investor_leads.export import _similar, entity_key

    assert entity_key("Backed VC") == entity_key("BACKED VC") == entity_key("Backed") == "backed"
    assert entity_key("Index Ventures") == entity_key("Index")
    assert entity_key("Bain Capital Ventures (BCV)") == "bain"
    assert entity_key("Tech Stars") == entity_key("Techstars")
    assert entity_key("MerakCapital") == entity_key("Merak Capital") == "merak"
    assert entity_key("DENMU Fund I") == "denmu"
    assert entity_key("BD Partners") == "bdpartners"            # too short to drop the suffix
    assert entity_key("General Catalyst") != entity_key("General Atlantic")
    assert _similar(entity_key("Andreessen Horowitz"), entity_key("Andreeson Horowitz"))
    assert not _similar(entity_key("Saga Ventures"), entity_key("Sapa Ventures"))  # short names must match exactly


def test_spelling_variants_are_one_row():
    from investor_leads.export import build_leads

    classified = [
        _news_with([_named("Andreessen Horowitz")], 1),
        _news_with([_named("Andreessen Horowitz")], 2),
        _news_with([_named("Andreeson Horowitz")], 3, likes=99),   # typo, and the most-liked post
        _news_with([_named("Play Ventures"), _named("PLAY VENTURES")], 4),
    ]
    leads = build_leads(classified, "low")
    assert sorted((lead.name, len(lead.posts)) for lead in leads) == [("Andreessen Horowitz", 3), ("Play Ventures", 1)]


def test_partners_are_folded_into_their_firm():
    from investor_leads.export import build_leads

    firm = "Archetype Media Finance"
    classified = [
        _news_with([_named(firm, kind="other", action="launched_fund"),
                    _named("Matthew Shreder", firm=firm), _named("Tyler Condon", firm=firm)], 1),
        _news_with([_named("Lutz Meschke", firm="Wealthgate Family Office")], 2),   # one person, firm not a lead
        _news_with([_named("Ana Ruiz", firm="Pixel Ventures"), _named("Bo Chen", firm="Pixel Ventures")], 3),
    ]
    leads = {lead.name: lead for lead in build_leads(classified, "low")}
    assert set(leads) == {firm, "Lutz Meschke", "Pixel Ventures"}
    assert leads[firm].why.endswith("People named: Matthew Shreder, Tyler Condon.")
    assert leads[firm].investor_type == "other"                      # the firm's own entry leads the row
    assert leads["Lutz Meschke"].firm == "Wealthgate Family Office"   # stays a person row
    assert "People named: Ana Ruiz, Bo Chen." in leads["Pixel Ventures"].why


def test_investor_who_posts_and_is_named_elsewhere_is_one_row(tmp_path):
    author = make_post(1, platform="linkedin")
    author.author_name, author.author_handle = "Merak Capital", "merak-capital"
    result, _ = do_run(tmp_path, FakeX([]), FakeLinkedIn([author, make_news(1), make_news(2)]))
    rows = read_csv(result.csv_path)
    assert len(rows) == 1
    assert (rows[0]["Name"], rows[0]["Lead type"], rows[0]["Confidence"], rows[0]["Mentions"]) == ("Merak Capital", AUTHOR, "high", "3")
    assert rows[0]["Profile link"] == "https://example.com/u/1"


def test_named_investor_in_unknown_industry_is_not_a_lead():
    from investor_leads.export import build_leads
    from investor_leads.models import NamedInvestor

    unknown = NamedInvestor("Tenacity Capital", evidence="Took part in a Series A.", kind="vc_firm", industry_match="unknown")
    assert build_leads([_news_with([unknown, _named("Play Ventures")])], "low")[0].name == "Play Ventures"
    assert len(build_leads([_news_with([unknown])], "low")) == 0


# ---- cost summary ----------------------------------------------------------------------------

def test_cost_summary_is_printed_at_the_end(tmp_path):
    x = FakeX([make_post(1)])
    x.post_ids, x.user_ids = set(map(str, range(79))), set(map(str, range(61)))
    li = FakeLinkedIn([make_news(1)])
    li.runs, li.items, li.cost_usd, li.cost_known = 2, 856, 1.3712, True
    result, logs = do_run(tmp_path, x, li)

    summary = logs[logs.index("API costs for this run:"):]
    assert [line.split()[0] for line in summary] == ["API", "X", "Apify", "Anthropic", "Total"]
    assert "79 posts, 61 author profiles" in summary[1] and "$1.00" in summary[1] and "estimate at list prices" in summary[1]
    assert "2 runs, 856 items" in summary[2] and "$1.37" in summary[2] and "reported by Apify" in summary[2]
    # FakeLLM reports 100,000 in / 20,000 out per call; two calls (plan + one batch) on Haiku 5.5 prices.
    assert "2 calls, 200,000 in / 40,000 out tokens" in summary[3] and "$0.04" in summary[3]
    assert summary[4].split()[-1] == "$2.42" and round(result.total_cost, 2) == 2.42
    assert logs[-1] == summary[-1]  # it is the last thing printed


def test_cost_summary_handles_unknown_amounts_and_dry_runs(tmp_path):
    from investor_leads.costs import CostLine, anthropic_cost, format_costs

    li = FakeLinkedIn([make_news(1)])
    li.runs, li.items, li.cost_usd, li.cost_known = 1, 10, 0.0, False
    s = settings(tmp_path, model="some-future-model")
    result, logs = do_run(tmp_path, FakeX([]), li, s=s)
    assert any("Apify" in line and "n/a" in line and "see the Apify console" in line for line in logs)
    assert any("no price on file for some-future-model" in line for line in logs)
    assert logs[-1].endswith("(not counting Apify, Anthropic)")

    _, dry_logs = do_run(tmp_path / "dry", FakeX([make_post(1)]), FakeLinkedIn([]), dry_run=True,
                         s=Settings(industry="gaming", stage="seed", data_dir=tmp_path / "dry", output_dir=tmp_path / "dry"))
    assert [line.split()[0] for line in dry_logs[dry_logs.index("API costs for this run:") + 1:]] == ["Anthropic", "Total"]

    assert anthropic_cost("claude-haiku-5-5", 1_000_000, 1_000_000) == pytest.approx(0.60)
    assert anthropic_cost("claude-sonnet-5-5", 1_000_000, 0) == pytest.approx(2.0)
    assert anthropic_cost("mystery", 1, 1) is None and format_costs([]) == []
    assert format_costs([CostLine("X", "1 post", 0.005, "estimate")])[-1].split()[-1] == "$0.01"
