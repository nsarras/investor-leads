"""The pipeline: plan queries, collect posts, classify them, export leads.

Code decides the order of steps. The model is called in two places only:
once to turn the industry and stage into search queries, and once per batch of
posts to classify them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .classifier import CLASSIFIER_VERSION, CONFIDENCE_LEVELS, classify_posts, is_lead
from .collectors import CollectorAuthError, CollectorError
from .collectors.linkedin import LinkedInCollector
from .collectors.x import XCollector, estimate_cost
from .config import X_MAX_DAYS, ConfigError, Keys, Settings
from .costs import CostLine, anthropic_cost, format_costs, x_cost
from .export import export
from .llm import AnthropicJSON
from .models import Classification, Post, QueryPlan
from .planner import PLAN_VERSION, plan_queries
from .store import Store, now_stamp

Log = Callable[[str], None]

# How far back to look when judging which searches have been worth their cost.
SEARCH_HISTORY_DAYS = 30
# A search with little history is assumed to be average. This many posts of evidence
# count as much as that assumption.
PRIOR_POSTS = 20


@dataclass
class RunResult:
    plan: QueryPlan
    collected: dict[str, int] = field(default_factory=dict)
    new_posts: int = 0
    classified: int = 0
    leads: int = 0
    leads_by_confidence: dict[str, int] = field(default_factory=dict)
    lead_items: list[Any] = field(default_factory=list)  # the Lead rows, strongest first
    posts_considered: int = 0  # classified posts the sheet was built from
    only_new: bool = False
    csv_path: Path | None = None
    xlsx_path: Path | None = None
    x_cost_estimate: float = 0.0
    warnings: list[str] = field(default_factory=list)
    dry_run: bool = False
    costs: list[CostLine] = field(default_factory=list)

    @property
    def total_cost(self) -> float:
        return sum(line.usd for line in self.costs if line.usd is not None)


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "leads"


def output_name(settings: Settings, now: datetime) -> str:
    """File name stem, e.g. leads-gaming-entertainment-pre-seed-seed-2026-11-14."""
    industries, stages = slug(" ".join(settings.industries)), slug(" ".join(settings.stages))
    return f"leads-{industries}-{stages}-{now.date().isoformat()}"


def active_platforms(settings: Settings, keys: Keys, log: Log) -> tuple[bool, bool]:
    """Which platforms can run: enabled in config and with an API key present."""
    use_x = settings.x.enabled and bool(keys.x_bearer)
    use_linkedin = settings.linkedin.enabled and bool(keys.apify)
    if settings.x.enabled and not keys.x_bearer:
        log("! X_BEARER_TOKEN is not set, so X is skipped.")
    if settings.linkedin.enabled and not keys.apify:
        log("! APIFY_API_TOKEN is not set, so LinkedIn is skipped.")
    if not use_x and not use_linkedin:
        raise ConfigError("No platform can run. Set X_BEARER_TOKEN and/or APIFY_API_TOKEN in .env.")
    return use_x, use_linkedin


def resolve_plan(
    settings: Settings, store: Store, get_llm: Callable[[], Any], *, use_x: bool, use_linkedin: bool, replan: bool
) -> QueryPlan:
    """Queries come from config if given, else the cached plan, else the model."""
    manual_x, manual_li = settings.x_queries, settings.linkedin_keywords
    need_model_x = use_x and not manual_x
    need_model_li = use_linkedin and not manual_li

    plan = QueryPlan(source="config")
    if need_model_x or need_model_li:
        cached = None if replan else store.get_plan(settings.target, PLAN_VERSION)
        covers = (
            cached
            and (not need_model_x or (cached.x_queries and cached.requested_x >= settings.x.max_queries))
            and (not need_model_li or (cached.linkedin_keywords and cached.requested_linkedin >= settings.linkedin.max_keywords))
        )
        if covers:
            plan = cached
        else:
            plan = plan_queries(
                get_llm(),
                settings.industries,
                settings.stages,
                max_x=settings.x.max_queries,
                max_linkedin=settings.linkedin.max_keywords,
            )
            plan.requested_x, plan.requested_linkedin = settings.x.max_queries, settings.linkedin.max_keywords
            store.save_plan(settings.target, plan, PLAN_VERSION)

    return QueryPlan(
        x_queries=(manual_x or plan.x_queries)[: settings.x.max_queries] if use_x else [],
        linkedin_keywords=(manual_li or plan.linkedin_keywords)[: settings.linkedin.max_keywords] if use_linkedin else [],
        source=plan.source,
    )


def _norm_query(text: str) -> str:
    return " ".join(text.lower().split())


def rank_searches(
    searches: list[str], history: list[tuple[str, Classification]], min_confidence: str
) -> tuple[list[str], dict[str, int]]:
    """Order searches by how often the posts they found turned into leads.

    `history` is (search, classification) for posts collected earlier. Each search is scored
    by the share of its posts that produced a lead at `min_confidence` or stronger. A search
    with no history is assumed to be average, so it is tried ahead of the ones known to
    produce nothing. Ties keep their original order.

    Returns the ordered searches and a count of each kind: proven, untried, unproductive.
    """
    posts: dict[str, int] = {}
    leads: dict[str, int] = {}
    for search, classification in history:
        key = _norm_query(search)
        posts[key] = posts.get(key, 0) + 1
        if is_lead(classification, min_confidence):
            leads[key] = leads.get(key, 0) + 1
    known = {_norm_query(s) for s in searches}
    total_posts = sum(count for key, count in posts.items() if key in known)
    total_leads = sum(count for key, count in leads.items() if key in known)
    average = total_leads / total_posts if total_posts else 0.0

    def score(search: str) -> float:
        key = _norm_query(search)
        return (leads.get(key, 0) + PRIOR_POSTS * average) / (posts.get(key, 0) + PRIOR_POSTS)

    ordered = sorted(searches, key=lambda s: -score(s))
    counts = {"proven": 0, "untried": 0, "unproductive": 0}
    for search in searches:
        key = _norm_query(search)
        counts["proven" if leads.get(key) else "unproductive" if posts.get(key) else "untried"] += 1
    return ordered, counts


def run(
    settings: Settings,
    keys: Keys,
    *,
    dry_run: bool = False,
    replan: bool = False,
    only_new: bool = False,
    log: Log = print,
    llm: Any | None = None,
    x_collector: XCollector | None = None,
    linkedin_collector: LinkedInCollector | None = None,
    store: Store | None = None,
    now: datetime | None = None,
) -> RunResult:
    """Run the whole pipeline once. The keyword-only collaborators exist so tests can stub them.

    With `only_new`, the spreadsheet lists only leads from posts collected for the first
    time in this run, which is what a daily run wants: nothing reported yesterday repeats.
    """
    settings.validate()
    started = now_stamp()  # the real clock, in the same form as each post's first_seen
    use_x, use_linkedin = active_platforms(settings, keys, log)

    def get_llm() -> Any:
        nonlocal llm
        if llm is None:
            if not keys.anthropic:
                raise ConfigError("ANTHROPIC_API_KEY is not set. Add it to .env.")
            llm = AnthropicJSON(keys.anthropic, settings.model)
        return llm

    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=settings.days)
    since_date = since.date().isoformat()
    store = store or Store(settings.data_dir / "leads.db")

    log(f"Industries: {', '.join(settings.industries)}")
    log(f"Stages:     {', '.join(settings.stages)}")
    log(f"Window:     posts since {since_date}")

    # 1. Plan
    plan = resolve_plan(settings, store, get_llm, use_x=use_x, use_linkedin=use_linkedin, replan=replan)
    result = RunResult(plan=plan, dry_run=dry_run, only_new=only_new)
    ranking = None
    if use_linkedin and plan.linkedin_keywords:
        # Search the keywords that have produced leads first. With an Apify budget set, the
        # ones at the end of the list are the ones that go unsearched.
        history_from = (now - timedelta(days=SEARCH_HISTORY_DAYS)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        history = store.search_history(settings.target, "linkedin", history_from)
        if history:
            plan.linkedin_keywords, ranking = rank_searches(plan.linkedin_keywords, history, settings.min_confidence)
    log(f"\nSearch plan (from {plan.source}):")
    for query in plan.x_queries:
        log(f"  X         {query}")
    for keyword in plan.linkedin_keywords:
        log(f"  LinkedIn  {keyword}")
    for platform, active, queries in (("X", use_x, plan.x_queries), ("LinkedIn", use_linkedin, plan.linkedin_keywords)):
        if active and not queries:
            warning = f"No usable {platform} searches in the plan. Re-run with --replan or set them in config.yaml."
            result.warnings.append(warning)
            log(f"! {warning}")

    if use_x and plan.x_queries:
        result.x_cost_estimate = estimate_cost(
            len(plan.x_queries), settings.x.max_posts_per_query, settings.x.include_author_profile
        )
        log(f"\nX worst-case cost this run: ${result.x_cost_estimate:.2f} (cap ${settings.x.max_cost_usd:.2f})")
        if settings.days > X_MAX_DAYS:
            log(f"  X search only reaches back {X_MAX_DAYS} days; LinkedIn uses the full {settings.days}.")
        if result.x_cost_estimate > settings.x.max_cost_usd:
            use_x = False
            warning = "X skipped: the cost estimate exceeds x.max_cost_usd. Lower the caps or raise the limit."
            result.warnings.append(warning)
            log(f"! {warning}")
    if use_linkedin and plan.linkedin_keywords:
        cap = len(plan.linkedin_keywords) * settings.linkedin.max_posts_per_keyword
        budget, limit = settings.linkedin.max_cost_usd, settings.linkedin.max_charge_usd
        if budget is not None:
            note = f" (Apify budget ${budget:.2f} for this run; searching stops when it is spent)"
        elif limit is not None:
            note = f" (Apify spending limit ${limit:.2f} per Actor run)"
        else:
            note = ""
        log(f"LinkedIn: up to {cap} posts via {settings.linkedin.actor_id}{note}")
        if ranking:
            log(
                f"  Keywords are searched best first, going by the last {SEARCH_HISTORY_DAYS} days: "
                f"{ranking['proven']} have produced leads, {ranking['untried']} are untried, "
                f"{ranking['unproductive']} have produced none."
            )

    if dry_run:
        log("\nDry run: nothing was collected and no API credits were spent on X or Apify.")
        _report_costs(result, settings, llm, None, None, log)
        return result

    # 2. Collect
    posts: list[Post] = []
    if use_x and plan.x_queries:
        x_collector = x_collector or XCollector(
            keys.x_bearer,
            lang=settings.x.lang,
            include_author_profile=settings.x.include_author_profile,
            exclude_terms=settings.x.exclude_terms,
            exclude_replies=settings.x.exclude_replies,
        )
        found: list[Post] = []
        log("\nCollecting from X...")
        for query in plan.x_queries:
            try:
                batch = x_collector.search(query, since, settings.x.max_posts_per_query)
            except CollectorAuthError as exc:
                result.warnings.append(str(exc))
                log(f"! {exc}")
                break
            except CollectorError as exc:
                result.warnings.append(f"X query failed ({query}): {exc}")
                log(f"  ! {exc}")
                continue
            log(f"  {len(batch):>3} posts  {query}")
            found.extend(batch)
        result.collected["x"] = len(found)
        posts.extend(found)

    if use_linkedin and plan.linkedin_keywords:
        linkedin_collector = linkedin_collector or LinkedInCollector(keys.apify, settings.linkedin, log=log)
        log("\nCollecting from LinkedIn (this can take a few minutes)...")
        try:
            found = linkedin_collector.search(
                plan.linkedin_keywords, since.date(), settings.linkedin.max_posts_per_keyword
            )
        except CollectorError as exc:
            result.warnings.append(str(exc))
            log(f"! {exc}")
            found = []
        log(f"  {len(found):>3} posts")
        result.collected["linkedin"] = len(found)
        posts.extend(found)

    result.new_posts = store.add_posts(posts)
    log(f"\n{len(posts)} posts collected, {result.new_posts} not seen before.")

    # 3. Classify (anything in the window not yet classified for this target)
    pending = store.unclassified(settings.target, since_date, CLASSIFIER_VERSION)
    if pending:
        log(f"Classifying {len(pending)} posts with {settings.model}...")
        model = get_llm()
        done = classify_posts(
            model,
            pending,
            settings.industries,
            settings.stages,
            batch_size=settings.batch_size,
            workers=settings.classify_workers,
            on_batch=lambda items: store.save_classifications(
                settings.target, settings.model, CLASSIFIER_VERSION, items
            ),
            log=log,
        )
        result.classified = len(done)
        if len(done) < len(pending):
            result.warnings.append(f"{len(pending) - len(done)} posts could not be classified; they will be retried next run.")

    # 4. Export
    _export(settings, store, since_date, now, result, log, first_seen_from=started if only_new else None)
    _report_costs(result, settings, llm, x_collector, linkedin_collector, log)
    return result


def _report_costs(result: RunResult, settings: Settings, llm: Any, x: Any, linkedin: Any, log: Log) -> None:
    """Add up what the run used on each API and print the summary."""
    posts, users = len(getattr(x, "post_ids", ())), len(getattr(x, "user_ids", ()))
    if posts or users:
        result.costs.append(
            CostLine("X", f"{posts:,} posts, {users:,} author profiles", x_cost(posts, users), "estimate at list prices")
        )
    runs = getattr(linkedin, "runs", 0)
    if runs:
        known = getattr(linkedin, "cost_known", False)
        detail = f"{runs} {'run' if runs == 1 else 'runs'}, {getattr(linkedin, 'items', 0):,} items"
        searched, planned = getattr(linkedin, "keywords_searched", 0), getattr(linkedin, "keywords_total", 0)
        if searched < planned:
            detail += f", {searched} of {planned} keywords"
        result.costs.append(
            CostLine(
                "Apify",
                detail,
                linkedin.cost_usd if known else None,
                "reported by Apify" if known else "see the Apify console",
            )
        )
    calls = getattr(llm, "call_count", 0)
    if calls:
        tokens_in, tokens_out = getattr(llm, "input_tokens", 0), getattr(llm, "output_tokens", 0)
        cost = anthropic_cost(settings.model, tokens_in, tokens_out)
        result.costs.append(
            CostLine(
                "Anthropic",
                f"{calls:,} calls, {tokens_in:,} in / {tokens_out:,} out tokens",
                cost,
                f"estimate, {settings.model}" if cost is not None else f"no price on file for {settings.model}",
            )
        )
    if result.costs:
        log("")
        for line in format_costs(result.costs):
            log(line)


def _export(
    settings: Settings, store: Store, since_date: str, now: datetime, result: RunResult, log: Log,
    first_seen_from: str | None = None,
) -> None:
    classified = store.classified(settings.target, since_date, first_seen_from)
    result.posts_considered = len(classified)
    result.csv_path, result.xlsx_path, leads = export(
        classified, settings.output_dir, output_name(settings, now), settings.min_confidence
    )
    result.leads = len(leads)
    result.lead_items = leads
    included = CONFIDENCE_LEVELS[: CONFIDENCE_LEVELS.index(settings.min_confidence) + 1]
    result.leads_by_confidence = {level: sum(1 for lead in leads if lead.confidence == level) for level in included}
    breakdown = ", ".join(f"{count} {level}" for level, count in result.leads_by_confidence.items() if count)
    kind = "new leads" if result.only_new else "leads"
    summary = f"{result.leads} {kind}" + (f" ({breakdown})" if breakdown else "")
    log(f"\n{summary} written to:\n  {result.csv_path}\n  {result.xlsx_path}")


def export_only(settings: Settings, *, log: Log = print, store: Store | None = None, now: datetime | None = None) -> RunResult:
    """Rebuild the spreadsheet from the local database without calling any API."""
    settings.validate()
    now = now or datetime.now(timezone.utc)
    since_date = (now - timedelta(days=settings.days)).date().isoformat()
    store = store or Store(settings.data_dir / "leads.db")
    result = RunResult(plan=QueryPlan(source="none"))
    _export(settings, store, since_date, now, result, log)
    return result
