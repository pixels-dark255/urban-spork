"""Items 11-14: caching, store reads, EOD square-off and the market calendar."""
import datetime as dt

import pytest

import intraday
import market_calendar
import storage
import timeutil


# --- item 11: response caching ---------------------------------------------

def test_repeated_downloads_hit_the_cache(fake_yf):
    import data_sources

    for _ in range(5):
        data_sources._yf_download_cached("TEST.NS", "5d", "5m")
    assert len(fake_yf.calls) == 1, f"expected 1 network call, got {len(fake_yf.calls)}"


def test_different_keys_are_cached_separately(fake_yf):
    import data_sources

    data_sources._yf_download_cached("TEST.NS", "5d", "5m")
    data_sources._yf_download_cached("TEST.NS", "1mo", "15m")
    data_sources._yf_download_cached("OTHER.NS", "5d", "5m")
    assert len(fake_yf.calls) == 3


def test_empty_frames_are_never_cached(fake_yf, monkeypatch):
    """Caching an empty frame would turn a transient Yahoo blip into minutes
    of a blank screen."""
    import pandas as pd
    import data_sources

    monkeypatch.setattr(data_sources, "_yf_download",
                        lambda *a: fake_yf.calls.append(a) or pd.DataFrame())
    for _ in range(3):
        data_sources._yf_download_cached("EMPTY.NS", "5d", "5m")
    assert len(fake_yf.calls) == 3


def test_cached_frames_are_copies(fake_yf):
    """A caller mutating its result must not corrupt the cache for everyone."""
    import data_sources

    first = data_sources._yf_download_cached("TEST.NS", "5d", "5m")
    first.loc[first.index[0], "Close"] = -999.0
    second = data_sources._yf_download_cached("TEST.NS", "5d", "5m")
    assert second["Close"].iloc[0] != -999.0


def test_cache_is_size_bounded(fake_yf):
    import data_sources

    limit = data_sources._PRICE_CACHE_MAX_ENTRIES
    for i in range(limit + 20):
        data_sources._yf_download_cached(f"SYM{i}.NS", "5d", "5m")
    assert len(data_sources._price_cache) <= limit


def test_news_is_cached(monkeypatch, fake_yf):
    import data_sources

    calls = []
    monkeypatch.setattr(data_sources, "_fetch_company_news_uncached",
                        lambda name, n=15: calls.append(name) or [{"title": "x"}])
    for _ in range(4):
        data_sources.fetch_company_news("RELIANCE")
    assert len(calls) == 1


def test_weather_is_cached(monkeypatch, fake_yf):
    import data_sources

    calls = []
    monkeypatch.setattr(data_sources, "_fetch_weather_signal_uncached",
                        lambda lat=28.6139, lon=77.2090: calls.append(1) or {"current": {}})
    for _ in range(4):
        data_sources.fetch_weather_signal()
    assert len(calls) == 1


# --- item 12: one store read per request ------------------------------------

def test_intraday_bucket_is_a_single_load(isolated_storage, monkeypatch):
    storage.add_intraday_stock("1.1.1.1", "A.NS", "A")
    storage.add_intraday_stock("1.1.1.1", "B.NS", "B")

    loads = []
    original = storage._intraday_load
    monkeypatch.setattr(storage, "_intraday_load",
                        lambda: loads.append(1) or original())

    bucket = storage.get_intraday_bucket("1.1.1.1")
    assert len(loads) == 1
    assert len(bucket["stocks"]) == 2
    # Every portfolio for both stocks came from that one load.
    assert len(bucket["portfolios"]) == 2 * len(intraday.TIMEFRAMES)


def test_intraday_list_endpoint_reads_the_store_once(client, monkeypatch):
    import storage as storage_module

    client.post("/api/intraday/stocks", json={"symbol": "A", "exchange": "NSE"})
    client.post("/api/intraday/stocks", json={"symbol": "B", "exchange": "NSE"})

    loads = []
    original = storage_module._intraday_load
    monkeypatch.setattr(storage_module, "_intraday_load",
                        lambda: loads.append(1) or original())

    response = client.get("/api/intraday/stocks")
    assert response.status_code == 200
    # Was 1 + 3N (7 for two stocks); must now be a single read.
    assert len(loads) == 1, f"expected 1 store load, got {len(loads)}"


def test_unknown_ip_gets_a_blank_bucket(isolated_storage):
    bucket = storage.get_intraday_bucket("0.0.0.0")
    assert bucket["stocks"] == [] and bucket["portfolios"] == {}


# --- item 13: EOD square-off ------------------------------------------------

@pytest.mark.parametrize("hour,minute,expected", [
    (9, 30, False), (13, 0, False), (15, 14, False),
    (15, 15, True), (15, 29, True),
])
def test_square_off_window(hour, minute, expected):
    moment = dt.datetime(2026, 10, 2, hour, minute, tzinfo=timeutil.IST)
    assert intraday.is_square_off_time(moment) is expected


def _open_portfolio(entry=100.0, qty=10):
    return {
        "cash": 50000.0,
        "position": {"qty": qty, "entry_price": entry,
                     "entry_at": timeutil.iso_now(), "timeframe": "5m"},
        "trade_log": [],
    }


