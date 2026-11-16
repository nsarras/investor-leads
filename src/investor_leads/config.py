"""Settings (from config.yaml and CLI flags) and API keys (from the environment)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml

# X pay-per-use read prices in USD, from https://docs.x.com/x-api/getting-started/pricing
# (checked November 2026). X says prices can change; the Developer Console is authoritative.
X_PRICE_PER_POST = 0.005
X_PRICE_PER_USER = 0.010

# X recent search only reaches back this far.
X_MAX_DAYS = 7

DEFAULT_LINKEDIN_INPUT: dict[str, Any] = {
    "search": "$keywords",
    "maxPosts": "$max_posts",
    "postedAfter": "$since_date",
}

DEFAULT_LINKEDIN_FIELD_MAP: dict[str, str] = {
    "post_id": "_metadata.post_id",
    "url": "post_url",
    "text": "content",
    "posted_at": "posted_at",
    "author_name": "author.name",
    "author_handle": "author.username",
    "author_headline": "author.headline",
    "author_url": "author.linkedinUrl",
    "engagement": "engagement.total_reactions",
    "matched_query": "_metadata.search_term",
}


class ConfigError(ValueError):
    """Raised for a bad config file or missing required setting."""


@dataclass
class XSettings:
    enabled: bool = True
    max_queries: int = 12
    max_posts_per_query: int = 25
    lang: str = "en"
    include_author_profile: bool = True
    # The run stops before calling X if the worst-case cost estimate exceeds this.
    max_cost_usd: float = 5.0
    # Posts containing any of these words are excluded from every X search (spam filter).
    exclude_terms: list[str] = field(default_factory=lambda: ["airdrop", "giveaway", "presale", "memecoin"])
    # Replies are mostly conversation, not announcements, so they are left out of searches.
    exclude_replies: bool = True


@dataclass
class LinkedInSettings:
    enabled: bool = True
    # Any Apify Actor that searches public LinkedIn posts by keyword. To swap providers,
    # change actor_id and adjust `input` and `field_map` to that Actor's schema.
    actor_id: str = "atomus/linkedin-post-search"
    max_keywords: int = 12
    max_posts_per_keyword: int = 25
    # Most keywords the Actor accepts in one run; longer lists are split across runs.
    max_keywords_per_run: int = 20
    # Passed to Apify as the run's spending limit. Set to null to disable.
    max_charge_usd: float | None = 3.0
    run_timeout_seconds: int = 900
    # Actor input. "$keywords", "$max_posts" and "$since_date" are filled in per run.
    input: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_LINKEDIN_INPUT))
    # Our field name -> dotted path in the Actor's output items.
    field_map: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_LINKEDIN_FIELD_MAP))
    # Keep only items where these fields have these values (ignored when a field is absent).
    item_filter: dict[str, Any] = field(default_factory=lambda: {"type": "post"})


@dataclass
class EmailSettings:
    # Who receives the results when a run is started with --email.
    to: list[str] = field(default_factory=list)
    # How many leads to list in the body of the email; the spreadsheet always has all of them.
    max_leads_in_body: int = 10


@dataclass
class Settings:
    industry: str = ""
    stage: str = ""
    days: int = 7
    model: str = "claude-haiku-5-5"
    batch_size: int = 10
    # Classification calls made at the same time. Raise for big runs; lower if rate-limited.
    classify_workers: int = 4
    # Weakest leads to export. "medium" leaves out low-confidence leads (partial fits);
    # "low" includes them; "high" keeps only explicit asks.
    min_confidence: str = "medium"
    data_dir: Path = Path("data")
    output_dir: Path = Path("output")
    x: XSettings = field(default_factory=XSettings)
    linkedin: LinkedInSettings = field(default_factory=LinkedInSettings)
    email: EmailSettings = field(default_factory=EmailSettings)
    # Optional manual overrides. When set, the model is not asked to plan that platform.
    x_queries: list[str] = field(default_factory=list)
    linkedin_keywords: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.data_dir, self.output_dir = Path(self.data_dir), Path(self.output_dir)

    @property
    def industries(self) -> list[str]:
        """`industry` split on commas: "gaming, entertainment" -> ["gaming", "entertainment"]."""
        return split_list(self.industry)

    @property
    def stages(self) -> list[str]:
        """`stage` split on commas: "pre-seed, seed" -> ["pre-seed", "seed"]."""
        return split_list(self.stage)

    @property
    def target(self) -> str:
        """Key identifying this set of industries + stages in the local database.

        Order and letter case do not matter: "Seed, Pre-seed" and "pre-seed, seed" share one key.
        """
        industries = ",".join(sorted(_norm(i) for i in self.industries))
        stages = ",".join(sorted(_norm(s) for s in self.stages))
        return f"{industries}|{stages}"

    def validate(self) -> None:
        if not self.industries:
            raise ConfigError("Set an industry (--industry or `industry:` in config.yaml).")
        if not self.stages:
            raise ConfigError("Set a funding stage (--stage or `stage:` in config.yaml).")
        if self.days < 1:
            raise ConfigError("days must be at least 1.")
        if not 1 <= self.batch_size <= 50:
            raise ConfigError("batch_size must be between 1 and 50.")
        if not 1 <= self.classify_workers <= 16:
            raise ConfigError("classify_workers must be between 1 and 16.")
        if self.linkedin.max_keywords_per_run < 1:
            raise ConfigError("linkedin.max_keywords_per_run must be at least 1.")
        if self.min_confidence not in ("low", "medium", "high"):
            raise ConfigError("min_confidence must be low, medium or high.")
        for name in ("max_queries", "max_posts_per_query"):
            if getattr(self.x, name) < 1:
                raise ConfigError(f"x.{name} must be at least 1.")
        for name in ("max_keywords", "max_posts_per_keyword"):
            if getattr(self.linkedin, name) < 1:
                raise ConfigError(f"linkedin.{name} must be at least 1.")
        missing = {"url", "text"} - set(self.linkedin.field_map)
        if missing:
            raise ConfigError(f"linkedin.field_map must map: {', '.join(sorted(missing))}.")


@dataclass
class Keys:
    """API keys. Each user of the tool supplies their own."""

    anthropic: str = ""
    x_bearer: str = ""
    apify: str = ""

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Keys":
        env = os.environ if env is None else env
        return cls(
            anthropic=env.get("ANTHROPIC_API_KEY", "").strip(),
            x_bearer=env.get("X_BEARER_TOKEN", "").strip(),
            apify=env.get("APIFY_API_TOKEN", "").strip(),
        )


def _norm(value: str) -> str:
    return " ".join(value.lower().split())


def split_list(value: str) -> list[str]:
    """Split a comma-separated string into trimmed items, dropping blanks and repeats."""
    items: list[str] = []
    seen: set[str] = set()
    for part in value.split(","):
        item = " ".join(part.split())
        if item and _norm(item) not in seen:
            seen.add(_norm(item))
            items.append(item)
    return items


def _checked(value: Any, default: Any, name: str) -> Any:
    """Return `value` if it has the same kind of type as the default, else raise."""
    if isinstance(default, bool):
        ok, expected = isinstance(value, bool), "true or false"
    elif isinstance(default, int):
        ok, expected = isinstance(value, int) and not isinstance(value, bool), "a whole number"
    elif isinstance(default, float):
        ok, expected = isinstance(value, (int, float)) and not isinstance(value, bool), "a number"
    elif isinstance(default, str):
        ok, expected = isinstance(value, str), "text"
    elif isinstance(default, list):
        ok, expected = isinstance(value, list) and all(isinstance(v, str) for v in value), "a list of text items"
    elif isinstance(default, dict):
        ok, expected = isinstance(value, dict), "a mapping"
    else:
        ok, expected = True, ""
    if not ok:
        raise ConfigError(f"`{name}` must be {expected}.")
    return value


def _apply(target: Any, data: dict[str, Any], path: str = "") -> None:
    """Copy `data` onto dataclass `target`, rejecting unknown keys so typos surface."""
    known = {f.name: f for f in fields(target)}
    for key, value in data.items():
        if key not in known:
            raise ConfigError(f"Unknown setting `{path}{key}` in config file.")
        current = getattr(target, key)
        if is_dataclass(current):
            if not isinstance(value, dict):
                raise ConfigError(f"`{path}{key}` must be a mapping.")
            _apply(current, value, f"{path}{key}.")
        elif isinstance(current, Path):
            setattr(target, key, Path(str(value)))
        elif not path and key in ("industry", "stage") and isinstance(value, list):
            # A YAML list is accepted as well as a comma-separated string.
            if not all(isinstance(v, str) for v in value):
                raise ConfigError(f"`{key}` must be text or a list of text items.")
            setattr(target, key, ", ".join(value))
        elif key == "max_charge_usd":
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
                raise ConfigError(f"`{path}{key}` must be a number or null.")
            setattr(target, key, value)
        elif value is None:
            continue  # an empty YAML value keeps the default
        else:
            setattr(target, key, _checked(value, current, f"{path}{key}"))


def load_settings(path: str | Path | None = None, overrides: dict[str, Any] | None = None) -> Settings:
    """Build Settings from defaults, then the YAML file, then CLI overrides."""
    settings = Settings()
    if path is not None:
        file = Path(path)
        if not file.exists():
            raise ConfigError(f"Config file not found: {file}")
        loaded = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ConfigError("Config file must contain a YAML mapping.")
        _apply(settings, loaded)
    for key, value in (overrides or {}).items():
        if value is not None:
            setattr(settings, key, value)
    return settings
