"""Market data from Alpaca, cached locally as parquet.

Free plan: full SIP history except the most recent 15 minutes; IEX in real time.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
