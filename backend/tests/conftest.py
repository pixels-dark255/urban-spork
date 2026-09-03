"""Test setup: point every store at a throwaway directory BEFORE the app
modules import config, so tests never touch real collected data."""
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="urban-spork-tests-")
os.environ["MARKET_DB_PATH"] = os.path.join(_TMP, "test.db")
os.environ["MODEL_DIR"] = os.path.join(_TMP, "models")
os.environ.setdefault("MARKET_DATA_PROVIDER", "yahoo")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pytest  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _init_db():
    import market_store
    market_store.init_db()
    yield


def make_bars(n=400, trend=0.0, noise=1.0, start=1000.0, freq="5min",
              volume_trend=False, seed=0, ranging=False) -> pd.DataFrame:
    """Synthetic OHLCV. Tests must not depend on a live market data source:
    it would make them fail at weekends, on a rate limit, or offline.

    `ranging=True` produces a genuinely range-bound series (mean-reverting
    around `start`). A plain zero-drift random walk is NOT range-bound - it
    happily wanders into a real trend, so it is the wrong fixture for
    asserting that the regime detector says "sideways"."""
    rng = np.random.default_rng(seed)
    end = pd.Timestamp("2026-09-02 15:25", tz="Asia/Kolkata")
    idx = pd.date_range(end=end, periods=n, freq=freq, tz="Asia/Kolkata")
    if ranging:
        deviation = np.zeros(n)
        for i in range(1, n):
            deviation[i] = deviation[i - 1] * 0.9 + rng.normal(0, noise)
        prices = start + deviation
    else:
        prices = start + np.cumsum(rng.normal(0, noise, n)) + np.linspace(0, trend, n)
    volume = (np.linspace(2000, 9000, n) if volume_trend
              else rng.integers(2000, 9000, n).astype(float))
    return pd.DataFrame({
        "Open": prices - 0.1,
        "High": prices + np.abs(rng.normal(0, 0.6, n)) + 0.4,
        "Low": prices - np.abs(rng.normal(0, 0.6, n)) - 0.4,
        "Close": prices,
        "Volume": volume,
    }, index=idx)


def store_bars(symbol, df, exchange="NSE", timeframe="5m"):
    import market_store
    rows = [{
        "ts": int(ts.timestamp()), "open": float(r["Open"]), "high": float(r["High"]),
        "low": float(r["Low"]), "close": float(r["Close"]), "volume": float(r["Volume"]),
    } for ts, r in df.iterrows()]
    return market_store.save_bars(symbol, exchange, timeframe, rows)


@pytest.fixture
def bars():
    return make_bars
