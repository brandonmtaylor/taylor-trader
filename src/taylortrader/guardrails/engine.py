from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from .limits import Limits
from .models import AccountSnapshot, OrderIntent, Side


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reasons: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.allowed


class Guardrails:
    """Checks every order intent against the limits before it can reach a broker.

    Exits (sells that reduce an existing long) are always allowed, even with the kill switch on:
    getting out of risk must never be blocked. Everything that adds risk is checked.
    """

    def __init__(self, limits: Limits | None = None, kill_switch: bool = False):
        self.limits = limits or Limits()
        self.limits.validate()
        self.kill_switch = kill_switch

    def check(self, intent: OrderIntent, account: AccountSnapshot) -> Decision:
        if self._is_exit(intent, account):
            return Decision(True)

        reasons: list[str] = []
        lim = self.limits

        if self.kill_switch:
            reasons.append("kill switch is on")
        if intent.side is not Side.BUY:
            reasons.append("short selling is not supported")
        if intent.qty <= 0 or intent.est_price <= 0:
            reasons.append("quantity and price must be positive")
        if account.equity <= 0:
            reasons.append("account equity is zero or unknown")

        # Every entry must carry a stop, held at the broker, within the allowed width.
        if intent.stop_loss_price is None:
            reasons.append("entry has no stop-loss")
        elif intent.est_price > 0:
            stop_pct = (intent.est_price - intent.stop_loss_price) / intent.est_price * 100
            if stop_pct <= 0:
                reasons.append("stop-loss must be below the entry price")
            elif stop_pct > lim.max_stop_loss_pct:
                reasons.append(f"stop-loss {stop_pct:.1f}% is wider than {lim.max_stop_loss_pct}%")

        if account.equity > 0:
            existing = account.positions.get(intent.symbol)
            position_value = (existing.market_value if existing else 0.0) + intent.notional
            position_pct = position_value / account.equity * 100
            if position_pct > lim.max_position_pct:
                reasons.append(
                    f"{intent.symbol} would be {position_pct:.1f}% of equity (limit {lim.max_position_pct}%)"
                )
            exposure_pct = (account.exposure + intent.notional) / account.equity * 100
            if exposure_pct > lim.max_total_exposure_pct:
                reasons.append(
                    f"total exposure would be {exposure_pct:.1f}% (limit {lim.max_total_exposure_pct}%)"
                )
            if intent.notional > account.cash:
                reasons.append("not enough cash (no margin)")

        if account.day_pnl_pct <= -lim.max_daily_loss_pct:
            reasons.append(
                f"down {abs(account.day_pnl_pct):.1f}% today (limit {lim.max_daily_loss_pct}%)"
            )
        if account.trades_today >= lim.max_trades_per_day:
            reasons.append(f"{account.trades_today} trades today (limit {lim.max_trades_per_day})")

        return Decision(not reasons, reasons)

    @staticmethod
    def _is_exit(intent: OrderIntent, account: AccountSnapshot) -> bool:
        pos = account.positions.get(intent.symbol)
        return intent.side is Side.SELL and pos is not None and pos.qty > 0 and intent.qty <= pos.qty


def find_unmanaged_positions(
    positions: Iterable[str], symbols_with_stop: Iterable[str], managed_symbols: Iterable[str]
) -> dict[str, list[str]]:
    """Positions that are missing protection or that no strategy owns.

    The original bot's worst failure was a position with no live stop and no running owner.
    """
    stops = set(symbols_with_stop)
    managed = set(managed_symbols)
    problems: dict[str, list[str]] = {}
    for symbol in positions:
        issues = []
        if symbol not in stops:
            issues.append("no stop-loss order at the broker")
        if symbol not in managed:
            issues.append("no enabled strategy manages this symbol")
        if issues:
            problems[symbol] = issues
    return problems
