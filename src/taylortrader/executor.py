"""The only path from a strategy signal to the broker. Sizing, stops, guardrails, journal, alerts."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from guardrails import Decision, Guardrails, OrderIntent, Side
from taylortrader.alerts import Alerts, Level
from taylortrader.config import StrategyConfig
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

    @property
    def mode(self) -> str:
        return "dry-run" if self.dry_run else self.broker.mode

    def _record(self, **kw) -> None:
        if self.journal:
            self.journal.record_order(mode=self.mode, **kw)

    def enter(self, sc: StrategyConfig, symbol: str, price: float, reason: str) -> EntryPlan:
        account = self.broker.snapshot()
        intent = plan_entry(sc, symbol, price, account.equity, reason)
        decision = self.guardrails.check(intent, account)
        common = {"strategy": sc.name, "symbol": symbol, "side": "buy", "qty": intent.qty, "est_price": price,
                      "stop_price": intent.stop_loss_price, "take_profit_price": intent.take_profit_price,
                      "reason": intent.reason}
        if not decision:
            why = "; ".join(decision.reasons)
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
            order = self.broker.submit_entry(intent)
        except Exception as e:
            self._record(status="failed", detail=str(e), **common)
            self.alerts.send(f"Buy FAILED: {symbol}", str(e), Level.WARNING)
            raise
        self._record(status="submitted", broker_order_id=str(order.id), **common)
        self.alerts.send(f"[{self.mode}] Buying {symbol}",
                         f"{int(intent.qty)} @ ~${price:,.2f}, stop ${intent.stop_loss_price:,.2f} at Alpaca\n"
                         f"{intent.reason}")
        return EntryPlan(intent, decision, True)

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
        self.alerts.send(f"[{self.mode}] Selling {symbol}", f"{qty:g} @ ~${price:,.2f}\n{reason}")
        return True
