"""The only path from a strategy signal to the broker. Sizing, stops and guardrails live here."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from guardrails import Decision, Guardrails, OrderIntent, Side
from taylortrader.config import StrategyConfig

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class EntryPlan:
    intent: OrderIntent
    decision: Decision


def plan_entry(
    sc: StrategyConfig, symbol: str, price: float, equity: float, reason: str
) -> OrderIntent:
    qty = int((equity * sc.allocation_pct / 100) // price)
    stop = price * (1 - sc.stop_loss_pct / 100)
    tp = price * (1 + sc.take_profit_pct / 100) if sc.take_profit_pct > 0 else None
    return OrderIntent(symbol, Side.BUY, qty, price, stop, tp, reason=f"{sc.name}: {reason}")


class Executor:
    def __init__(self, broker, guardrails: Guardrails):
        self.broker = broker
        self.guardrails = guardrails

    def enter(self, sc: StrategyConfig, symbol: str, price: float, reason: str) -> EntryPlan:
        account = self.broker.snapshot()
        intent = plan_entry(sc, symbol, price, account.equity, reason)
        decision = self.guardrails.check(intent, account)
        if decision:
            self.broker.submit_entry(intent)
        else:
            log.warning("blocked %s %s: %s", symbol, sc.name, "; ".join(decision.reasons))
        return EntryPlan(intent, decision)

    def exit(self, symbol: str, reason: str) -> None:
        # Exits are never blocked by guardrails.
        self.broker.close_position(symbol, reason)
