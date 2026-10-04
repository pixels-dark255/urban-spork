"""Shared test fixtures.

Everything here exists so the suite never touches Yahoo. Tests monkeypatch
``data_sources._yf_download`` with synthetic OHLCV frames carrying a
tz-aware Asia/Kolkata index - the same shape yfinance returns for Indian
intraday data - so the suite passes offline, at weekends, and under a rate
limit, and so timezone behaviour is actually exercised rather than assumed.
"""
import datetime as dt
import os
import sys
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

IST = "Asia/Kolkata"


def recent_session_date() -> str:
    """Today if it is a weekday, else the previous Friday.

    Deliberately relative to now rather than a fixed date: price_at_time only
    consults intraday bars inside Yahoo's ~55-day window, so a hardcoded date
    silently falls out of that window as time passes and starts exercising a
    different code path than the test intends.
    """
    day = dt.date.today()
    while day.weekday() >= 5:
        day -= dt.timedelta(days=1)
    return day.isoformat()


def make_frame(n: int = 60, start: float = 100.0, step: float = 0.5,
               freq: str = "5min", session_date: str | None = None,
               open_time: str = "09:15", volume: float = 1000.0,
               prices: list[float] | None = None) -> pd.DataFrame:
    """Synthetic OHLCV with a tz-aware IST index, stamped inside a real NSE
    session so market-hours and session-boundary logic get exercised."""
    if prices is not None:
        n = len(prices)
        closes = np.asarray(prices, dtype=float)
    else:
        closes = start + np.arange(n, dtype=float) * step

    begin = pd.Timestamp(f"{session_date or recent_session_date()} {open_time}", tz=IST)
    index = pd.date_range(start=begin, periods=n, freq=freq, tz=IST)
    return pd.DataFrame(
        {
            "Open": closes,
            "High": closes + 0.4,
            "Low": closes - 0.4,
            "Close": closes,
            "Volume": np.full(n, volume, dtype=float),
        },
        index=index,
    )


def make_daily_frame(n: int = 300, start: float = 100.0, step: float = 0.2,
                     end_date: str | None = None) -> pd.DataFrame:
    """Daily bars stamped at IST midnight, the way Yahoo returns them for
    .NS symbols - the exact case that used to render as the previous date."""
    closes = start + np.arange(n, dtype=float) * step
    index = pd.date_range(end=pd.Timestamp(end_date or recent_session_date(), tz=IST),
                          periods=n, freq="D", tz=IST)
    return pd.DataFrame(
        {
            "Open": closes,
            "High": closes + 1.0,
            "Low": closes - 1.0,
            "Close": closes,
            "Volume": np.full(n, 5000.0),
        },
        index=index,
    )


@pytest.fixture
def frames():
    """Factory namespace so tests read declaratively."""
    return type("Frames", (), {"intraday": staticmethod(make_frame),
                               "daily": staticmethod(make_daily_frame)})


@pytest.fixture
def fake_yf(monkeypatch):
    """Replace the single network chokepoint.

    Every price fetch in the app funnels through data_sources._yf_download,
    so patching it is enough to make the whole stack deterministic. Returns a
    recorder so tests can assert on call counts (used by the cache tests).
    """
    import data_sources

    calls: list[tuple] = []
    responses: dict = {}
    default = {"frame": None}

    def fake_download(symbol, period, interval):
        calls.append((symbol, period, interval))
        if (symbol, period, interval) in responses:
            return responses[(symbol, period, interval)].copy()
        if interval in ("1d", "1wk"):
            return make_daily_frame().copy()
        if default["frame"] is not None:
            return default["frame"].copy()
        return make_frame().copy()

    monkeypatch.setattr(data_sources, "_yf_download", fake_download)
    data_sources.clear_caches()

    # A plain namespace rather than a class: inside a class body `calls =
    # calls` resolves the name in class scope, not the enclosing function's,
    # and raises NameError.
    handle = SimpleNamespace(
        calls=calls,
        set=lambda symbol, period, interval, frame: responses.__setitem__(
            (symbol, period, interval), frame),
        set_default=lambda frame: default.__setitem__("frame", frame),
        reset=calls.clear,
        clear_cache=data_sources.clear_caches,
    )

    yield handle
    data_sources.clear_caches()


@pytest.fixture
def isolated_storage(monkeypatch, tmp_path):
    """Point both JSON stores at a temp dir so tests never read or write the
    developer's real watchlist."""
    import storage

    monkeypatch.setattr(storage, "STORE_PATH", str(tmp_path / "watchlists.json"))
    monkeypatch.setattr(storage, "INTRADAY_STORE_PATH", str(tmp_path / "intraday.json"))
    monkeypatch.setattr(storage, "_pg_pool", None)
    return tmp_path


@pytest.fixture(autouse=True)
def no_outbound_calls(monkeypatch):
    """Stub every remaining network edge, at the module that owns it.

    Patching main's re-exports is not enough: backtest, scheduler and the
    search endpoint call data_sources directly, so a watchlist POST was
    still reaching news.google.com, api.open-meteo.com and api.bseindia.com
    for real. A test suite that depends on the internet fails at weekends,
    offline and under a rate limit.
    """
    import data_sources

    # Only the uncached layer is stubbed. The cached wrappers stay real so
    # the caching behaviour itself is still under test, and they cannot
    # reach the network because the thing they call is stubbed.
    monkeypatch.setattr(data_sources, "_fetch_company_news_uncached", lambda *a, **k: [])
    monkeypatch.setattr(data_sources, "_fetch_weather_signal_uncached", lambda *a, **k: {})
    monkeypatch.setattr(data_sources, "fetch_nse_list", lambda *a, **k: [])
    monkeypatch.setattr(data_sources, "fetch_bse_list", lambda *a, **k: [])
    monkeypatch.setattr(
        data_sources, "get_stock_universe",
        lambda *a, **k: [
            {"symbol": "RELIANCE", "name": "Reliance Industries Ltd", "exchange": "NSE"},
            {"symbol": "TCS", "name": "Tata Consultancy Services Ltd", "exchange": "NSE"},
            {"symbol": "INFY", "name": "Infosys Ltd", "exchange": "NSE"},
            {"symbol": "WIPRO", "name": "Wipro Ltd", "exchange": "NSE"},
        ],
    )


@pytest.fixture
def client(fake_yf, isolated_storage, monkeypatch):
    """TestClient with the scheduler stubbed - a test suite must not start
    background jobs."""
    from fastapi.testclient import TestClient

    import scheduler
    monkeypatch.setattr(scheduler, "start_scheduler", lambda *a, **k: None)
    import main
    monkeypatch.setattr(main, "start_scheduler", lambda *a, **k: None)

    with TestClient(main.app) as c:
        yield c


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)