def _signal(price, score):
    return {"last_close": price, "fast_ma": price, "slow_ma": price, "rsi": 50.0,
            "vwap": price, "orb_high": price, "orb_low": price, "score": score}


def test_open_position_is_force_exited_at_square_off(monkeypatch):
    """An 'intraday' trade held overnight takes gap risk the strategy never
    measured, and a real broker would auto-square-off an MIS position."""
    monkeypatch.setattr(intraday, "is_square_off_time", lambda now=None: True)
    portfolio = _open_portfolio()
    # A strongly bullish score would normally keep the position open.
    result = intraday.step(portfolio, _signal(101.0, 4), "5m")
    assert result["position"] is None
    assert result["trade_log"][-1]["exit_reason"] == "eod_square_off"


def test_no_new_positions_are_opened_at_square_off(monkeypatch):
    monkeypatch.setattr(intraday, "is_square_off_time", lambda now=None: True)
    flat = {"cash": 50000.0, "position": None, "trade_log": []}
    result = intraday.step(flat, _signal(100.0, 4), "5m")
    assert result["position"] is None
    assert result["trade_log"] == []


def test_positions_open_normally_during_the_session(monkeypatch):
    monkeypatch.setattr(intraday, "is_square_off_time", lambda now=None: False)
    flat = {"cash": 50000.0, "position": None, "trade_log": []}
    result = intraday.step(flat, _signal(100.0, 4), "5m")
    assert result["position"] is not None


def test_square_off_takes_precedence_over_other_exit_reasons(monkeypatch):
    """At 15:15 a position that is also past its target closes for EOD -
    the session ending is why it closed."""
    monkeypatch.setattr(intraday, "is_square_off_time", lambda now=None: True)
    portfolio = _open_portfolio(entry=100.0)
    result = intraday.step(portfolio, _signal(105.0, 4), "5m")
    assert result["trade_log"][-1]["exit_reason"] == "eod_square_off"


def test_normal_exits_still_report_their_own_reason(monkeypatch):
    monkeypatch.setattr(intraday, "is_square_off_time", lambda now=None: False)
    portfolio = _open_portfolio(entry=100.0)
    result = intraday.step(portfolio, _signal(103.0, 4), "5m")
    assert result["trade_log"][-1]["exit_reason"] == "target"


# --- item 14: market calendar ----------------------------------------------

def test_weekends_are_closed():
    saturday = dt.datetime(2026, 10, 3, 11, 0, tzinfo=timeutil.IST)
    sunday = dt.datetime(2026, 10, 4, 11, 0, tzinfo=timeutil.IST)
    assert not market_calendar.is_market_open(saturday)
    assert not market_calendar.is_market_open(sunday)


def test_weekday_session_hours():
    # 2026-10-01 (Thu). NOT the 2nd - that is Gandhi Jayanti, a real
    # trading holiday now that the calendar is populated.
    weekday = dt.datetime(2026, 10, 1, 11, 0, tzinfo=timeutil.IST)
    assert market_calendar.is_market_open(weekday)
    assert not market_calendar.is_market_open(weekday.replace(hour=8))
    assert not market_calendar.is_market_open(weekday.replace(hour=16))


def test_market_open_is_evaluated_in_ist_not_server_local_time():
    """Same instant expressed in UTC must give the same answer."""
    ist_noon = dt.datetime(2026, 10, 1, 12, 0, tzinfo=timeutil.IST)
    assert market_calendar.is_market_open(ist_noon)
    assert market_calendar.is_market_open(ist_noon.astimezone(dt.timezone.utc))


def test_a_configured_holiday_closes_the_market(monkeypatch):
    holiday = dt.date(2026, 10, 2)
    monkeypatch.setattr(market_calendar, "_holidays",
                        {holiday.isoformat(): "Mahatma Gandhi Jayanti"})
    monkeypatch.setattr(market_calendar, "_years_loaded", {"2026"})
    moment = dt.datetime(2026, 10, 2, 11, 0, tzinfo=timeutil.IST)
    assert not market_calendar.is_market_open(moment)
    status = market_calendar.market_status(moment)
    assert status["reason"] == "holiday"
    assert status["holiday_name"] == "Mahatma Gandhi Jayanti"


def test_status_reports_honestly_when_holidays_are_unknown():
    """The shipped list is empty on purpose; the app must say so rather than
    imply every weekday is a trading day."""
    status = market_calendar.market_status()
    assert "holidays_known_for_year" in status
    assert status["holidays_known_for_year"] is market_calendar.holidays_loaded_for(
        market_calendar.now_ist().year)


def test_next_trading_day_skips_the_weekend():
    friday = dt.date(2026, 10, 2)
    assert market_calendar.next_trading_day(friday) == dt.date(2026, 10, 5)


def test_scheduler_uses_the_shared_calendar(monkeypatch):
    import scheduler
    monkeypatch.setattr(market_calendar, "is_market_open", lambda now=None: False)
    assert scheduler.is_market_hours() is False
    monkeypatch.setattr(market_calendar, "is_market_open", lambda now=None: True)
    assert scheduler.is_market_hours() is True


def test_market_status_endpoint(client):
    body = client.get("/api/market-status").json()
    for key in ("is_open", "reason", "session", "next_trading_day",
                "holidays_known_for_year"):
        assert key in body
    assert body["session"]["timezone"] == "Asia/Kolkata"
