"""Alpaca broker adapter (alpaca-py). Paper unless the credentials say otherwise.

Entries always carry a stop-loss leg held at Alpaca (OTO, or bracket when there is also a
take-profit), with time-in-force GTC so the stop survives past today and survives this process dying.
Invest plan buys are the exception: dollar-amount (fractional) market orders with no stop, which
Alpaca only accepts as day orders.
"""

from __future__ import annotations

import logging
import math
import time
from datetime import UTC, datetime, timedelta

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, OrderType, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetOrdersRequest,
    GetPortfolioHistoryRequest,
    MarketOrderRequest,
    StopLossRequest,
    StopOrderRequest,
    TakeProfitRequest,
)

from guardrails import AccountSnapshot, OrderIntent, Position, Side
from taylortrader.secrets import AlpacaCredentials

log = logging.getLogger(__name__)

_STOP_TYPES = {OrderType.STOP, OrderType.STOP_LIMIT, OrderType.TRAILING_STOP}


class BrokerError(RuntimeError):
    pass


# alpaca-py sends requests with no timeout, so one stalled connection can freeze the service.
# (connect, read) seconds; a timed-out call raises and the next check tries again.
HTTP_TIMEOUT = (5.0, 30.0)


def with_timeout(client, timeout=HTTP_TIMEOUT):
    """Give an alpaca-py client a default request timeout (it has no option for one)."""
    session = getattr(client, "_session", None)
    if session is None:  # alpaca-py changed; better no timeout than not starting
        log.warning("couldn't set a request timeout on %s", type(client).__name__)
        return client
    send = session.request

    def request(method, url, **kwargs):
        kwargs.setdefault("timeout", timeout)
        return send(method, url, **kwargs)

    session.request = request
    return client


def _price(p: float) -> float:
    # Alpaca accepts 2 decimals at/above $1, 4 below.
    return round(p, 2) if p >= 1 else round(p, 4)


