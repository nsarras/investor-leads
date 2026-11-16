"""Turn industries and funding stages into search queries for each platform.

This is the one place the model shapes what gets collected. It runs once per
set of industries + stages and the result is cached, so later runs search the same way.
"""

from __future__ import annotations

from typing import Any, Protocol

from .models import QueryPlan

# Bump when the planning rules change, so cached plans made under older rules are replaced.
PLAN_VERSION = 3

X_QUERY_MAX_CHARS = 450
LINKEDIN_KEYWORD_MAX_CHARS = 80

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "x_queries": {"type": "array", "items": {"type": "string"}},
        "linkedin_keywords": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["x_queries", "linkedin_keywords"],
    "additionalProperties": False,
}

SYSTEM = """\
You plan social-media searches for a startup founder who is looking for investors.

Given one or more target industries and one or more target funding stages, write searches
that surface recent posts showing which investors are active in those industries. Two kinds
of post are valuable: posts written by investors themselves (a deal they did, a new fund, a
call for pitches, a thesis), and funding announcements that name the investors in a round.
Every post found is read afterwards and each one costs money, so a search must be likely to
return investing-related posts, not general chatter about the industry.

Rules for x_queries (X / Twitter search):
- Write only the keyword part. Retweet, reply, language and spam filters are added
  automatically.
- Allowed syntax: words (implicit AND), "exact phrases", OR, parentheses. Do not use
  -exclusions.
- Each query is two AND-ed groups: (investing or funding phrases) (industry terms). Never add
  a third required group, and never require stage or role words as a group of their own.
- Build both groups from multi-word phrases and unambiguous terms. Everyday words with other
  meanings pull in noise, so these must never appear as bare words: games, game, media,
  film, streaming, music, angel, fund, raised, raises, investor, investors, VC, seed, backed.
  Use them only inside phrases: "game studio", "gaming startup", "seed round", "angel
  investor", "venture capital", "gaming fund", "led by".
- Industry groups hold 4 to 8 specific terms, for example:
  ("game studio" OR "gaming startup" OR "games company" OR gamedev OR esports OR "indie games" OR "video game")
- Give every query a different investing angle. Cycle through the angles below, and when
  several industries are given write separate queries for each industry.
  1. Investors announcing deals: ("we invested" OR "our investment in" OR "proud to back" OR "excited to back" OR "we led" OR "our portfolio company" OR "newest portfolio")
  2. Round announcements: ("seed round" OR "pre-seed round" OR "funding round" OR "led by" OR "participation from" OR "closes seed")
  3. Funds: ("new fund" OR "fund II" OR "first close" OR "final close" OR "venture fund" OR "launches fund")
  4. Open calls: ("open to pitches" OR "send me your deck" OR "looking to invest" OR "actively investing" OR "pitch us" OR "office hours")
  5. Early-stage money: ("pre-seed" OR "seed stage" OR "seed funding" OR "angel round" OR "angel investors" OR "early-stage")
  6. Investor identity: ("venture capital" OR "early-stage investor" OR "angel investor" OR "general partner" OR "venture partner")
  For angle 3 a single group of industry-specific phrases also works:
  ("gaming fund" OR "games VC" OR "gaming VC" OR "games investor" OR "esports fund")
- Keep each query under 400 characters.

Rules for linkedin_keywords (LinkedIn post search):
- Plain keyword phrases only, 3 to 6 words, no operators or quotation marks.
- Every phrase combines an investing term with an industry term, worded the way an investor
  or a funding announcement would write it: "we invested in game studio", "proud to back
  gaming startup", "gaming startup raises seed round", "new venture fund for games",
  "looking to invest gaming founders", "led pre-seed round gaming".
- Vary the angle (deals, new funds, calls for founders, portfolio news) and spread the
  phrases across the industries and stages given.

Return exactly the number of queries and keywords requested, best first.
"""


class JSONModel(Protocol):
    def json(self, *, system: str, user: str, schema: dict[str, Any], max_tokens: int = ...) -> dict[str, Any]: ...


def _clean(items: Any, limit: int, max_chars: int) -> list[str]:
    seen: set[str] = set()
    cleaned: list[str] = []
    for item in items if isinstance(items, list) else []:
        text = " ".join(str(item).split())
        key = text.lower()
        if not text or len(text) > max_chars or key in seen:
            continue
        seen.add(key)
        cleaned.append(text)
        if len(cleaned) == limit:
            break
    return cleaned


def plan_queries(
    llm: JSONModel, industries: list[str], stages: list[str], *, max_x: int, max_linkedin: int
) -> QueryPlan:
    """Ask the model for search queries, then validate and trim them in code."""
    user = (
        f"Target industries (any of): {', '.join(industries)}\n"
        f"Target funding stages (any of): {', '.join(stages)}\n"
        f"Write {max_x} x_queries and {max_linkedin} linkedin_keywords."
    )
    # Room for the model's reasoning plus the queries themselves, which grow with the count asked for.
    max_tokens = 8000 + 400 * max_x + 100 * max_linkedin
    result = llm.json(system=SYSTEM, user=user, schema=PLAN_SCHEMA, max_tokens=max_tokens)
    return QueryPlan(
        x_queries=_clean(result.get("x_queries"), max_x, X_QUERY_MAX_CHARS),
        linkedin_keywords=_clean(result.get("linkedin_keywords"), max_linkedin, LINKEDIN_KEYWORD_MAX_CHARS),
        source="model",
    )
