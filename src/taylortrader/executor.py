"""The only path from a strategy signal to the broker. Sizing, stops, guardrails, journal, alerts."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from taylortrader.alerts import Alerts, Level
from taylortrader.config import StrategyConfig
from taylortrader.guardrails import AccountSnapshot, Decision, Guardrails, OrderIntent, Position, Side
from taylortrader.journal import Journal

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class EntryPlan:
    intent: OrderIntent
    decision: Decision
    submitted: bool


def plan_entry(sc: StrategyConfig, symbol: str, price: float, equity: float, reason: str) -> OrderIntent:
    qty = int((equity * sc.allocation_pct / 100) // price)
    stop = price * (1 - sc.stop_loss_pct / 100)
    tp = price * (1 + sc.take_profit_pct / 100) if sc.take_profit_pct > 0 else None
    return OrderIntent(symbol, Side.BUY, qty, price, stop, tp, reason=f"{sc.name}: {reason}")


class Executor:
    def __init__(self, broker, guardrails: Guardrails, journal: Journal | None = None,
                 alerts: Alerts | None = None, dry_run: bool = False):
        self.broker = broker
        self.guardrails = guardrails
        self.journal = journal
        self.alerts = alerts or Alerts()
        self.dry_run = dry_run
        # Money strategies don't size from (the invest plan's holdings and next contribution).
        self.reserved = lambda account: 0.0
        # Re-read just before a buy is sent: "pause" or "sell everything" can land mid-check.
        self.kill_check = lambda: False
        # Buys sent during this check. The account snapshot can't show them yet, so without this,
        # several signals in one check would each see the same room under the limits.
        self.inflight: dict[str, Position] = {}

    @property
    def mode(self) -> str:
        return "dry-run" if self.dry_run else self.broker.mode

    def _record(self, **kw) -> None:
        if self.journal:
            self.journal.record_order(mode=self.mode, **kw)

    def _with_inflight(self, account: AccountSnapshot) -> AccountSnapshot:
        new = {s: p for s, p in self.inflight.items() if s not in account.positions}
        return replace(account, positions={**account.positions, **new},
                       cash=account.cash - sum(p.market_value for p in new.values()),
                       trades_today=account.trades_today + len(new))

    def enter(self, sc: StrategyConfig, symbol: str, price: float, reason: str,
              client_order_id: str | None = None) -> EntryPlan:
        if self.kill_check():
            self.guardrails.kill_switch = True
        account = self._with_inflight(self.broker.snapshot())
        base = max(0.0, account.equity - self.reserved(account))  # the trading slice
        intent = plan_entry(sc, symbol, price, base, reason)
        decision = self.guardrails.check(intent, account)
        common = {"strategy": sc.name, "symbol": symbol, "side": "buy", "qty": intent.qty, "est_price": price,
                      "stop_price": intent.stop_loss_price, "take_profit_price": intent.take_profit_price,
                      "reason": intent.reason}
        if not decision:
            why = "; ".join(decision.reasons)
            if intent.qty < 1 and price > 0:  # say why in money, not "quantity must be positive"
                budget = base * sc.allocation_pct / 100
                slice_note = " (the trading slice; the invest plan's money is set aside)" if base < account.equity else ""
                why = (f"{sc.allocation_pct:g}% of ${base:,.0f}{slice_note} is ${budget:,.0f}, less than one share at "
                       f"${price:,.2f}. Raise the strategy's position size or add money to the account")
            log.warning("blocked %s %s: %s", symbol, sc.name, why)
            self._record(status="blocked", detail=why, **common)
            self.alerts.send(f"Blocked buy: {symbol}", f"{intent.reason}\nGuardrails: {why}",
                             Level.INFO, key=f"blocked:{symbol}:{why}")
            return EntryPlan(intent, decision, False)

        if self.dry_run:
            self._record(status="dry-run", **common)
            self.alerts.send(f"[dry run] Would buy {symbol}",
                             f"{intent.qty:g} @ ~${price:,.2f}, stop ${intent.stop_loss_price:,.2f}\n{intent.reason}")
            return EntryPlan(intent, decision, False)

        try:
            order = self.broker.submit_entry(intent, client_order_id)
        except Exception as e:
            # A retried request or a timeout can fail here although Alpaca took the order. Look it
            # up, so it's recorded and counted against the limits for the rest of this check.
            order = self._placed_anyway(client_order_id)
            if order is None:
                if client_order_id and "client_order_id" in str(e).lower():
                    log.info("%s %s: already placed (%s)", symbol, sc.name, client_order_id)
                    return EntryPlan(intent, decision, False)
                self._record(status="failed", detail=str(e), **common)
                self.alerts.send(f"Buy FAILED: {symbol}", str(e), Level.WARNING, key=f"buyfail:{symbol}:{e}")
                raise
            if self.journal and self.journal.has_order(str(order.id)):
                return EntryPlan(intent, decision, False)  # placed in an earlier check and recorded then
        self.inflight[symbol] = Position(symbol, intent.qty, intent.qty * price, price)
        self._record(status="submitted", broker_order_id=str(order.id), **common)
        self.alerts.send(f"[{self.mode}] Buying {symbol}",
                         f"{int(intent.qty)} @ ~${price:,.2f}, stop ${intent.stop_loss_price:,.2f} at Alpaca\n"
                         f"{intent.reason}", key=f"buy:{order.id}")
        return EntryPlan(intent, decision, True)

    def _placed_anyway(self, client_order_id: str | None):
        if not client_order_id or not hasattr(self.broker, "order_by_client_id"):
            return None
        try:
            return self.broker.order_by_client_id(client_order_id)
        except Exception:  # noqa: BLE001 - not found, or Alpaca unreachable: treat as not placed
            return None

    def exit(self, strategy: str, symbol: str, qty: float, price: float, reason: str) -> bool:
        # Exits are never blocked by guardrails.
        common = {"strategy": strategy, "symbol": symbol, "side": "sell", "qty": qty, "est_price": price, "reason": reason}
        if self.dry_run:
            self._record(status="dry-run", **common)
            self.alerts.send(f"[dry run] Would sell {symbol}", f"{qty:g} @ ~${price:,.2f}\n{reason}")
            return False
        try:
            order = self.broker.close_position(symbol, reason)
        except Exception as e:
            self._record(status="failed", detail=str(e), **common)
            self.alerts.send(f"Sell FAILED: {symbol}", f"{reason}\n{e}", Level.CRITICAL, key=f"sellfail:{symbol}")
            raise
        self._record(status="submitted", broker_order_id=str(getattr(order, "id", "")) or None, **common)
        self.alerts.send(f"[{self.mode}] Selling {symbol}", f"{qty:g} @ ~${price:,.2f}\n{reason}",
                         key=f"sell:{getattr(order, 'id', '') or symbol}")
        return True
