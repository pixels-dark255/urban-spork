"""
Market data provider interface.

The prediction engine must never import a broker SDK directly. It talks to
this interface; concrete providers (Yahoo today, Kotak Neo later, anything
else after that) implement it. Swapping providers is then a config change,
not a code change - which is the whole point of keeping the broker layer
separate from the prediction layer.

Contract for get_bars(): return a pandas DataFrame indexed by timestamp with
columns Open/High/Low/Close/Volume, oldest first, or an empty DataFrame if
the data is unavailable. Never raise for an ordinary data outage - return
empty and let the caller decide (usually: fall back to stored history).
"""
from __future__ import annotations

import pandas as pd

OHLCV_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]


class MarketDataProvider:
    name = "base"

    def is_available(self) -> bool:
        """Whether this provider is configured well enough to be used at all
        (e.g. credentials present). Checked at selection time, not per call."""
        return False

    def capabilities(self) -> dict:
        return {
            "quotes": False,
            "historical": False,
            "intraday": False,
            "streaming": False,
            "orders": False,
        }

    def get_bars(self, symbol: str, exchange: str, interval: str, period: str) -> pd.DataFrame:
        raise NotImplementedError

    def get_quote(self, symbol: str, exchange: str) -> float | None:
        raise NotImplementedError

    def status(self) -> dict:
        return {
            "name": self.name,
            "available": self.is_available(),
            "capabilities": self.capabilities(),
        }


def empty_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=OHLCV_COLUMNS)


def normalise_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Make any provider's frame conform to the contract: single-level
    OHLCV columns, sorted ascending, no all-NaN rows."""
    if df is None or len(df) == 0:
        return empty_frame()
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = df.columns.get_level_values(0)
    rename = {c: c.title() for c in df.columns if isinstance(c, str)}
    df = df.rename(columns=rename)
    missing = [c for c in ["Open", "High", "Low", "Close"] if c not in df.columns]
    if missing:
        return empty_frame()
    if "Volume" not in df.columns:
        df = df.assign(Volume=0.0)
    df = df[OHLCV_COLUMNS].dropna(how="all").sort_index()
    return df
