"""Plain data records passed between pipeline stages."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Post:
    """One public post, normalized across platforms."""

    platform: str  # "x" or "linkedin"
    post_id: str
    url: str
    text: str
    posted_at: str  # ISO 8601 date or datetime, UTC
    author_name: str = ""
    author_handle: str = ""
    author_headline: str = ""  # X bio or LinkedIn headline
    author_url: str = ""
    author_followers: int | None = None
    engagement: int | None = None  # likes / reactions
    matched_query: str = ""


@dataclass
class NamedInvestor:
    """An investor a post names (for example in a funding announcement), other than its author."""

    name: str
    firm: str = ""
    evidence: str = ""
    industry_match: str = "unknown"  # "yes" | "generalist" | "no" | "unknown"
    stage_match: str = "unknown"  # "yes" | "no" | "unknown"
    kind: str = "other"  # vc_firm, angel, corporate_vc, accelerator, family_office, fund_of_funds, strategic, other
    action: str = "invested"  # invested, launched_fund, seeking_deals, mentioned_only


@dataclass
class Classification:
    """The model's reading of one post, relative to the target industries and stages."""

    url: str
    is_investor: bool
    author_type: str
    firm: str
    role: str
    intent: str
    intent_score: int
    sectors: list[str] = field(default_factory=list)
    stages: list[str] = field(default_factory=list)
    industry_match: str = "unknown"  # "yes" | "generalist" | "no" | "unknown"
    stage_match: str = "unknown"  # "yes" | "no" | "unknown"
    reason: str = ""
    named_investors: list[NamedInvestor] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Classification":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__ and k != "named_investors"}
        named = [
            NamedInvestor(**{k: v for k, v in item.items() if k in NamedInvestor.__dataclass_fields__})
            for item in data.get("named_investors", [])
            if isinstance(item, dict) and item.get("name")
        ]
        return cls(**known, named_investors=named)


@dataclass
class QueryPlan:
    """Search queries for each platform, generated from the industries and stages."""

    x_queries: list[str] = field(default_factory=list)
    linkedin_keywords: list[str] = field(default_factory=list)
    source: str = "model"  # "model" | "cache" | "config"
    # How many searches were asked for when the plan was made (0 = unknown). A cached plan
    # is replaced when the user later asks for more.
    requested_x: int = 0
    requested_linkedin: int = 0
