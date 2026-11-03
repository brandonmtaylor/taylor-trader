"""Market data, cached locally as parquet.

Feeds:
  sip    Alpaca, every US exchange. Free plan: history only, the newest 15 minutes are held back.
  iex    Alpaca, one exchange (~2-3% of volume). Free and real time.
  yahoo  Yahoo Finance via the unofficial yfinance library, all exchanges, free and current.
         Unofficial: Yahoo can change or rate-limit it without notice, so the service pairs it
         with a fallback feed (FallbackData) and switches automatically when it fails or lags.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from taylortrader.secrets import AlpacaCredentials

log = logging.getLogger(__name__)

TIMEFRAMES = {
    "1Min": TimeFrame(1, TimeFrameUnit.Minute),
    "5Min": TimeFrame(5, TimeFrameUnit.Minute),
    "15Min": TimeFrame(15, TimeFrameUnit.Minute),
    "1Hour": TimeFrame(1, TimeFrameUnit.Hour),
    "1Day": TimeFrame(1, TimeFrameUnit.Day),
}
COLUMNS = ["open", "high", "low", "close", "volume"]
SIP_DELAY = timedelta(minutes=16)


def regular_hours(df: pd.DataFrame) -> pd.DataFrame:
    """Keep 9:30-16:00 New York time bars (intraday timeframes only)."""
    et = df.index.tz_convert("America/New_York")
    minutes = et.hour * 60 + et.minute
    return df[(minutes >= 9 * 60 + 30) & (minutes < 16 * 60)]


class MarketData:
    """Alpaca bars (feed "iex" or "sip")."""

    def __init__(self, creds: AlpacaCredentials, cache_dir: Path, feed: str = "iex"):
        self._client = StockHistoricalDataClient(
            creds.key_id.get_secret_value(), creds.secret_key.get_secret_value()
        )
        self.cache_dir = Path(cache_dir)
        self.feed = feed

    def _cache_path(self, symbol: str, timeframe: str) -> Path:
        return self.cache_dir / f"{symbol}_{timeframe}_{self.feed}.parquet"

    def _fetch(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> pd.DataFrame:
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TIMEFRAMES[timeframe],
            start=start,
            end=end,
            feed=DataFeed.SIP if self.feed == "sip" else DataFeed.IEX,
            adjustment=Adjustment.ALL,
        )
        df = self._client.get_stock_bars(req).df
        if df.empty:
            return pd.DataFrame(columns=COLUMNS)
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level="symbol")
        return df[COLUMNS].astype(float)

    def bars(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime | None = None,
        regular_session: bool = True,
    ) -> pd.DataFrame:
        if timeframe not in TIMEFRAMES:
            raise ValueError(f"timeframe must be one of {list(TIMEFRAMES)}")
        now = datetime.now(UTC)
        end = min(end or now, now - SIP_DELAY) if self.feed == "sip" else (end or now)

        path = self._cache_path(symbol, timeframe)
        cached = pd.read_parquet(path) if path.exists() else pd.DataFrame(columns=COLUMNS)

        if cached.empty or cached.index[0] > pd.Timestamp(start) + timedelta(days=4):
            log.info("fetching %s %s %s..%s", symbol, timeframe, start.date(), end.date())
            df = self._fetch(symbol, timeframe, start, end)
        else:
            tail_start = cached.index[-1].to_pydatetime()
            new = self._fetch(symbol, timeframe, tail_start, end) if tail_start < end else cached.iloc[:0]
            df = pd.concat([cached, new])
            df = df[~df.index.duplicated(keep="last")]

        df = df.sort_index()
        if not df.empty:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            df.to_parquet(path)

        df = df[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))]
        if regular_session and timeframe != "1Day" and not df.empty:
            df = regular_hours(df)
        return df


YAHOO_INTERVALS = {"1Min": "1m", "5Min": "5m", "15Min": "15m", "1Day": "1d"}
# Yahoo's hourly bars start at :30 while Alpaca's start on the hour, so a strategy would see
# different bars than it was backtested on. Hourly strategies use the fallback feed instead.


class YahooData:
    """Yahoo Finance bars. No cache: the service only asks for a short recent window."""

    feed = "yahoo"

    def __init__(self, timeout: float = 10.0):
        import yfinance  # imported here so Alpaca-only setups don't pay for it

        self._yf = yfinance
        self.timeout = timeout

    def supports(self, timeframe: str) -> bool:
        return timeframe in YAHOO_INTERVALS

    def bars(self, symbol: str, timeframe: str, start: datetime, end: datetime | None = None,
             regular_session: bool = True) -> pd.DataFrame:
        if not self.supports(timeframe):
            raise ValueError(f"yahoo feed doesn't serve {timeframe}")
        end = end or datetime.now(UTC)
        df = self._yf.Ticker(symbol).history(
            start=start, end=end + timedelta(minutes=1), interval=YAHOO_INTERVALS[timeframe],
            auto_adjust=True, prepost=False, timeout=self.timeout, raise_errors=True,
        )
        if df.empty:
            return pd.DataFrame(columns=COLUMNS)
        df = df.rename(columns=str.lower)[COLUMNS].astype(float).dropna(subset=["close"])
        df.index = df.index.tz_convert(UTC) if timeframe != "1Day" else df.index.tz_localize(None).tz_localize(UTC)
        df.index.name = "timestamp"
        if regular_session and timeframe != "1Day" and not df.empty:
            df = regular_hours(df)
        return df


class FallbackData:
    """Use the primary feed; switch to the fallback when it errors, is empty, or lags.

    "Lags" means the newest bar is more than two bars behind during market hours, which
    would make the strategy act on stale prices. Switches are logged once per change
    (not every tick) through on_switch, so the service log shows when and why.
    """

    def __init__(self, primary, fallback, on_switch=None, now=lambda: datetime.now(UTC)):
        self.primary, self.fallback = primary, fallback
        self.on_switch = on_switch or (lambda msg, level: None)
        self.now = now
        self.using = primary.feed
        self.feed = f"{primary.feed} (fallback {fallback.feed})"

    def _stale(self, df: pd.DataFrame, timeframe: str) -> str | None:
        if df.empty:
            return "returned no bars"
        if timeframe == "1Day":
            return None
        now = self.now()
        et = now.astimezone(ZoneInfo("America/New_York"))
        minutes = et.hour * 60 + et.minute
        if et.weekday() >= 5 or not (9 * 60 + 30 + 2 * BAR_MINUTES[timeframe] <= minutes < 16 * 60):
            return None
        age = now - df.index[-1].to_pydatetime()
        limit = timedelta(minutes=3 * BAR_MINUTES[timeframe])  # bar start + its length + one more bar
        return f"newest bar is {int(age.total_seconds() // 60)} min old" if age > limit else None

    def _switch(self, to: str, why: str) -> None:
        if to == self.using:
            return
        self.using = to
        if to == self.fallback.feed:
            self.on_switch(f"{self.primary.feed} data {why}; using {to} until it recovers", "warning")
        else:
            self.on_switch(f"{to} data is back; switched from {self.fallback.feed}", "info")

    def bars(self, symbol: str, timeframe: str, start: datetime, end: datetime | None = None,
             regular_session: bool = True) -> pd.DataFrame:
        if hasattr(self.primary, "supports") and not self.primary.supports(timeframe):
            return self.fallback.bars(symbol, timeframe, start, end, regular_session)
        try:
            df = self.primary.bars(symbol, timeframe, start, end, regular_session)
            why = self._stale(df, timeframe)
        except Exception as e:  # noqa: BLE001 - any primary failure means: use the fallback
            df, why = None, f"failed ({type(e).__name__}: {str(e)[:120]})"
        if why is None:
            self._switch(self.primary.feed, "")
            return df
        log.warning("%s %s %s: %s", self.primary.feed, symbol, timeframe, why)
        self._switch(self.fallback.feed, why)
        return self.fallback.bars(symbol, timeframe, start, end, regular_session)


BAR_MINUTES = {"1Min": 1, "5Min": 5, "15Min": 15, "1Hour": 60}
