"""Work out what a run cost on each API, for the summary printed at the end.

X and Anthropic amounts are estimates from what the run used, at list prices. The Apify
amount is the figure Apify itself reports for the run. The provider's own billing page is
always the final word.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import X_PRICE_PER_POST, X_PRICE_PER_USER

# USD per million tokens (input, output), from https://platform.claude.com/docs/en/about-claude/pricing
# (checked November 2026). Add a line here to price another model.
ANTHROPIC_PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-fable-5-2": (8.00, 40.00),
}


@dataclass
class CostLine:
    provider: str
    detail: str
    usd: float | None  # None when the amount could not be determined
    basis: str


def x_cost(posts: int, users: int) -> float:
    return posts * X_PRICE_PER_POST + users * X_PRICE_PER_USER


def anthropic_cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    prices = next((p for name, p in ANTHROPIC_PRICES_PER_MTOK.items() if model.startswith(name)), None)
    if prices is None:
        return None
    return (input_tokens * prices[0] + output_tokens * prices[1]) / 1_000_000


def format_costs(lines: list[CostLine]) -> list[str]:
    """Lines for the end-of-run summary, with a total."""
    if not lines:
        return []
    width = max(len(line.detail) for line in lines)
    out = ["API costs for this run:"]
    for line in lines:
        amount = f"${line.usd:.2f}" if line.usd is not None else "  n/a"
        out.append(f"  {line.provider:<10} {line.detail:<{width}}  {amount:>7}  ({line.basis})")
    known = [line.usd for line in lines if line.usd is not None]
    missing = [line.provider for line in lines if line.usd is None]
    total = f"  {'Total':<10} {'':<{width}}  {'$' + format(sum(known), '.2f'):>7}"
    out.append(total + (f"  (not counting {', '.join(missing)})" if missing else ""))
    return out
