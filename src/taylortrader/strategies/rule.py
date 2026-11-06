"""A strategy written as two rules, no code: buy when one holds, sell when the other does.

Rules use the same small language as the AI strategy's filters (criteria.py):
`rsi < 50 and macd_diff > 0`, `prev_rsi < 30 and rsi >= 30`, `close > ma_50`, `close > prev_high_20`.
"""

from __future__ import annotations

from dataclasses import dataclass

from taylortrader import criteria
from taylortrader.strategies.base import NO, Bars, Signal, Strategy
from taylortrader.strategies.registry import register


@register
class RuleStrategy(Strategy):
    name = "rule"
    description = ("Your own rules, no code: buy when the buy rule holds, sell when the sell rule holds "
                   "(e.g. buy when 'rsi < 50 and macd_diff > 0', sell when 'rsi > 68'). The stop-loss still applies.")

    @dataclass(frozen=True)
    class Params:
        buy_when: str = "rsi < 50 and macd_diff > 0"
        sell_when: str = "rsi > 68 or macd_diff < 0"

    def __init__(self, params=None):
        super().__init__(params)
        try:
            self._buy = criteria.parse(self.p.buy_when)
            self._sell = criteria.parse(self.p.sell_when)
        except criteria.CriteriaError as e:
            raise ValueError(f"rule: {e}") from None
        if self._buy is None:
            raise ValueError("rule: buy_when can't be empty (it would buy on every bar)")

    @property
    def warmup(self) -> int:
        return max(35, criteria.longest(self._buy), criteria.longest(self._sell))

    def entry(self, b: Bars, i: int) -> Signal:
        if i < 1 or not criteria.evaluate(self._buy, *criteria.rows(b, i, self._buy)):
            return NO
        return Signal(True, f"rule: {self.p.buy_when}")

    def exit(self, b: Bars, i: int, entry_price: float) -> Signal:
        if self._sell is None or i < 1 or not criteria.evaluate(self._sell, *criteria.rows(b, i, self._sell)):
            return NO
        return Signal(True, f"rule: {self.p.sell_when}")