class AlpacaBroker:
    def __init__(self, creds: AlpacaCredentials):
        self.paper = creds.paper
        self._client = with_timeout(TradingClient(
            creds.key_id.get_secret_value(), creds.secret_key.get_secret_value(), paper=creds.paper
        ))

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

    def exit_levels(self) -> dict[str, dict]:
        """Each symbol's protective orders at the broker: {"stop": price, "target": price}.
        stop is the highest stop-loss; target is a take-profit (a limit sell that's a bracket leg).
        A plain limit sell placed on its own is a sale in progress, not a target, so it's left out."""
        out: dict[str, dict] = {}
        orders = self._client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, nested=True))
        for top in orders:
            for o, is_leg in [(top, False), *((leg, True) for leg in (top.legs or []))]:
                if o.side != OrderSide.SELL:
                    continue
                lv = out.setdefault(o.symbol, {"stop": None, "target": None})
                if o.order_type in _STOP_TYPES and o.stop_price is not None:
                    lv["stop"] = max(lv["stop"] or 0.0, float(o.stop_price))
                elif o.order_type == OrderType.LIMIT and is_leg and o.limit_price is not None:
                    lv["target"] = float(o.limit_price)
        return {s: lv for s, lv in out.items() if lv["stop"] is not None or lv["target"] is not None}

    def symbols_selling(self) -> set[str]:
        """Symbols with a sell waiting to fill (a close, not a protective stop or bracket leg),
        e.g. one sent while the market is closed."""
        orders = self._client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, nested=True))
        return {o.symbol for o in orders
                if o.side == OrderSide.SELL and o.order_type in (OrderType.MARKET, OrderType.LIMIT)}

    def trades_today(self) -> int:
        start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        orders = self._client.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.CLOSED, after=start, limit=500)
        )
        return sum(1 for o in orders if o.filled_at is not None)

    def portfolio_history(self, period: str = "1M") -> list[tuple[int, float]]:
        """Daily account equity as (unix seconds, equity), skipping days before the account had money."""
        h = self._client.get_portfolio_history(GetPortfolioHistoryRequest(period=period, timeframe="1D"))
        return [(int(t), float(e)) for t, e in zip(h.timestamp or [], h.equity or []) if e]

    def fills(self, days: int = 365) -> list:
        """Filled buys and sells (stop-loss legs included), for the results panel."""
        from taylortrader.stats import Fill

        after = datetime.now(UTC) - timedelta(days=days)
        orders = self._client.get_orders(GetOrdersRequest(status=QueryOrderStatus.CLOSED, after=after,
                                                          limit=500, nested=True))
        out = []
        for o in orders:
            for x in [o, *(o.legs or [])]:
                qty = float(x.filled_qty or 0)
                if x.filled_at is None or qty <= 0 or x.filled_avg_price is None:
                    continue
                side = str(getattr(x.side, "value", x.side))
                out.append(Fill(x.filled_at, x.symbol, side, qty, float(x.filled_avg_price), str(x.id)))
        return out

    def get_order(self, order_id: str):
        return self._client.get_order_by_id(order_id)

    def account_info(self) -> dict:
        """Who this key pair belongs to, for the dashboard's connection test. No secrets."""
        a = self._client.get_account()
        number = str(a.account_number or "")
        return {"account": "…" + number[-4:] if number else None, "status": str(a.status.value if hasattr(a.status, "value") else a.status),
                "equity": float(a.equity), "paper": self.paper}

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

    def submit_notional_buy(self, symbol: str, notional: float, client_order_id: str):
        """An invest plan buy: $notional of symbol, fractional shares, no stop. Only reached through
        InvestRunner, which runs the investing guardrails first. Alpaca rejects a repeated
        client_order_id, which is what makes a plan buy impossible to place twice."""
        amount = math.floor(notional * 100) / 100  # Alpaca takes whole cents
        if amount < 1:
            raise BrokerError(f"{symbol}: ${notional:.2f} is under the $1 minimum")
        req = MarketOrderRequest(symbol=symbol, notional=amount, side=OrderSide.BUY,
                                 time_in_force=TimeInForce.DAY, client_order_id=client_order_id)
        log.info("[%s] INVEST BUY %s $%.2f (%s)", self.mode, symbol, amount, client_order_id)
        return self._client.submit_order(req)

    def order_by_client_id(self, client_order_id: str):
        """The order placed with this client order id, or None if there isn't one."""
        from alpaca.common.exceptions import APIError

        try:
            return self._client.get_order_by_client_id(client_order_id)
        except APIError as e:
            if getattr(e, "status_code", None) == 404 or "not found" in str(e).lower():
                return None
            raise

    def asset_status(self, symbol: str) -> tuple[bool, bool]:
        """(tradable, fractionable) at Alpaca."""
        a = self._client.get_asset(symbol)
        return bool(a.tradable), bool(a.fractionable)

    def place_stop(self, symbol: str, qty: float, stop_price: float):
        """Standalone GTC stop for an existing long position (used to repair missing protection)."""
        whole = math.floor(qty)
        if whole < 1:
            raise BrokerError(f"{symbol}: can't place a stop on a fractional position ({qty})")
        req = StopOrderRequest(symbol=symbol, qty=whole, side=OrderSide.SELL,
                               time_in_force=TimeInForce.GTC, stop_price=_price(stop_price))
        log.info("[%s] STOP %s x%d at %.2f", self.mode, symbol, whole, req.stop_price)
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

    def cancel_all_orders(self, wait: float = 5.0) -> None:
        """Cancel every open order in the account, waiting briefly until Alpaca confirms.

        Used by "sell everything" *before* the sells: run after them, it would also cancel sells
        that are queued for the next open while the market is closed.
        """
        log.info("[%s] CANCEL all open orders", self.mode)
        self._client.cancel_orders()
        deadline = time.monotonic() + wait
        while self.open_orders() and time.monotonic() < deadline:
            time.sleep(0.25)
