"""Checks for long-term invest plan buys: a separate profile from trading entries.

A plan buys broad funds on a schedule and holds them. So, unlike a trading entry:
- no stop-loss (a stop sells the dips a long-term investor means to hold through)
- the daily loss limit doesn't apply (buying on down days is the point of a schedule)
What still applies: the kill switch, only the plan's own symbols, no margin, and a size cap per
order set by the plan's contribution, under a hard ceiling no setting can raise.
"""

from __future__ import annotations

from collections.abc import Iterable

from guardrails.engine import Decision
from guardrails.models import AccountSnapshot

HARD_MAX_CONTRIBUTION = 100_000.0  # dollars per contribution; changing it is a reviewed code change
MIN_ORDER = 1.0                    # Alpaca's smallest dollar-amount order


def check_invest_buy(symbol: str, notional: float, account: AccountSnapshot, *, plan_symbols: Iterable[str],
                     max_notional: float, kill_switch: bool) -> Decision:
    reasons: list[str] = []
    if kill_switch:
        reasons.append("kill switch is on")
    if symbol not in set(plan_symbols):
        reasons.append(f"{symbol} isn't in the invest plan")
    if not notional >= MIN_ORDER:  # also catches NaN
        reasons.append(f"${notional:,.2f} is under Alpaca's ${MIN_ORDER:g} minimum")
    if notional > min(max_notional, HARD_MAX_CONTRIBUTION) + 0.005:
        reasons.append(f"${notional:,.2f} is more than this contribution allows (${max_notional:,.2f})")
    if notional > account.cash + 0.005:
        reasons.append(f"not enough cash (${account.cash:,.2f}; no margin)")
    return Decision(not reasons, reasons)
