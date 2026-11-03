"""Alpaca broker adapter (alpaca-py). Paper unless the credentials say otherwise.

Entries always carry a stop-loss leg held at Alpaca (OTO, or bracket when there is also a
take-profit), with time-in-force GTC so the stop survives past today and survives this process dying.
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, OrderType, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetOrdersRequest,
    MarketOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)

from guardrails import AccountSnapshot, OrderIntent, Position, Side
from taylortrader.secrets import AlpacaCredentials

log = logging.getLogger(__name__)

_STOP_TYPES = {OrderType.STOP, OrderType.STOP_LIMIT, OrderType.TRAILING_STOP}


class BrokerError(RuntimeError):
    pass


def _price(p: float) -> float:
    # Alpaca accepts 2 decimals at/above $1, 4 below.
    return round(p, 2) if p >= 1 else round(p, 4)


class AlpacaBroker:
    def __init__(self, creds: AlpacaCredentials):
        self.paper = creds.paper
        self._client = TradingClient(
            creds.key_id.get_secret_value(), creds.secret_key.get_secret_value(), paper=creds.paper
        )

    @property
    def mode(self) -> str:
        return "paper" if self.paper else "live"

    # --- reads ---------------------------------------------------------------------------------

    def clock(self):
        return self._client.get_clock()

    def positions(self) -> dict[str, Position]:
        out = {}
        for p in self._client.get_all_positions():
            out[p.symbol] = Position(
                symbol=p.symbol,
                qty=float(p.qty),
                market_value=float(p.market_value or 0),
                avg_entry_price=float(p.avg_entry_price),
            )
        return out

    def open_orders(self) -> list:
        orders = self._client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, nested=True))
        flat = []
        for o in orders:
            flat.append(o)
            flat.extend(o.legs or [])
        return flat

    def symbols_with_stop(self) -> set[str]:
        return {
            o.symbol
            for o in self.open_orders()
            if o.side == OrderSide.SELL and o.order_type in _STOP_TYPES
        }

    def trades_today(self) -> int:
        start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        orders = self._client.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.CLOSED, after=start, limit=500)
        )
        return sum(1 for o in orders if o.filled_at is not None)

    def snapshot(self) -> AccountSnapshot:
        a = self._client.get_account()
        return AccountSnapshot(
            equity=float(a.equity),
            cash=float(a.cash),
            # last_equity = equity at the previous trading day's close
            day_start_equity=float(a.last_equity or a.equity),
            positions=self.positions(),
            trades_today=self.trades_today(),
        )

    # --- writes (only reached through Executor, which runs guardrails first) --------------------

    def submit_entry(self, intent: OrderIntent):
        if intent.side is not Side.BUY:
            raise BrokerError("submit_entry only opens long positions")
        if intent.stop_loss_price is None:
            raise BrokerError("refusing an entry without a stop-loss")
        qty = math.floor(intent.qty)  # stop legs require whole shares
        if qty < 1:
            raise BrokerError(f"{intent.symbol}: position size rounds to 0 whole shares")

        stop = StopLossRequest(stop_price=_price(intent.stop_loss_price))
        if intent.take_profit_price:
            order_class = OrderClass.BRACKET
            tp = TakeProfitRequest(limit_price=_price(intent.take_profit_price))
        else:
            order_class = OrderClass.OTO
            tp = None
        req = MarketOrderRequest(
            symbol=intent.symbol,
            qty=qty,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.GTC,
            order_class=order_class,
            stop_loss=stop,
            take_profit=tp,
        )
        log.info("[%s] BUY %s x%d, stop %.2f (%s)", self.mode, intent.symbol, qty, stop.stop_price, intent.reason)
        return self._client.submit_order(req)

    def close_position(self, symbol: str, reason: str = ""):
        # Cancel this symbol's open orders (including stop legs) first, or the close is rejected
        # because the shares are held by the stop order.
        for o in self.open_orders():
            if o.symbol == symbol and o.status not in ("filled", "canceled"):
                try:
                    self._client.cancel_order_by_id(o.id)
                except Exception as e:  # noqa: BLE001  leg may already be gone; never block an exit
                    log.warning("cancel %s order %s failed: %s", symbol, o.id, e)
        log.info("[%s] SELL all %s (%s)", self.mode, symbol, reason)
        return self._client.close_position(symbol)
