import pandas as pd
import pytest

from conftest import make_bars, store_bars
import market_data
import market_store
import universe


@pytest.fixture(scope="module", autouse=True)
def seeded():
    universe.ensure_seeded()
    universe._invalidate_index()


def test_seed_makes_search_work_with_no_network():
    assert market_store.stock_master_count() >= 100


def test_partial_symbol_search():
    assert "RELIANCE" in [r["symbol"] for r in universe.search("reli", limit=5)]


def test_fuzzy_search_tolerates_a_typo():
    assert "RELIANCE" in [r["symbol"] for r in universe.search("relaince", limit=5)]


def test_company_name_search():
    results = [r["symbol"] for r in universe.search("tata consultancy", limit=5)]
    assert "TCS" in results


def test_exact_ticker_ranks_first():
    assert universe.search("tcs", limit=5)[0]["symbol"] == "TCS"


def test_search_returns_sector_metadata():
    result = universe.search("reliance", limit=1)[0]
    assert result["sector"] and result["exchange"] == "NSE"


def test_empty_query_returns_nothing():
    assert universe.search("") == []


def test_nonsense_query_returns_nothing():
    assert universe.search("zzzzqqqqxxxx") == []


def test_resample_rolls_five_minute_bars_into_ten():
    df = make_bars(n=60, freq="5min", seed=9)
    resampled = market_data.resample_bars(df, 10)
    assert len(resampled) == pytest.approx(len(df) / 2, abs=1)
    first = df.iloc[:2]
    assert resampled["Open"].iloc[0] == pytest.approx(first["Open"].iloc[0])
    assert resampled["Close"].iloc[0] == pytest.approx(first["Close"].iloc[-1])
    assert resampled["High"].iloc[0] == pytest.approx(first["High"].max())
    assert resampled["Volume"].iloc[0] == pytest.approx(first["Volume"].sum())


def test_resampling_never_merges_two_sessions():
    day1 = make_bars(n=20, freq="5min", seed=1)
    day2 = day1.copy()
    day2.index = day2.index + pd.Timedelta(days=1)
    combined = pd.concat([day1, day2])
    resampled = market_data.resample_bars(combined, 30)
    dates = {ts.date() for ts in resampled.index}
    assert len(dates) == 2


def test_every_spec_timeframe_is_supported():
    for label in ["30s", "1m", "2m", "5m", "10m", "15m", "30m", "1h", "1.5h", "2h"]:
        assert label in market_data.TIMEFRAMES


def test_thirty_second_timeframe_is_flagged_as_approximated():
    tf = market_data.TIMEFRAMES["30s"]
    assert tf.approximated and tf.base_interval == "1m" and tf.note


def test_bars_persist_and_reload_identically():
    df = make_bars(n=50, seed=11)
    store_bars("PERSIST", df, timeframe="5m")
    rows = market_store.load_bars("PERSIST", "NSE", "5m", limit=100)
    assert len(rows) == 50
    assert rows[-1]["close"] == pytest.approx(float(df["Close"].iloc[-1]), abs=1e-6)


def test_saving_the_same_window_twice_does_not_duplicate():
    df = make_bars(n=30, seed=12)
    store_bars("IDEMPOTENT", df)
    store_bars("IDEMPOTENT", df)
    assert len(market_store.load_bars("IDEMPOTENT", "NSE", "5m", limit=200)) == 30


def test_get_bars_falls_back_to_stored_history_when_provider_is_down(monkeypatch):
    """The market being shut, or Yahoo being unreachable, must not empty the
    app - that is the entire point of the local store."""
    df = make_bars(n=120, seed=13)
    store_bars("FALLBACK", df, timeframe="5m")

    class DeadProvider:
        name = "dead"
        def get_bars(self, *a, **k):
            raise ConnectionError("provider unreachable")
        def get_quote(self, *a, **k):
            raise ConnectionError("provider unreachable")

    monkeypatch.setattr("providers.get_provider", lambda *a, **k: DeadProvider())
    out = market_data.get_bars("FALLBACK", "NSE", "5m")
    assert len(out) == 120
    assert market_data.get_quote("FALLBACK", "NSE") == pytest.approx(
        round(float(df["Close"].iloc[-1]), 2))
