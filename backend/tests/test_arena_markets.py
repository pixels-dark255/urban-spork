"""Global markets: sessions, windows, currencies, costs, separate arenas.

Synthetic bars only - no network. The sessions these tests assert are the
ones most likely to be quietly wrong: a US session seen from India moves by
an hour twice a year, forex shuts for 48 hours in the middle of a week that
otherwise looks continuous, and a window the user sets at 22:00 does not end
on the date it started. None of that can be checked by looking at the app in
October.
"""
import datetime as dt
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from arena import engine, markets, store
from arena import service as arena_service
from arena.strategies import enrich

IST = ZoneInfo("Asia/Kolkata")
NY = ZoneInfo("America/New_York")


def span(market_id, date, window=None):
    market = markets.get(market_id)
    return markets.active_session(market, window or market.default_window(),
                                  dt.date.fromisoformat(date))


def ist_hhmm(moment):
    return moment.astimezone(IST).strftime("%H:%M")


def bars_for(market_id, dates, window=None, lo=100.0, hi=104.0, volume=10_000.0,
             high_mult=1.002, low_mult=0.998):
    """5-minute bars covering each date's whole active session."""
    market = markets.get(market_id)
    window = window or market.default_window()
    frames = []
    for date in dates:
        bounds = markets.active_session(market, window, dt.date.fromisoformat(date))
        assert bounds, f"{market_id} has no session on {date}"
        start, end = bounds
        idx = pd.date_range(start.astimezone(dt.timezone.utc),
                            (end - dt.timedelta(minutes=5)).astimezone(dt.timezone.utc),
                            freq="5min")
        px = np.linspace(lo, hi, len(idx))
        frames.append(pd.DataFrame(
            {"Open": px, "High": px * high_mult, "Low": px * low_mult,
             "Close": px, "Volume": np.full(len(idx), volume)}, index=idx))
    return pd.concat(frames)


def run_sessions(market_id, symbols, dates, fx=None, window=None, capital=10_000.0,
                 volume=10_000.0, strategies=None):
    market = markets.get(market_id)
    window = window or market.default_window()
    cfg = {"market": market_id, "symbols": symbols, "daily_capital": capital,
           "window": window}
    if strategies:
        cfg["strategies"] = strategies
    state = engine.new_state(cfg)
    enriched = {}
    for sym in state["config"]["symbols"]:
        scale = ((fx or {}).get("symbol_scale") or {}).get(sym) or 1.0
        enriched[sym] = enrich(bars_for(market_id, dates, window, volume=volume),
                               market=market, window=window, scale=float(scale))
    for date in dates:
        engine.start_day(state, date, fx=fx)
        engine.process_bars(state, enriched, now=None)
        engine.settle_day(state)
    return state, enriched


# ---------------------------------------------------------------- sessions

def test_the_us_session_moves_by_an_hour_when_us_daylight_time_ends():
    """The headline timezone trap: 09:30-16:00 New York is 19:00-01:30 IST
    in October and 20:00-02:30 IST in November, because the US changes
    clocks and India does not. A fixed offset is wrong for four months."""
    october = span("us", "2026-10-15")
    assert (ist_hhmm(october[0]), ist_hhmm(october[1])) == ("19:00", "01:30")

    # US daylight time ends on the first Sunday in November - 2026-11-01.
    november = span("us", "2026-11-02")
    assert (ist_hhmm(november[0]), ist_hhmm(november[1])) == ("20:00", "02:30")

    # Local New York time is unchanged on both sides of the switch.
    for sess in (october, november):
        assert sess[0].astimezone(NY).strftime("%H:%M") == "09:30"
        assert sess[1].astimezone(NY).strftime("%H:%M") == "16:00"


def test_a_us_holiday_has_no_session():
    assert span("us", "2026-11-26") is None      # Thanksgiving
    assert span("us", "2026-07-03") is None      # Independence Day observed
    assert span("us", "2027-03-26") is None      # Good Friday 2027
    assert span("us", "2026-11-25") is not None  # the day before is normal


def test_a_us_half_day_ends_early():
    """The day after Thanksgiving closes at 13:00 ET, and the square-off
    has to follow the real close rather than a nominal 16:00."""
    sess = span("us", "2026-11-27")
    assert sess[1].astimezone(NY).strftime("%H:%M") == "13:00"
    marks = markets.minute_marks(markets.get("us"), sess)
    assert marks["total_minutes"] == 210


def test_every_us_holiday_in_the_file_is_a_weekday():
    """The rule that caught a bad NSE list: an exchange never publishes a
    weekend closure, so a weekend date means the list is a public-holiday
    calendar and the whole file is suspect."""
    status = markets.us_holiday_status()
    assert status["years_loaded"] == ["2026", "2027"]
    assert status["holiday_count"] == 20
    for iso in markets._us_holidays:
        assert dt.date.fromisoformat(iso).weekday() < 5, iso


def test_forex_is_shut_from_friday_evening_to_sunday_evening_new_york():
    market = markets.get("forex")

    def open_at(when):
        return market.is_open(when)

    assert open_at(dt.datetime(2026, 10, 16, 16, 0, tzinfo=NY)) is True   # Fri 16:00
    assert open_at(dt.datetime(2026, 10, 16, 17, 30, tzinfo=NY)) is False  # Fri 17:30
    assert open_at(dt.datetime(2026, 10, 17, 12, 0, tzinfo=NY)) is False   # Saturday
    assert open_at(dt.datetime(2026, 10, 18, 16, 0, tzinfo=NY)) is False   # Sun 16:00
    assert open_at(dt.datetime(2026, 10, 18, 18, 0, tzinfo=NY)) is True    # Sun 18:00


def test_forex_has_no_weekend_session_but_crypto_does():
    # 2026-10-17 is a Saturday.
    assert span("forex", "2026-10-17") is None
    assert span("crypto", "2026-10-17") is not None
    assert span("crypto", "2026-10-18") is not None     # Sunday too


def test_a_forex_window_inside_the_weekend_gap_yields_no_session():
    """A window that only overlaps the shut part of the week is not a
    short session - it is no session."""
    window = {"start": "08:00", "end": "14:00", "tz": "Asia/Kolkata",
              "days": [0, 1, 2, 3, 4, 5, 6]}
    assert span("forex", "2026-10-17", window) is None


def test_nse_sessions_are_unchanged():
    """The India arena must behave exactly as it did before markets existed:
    09:15-15:30, last entry 14:45, square-off 15:15."""
    sess = span("nse", "2026-10-19")
    assert (ist_hhmm(sess[0]), ist_hhmm(sess[1])) == ("09:15", "15:30")
    marks = markets.minute_marks(markets.get("nse"), sess)
    assert marks == {"total_minutes": 375, "entry_cutoff": 330, "square_off": 360}
    # 09:15 + 330 min = 14:45; 09:15 + 360 min = 15:15.
    assert span("nse", "2026-10-17") is None            # Saturday


# ---------------------------------------------------------------- windows

def test_a_window_crossing_midnight_belongs_to_the_date_it_starts():
    window = {"start": "22:00", "end": "02:00", "tz": "Asia/Kolkata", "days": [0, 1, 2, 3, 4]}
    start, end = span("crypto", "2026-10-19", window)
    assert start.astimezone(IST).date().isoformat() == "2026-10-19"
    assert end.astimezone(IST).date().isoformat() == "2026-10-20"
    assert (ist_hhmm(start), ist_hhmm(end)) == ("22:00", "02:00")


def test_a_midnight_crossing_session_settles_once_under_its_start_date():
    window = {"start": "22:00", "end": "02:00", "tz": "Asia/Kolkata", "days": [0, 1, 2, 3, 4]}
    state, _ = run_sessions("crypto", ["BTC-USD"], ["2026-10-19", "2026-10-20"],
                            window=window, fx=markets.fx_for_session(
                                markets.get("crypto"), ["BTC-USD"], {"USDINR=X": 88.0}))
    dates = [h["date"] for h in state["history"]]
    assert dates == ["2026-10-19", "2026-10-20"], "one settlement per session, under its start date"
    assert len(dates) == len(set(dates))


def test_the_entry_cutoff_and_square_off_follow_the_window_end():
    """A user who trades 19:00-23:30 IST gets a cutoff at 23:00 and a
    square-off at 23:15, not NSE's 14:45 and 15:15."""
    window = {"start": "19:00", "end": "23:30", "tz": "Asia/Kolkata", "days": [0, 1, 2, 3, 4]}
    market = markets.get("crypto")
    sess = markets.active_session(market, window, dt.date(2026, 10, 19))
    marks = markets.minute_marks(market, sess)
    assert marks["total_minutes"] == 270
    assert marks["entry_cutoff"] == 240     # 19:00 + 4h = 23:00, 30 min before the end
    assert marks["square_off"] == 255       # 23:15, 15 min before the end

    # And the engine actually honours it: nothing is held past the end.
    fx = markets.fx_for_session(market, ["BTC-USD"], {"USDINR=X": 88.0})
    state, _ = run_sessions("crypto", ["BTC-USD"], ["2026-10-19"], window=window, fx=fx)
    assert all(not book["positions"] for book in state["books"].values())
    exits = [t["exit_at"] for b in state["books"].values() for t in b["trades"]]
    for exit_at in exits:
        moment = dt.datetime.fromisoformat(exit_at).astimezone(IST)
        assert moment.strftime("%H:%M") <= "23:30"


def test_a_window_shorter_than_the_cutoff_still_allows_entries():
    """A 40-minute window would otherwise forbid every entry, because the
    cutoff is 30 minutes and the square-off 15 - leaving a negative gap."""
    window = {"start": "20:00", "end": "20:40", "tz": "Asia/Kolkata", "days": [0, 1, 2, 3, 4]}
    market = markets.get("crypto")
    sess = markets.active_session(market, window, dt.date(2026, 10, 19))
    marks = markets.minute_marks(market, sess)
    assert marks["entry_cutoff"] < marks["square_off"]
    assert marks["entry_cutoff"] > 0


def test_a_window_that_excludes_a_weekday_has_no_session_that_day():
    window = {"start": "19:00", "end": "23:00", "tz": "Asia/Kolkata", "days": [0, 2, 4]}
    assert span("crypto", "2026-10-19", window) is not None     # Monday
    assert span("crypto", "2026-10-20", window) is None         # Tuesday, excluded


def test_the_opening_range_is_the_first_bars_of_the_active_session():
    """Not the first bars of the calendar day - a window starting at 22:00
    has its own opening range."""
    window = {"start": "22:00", "end": "02:00", "tz": "Asia/Kolkata", "days": [0, 1, 2, 3, 4]}
    market = markets.get("crypto")
    df = enrich(bars_for("crypto", ["2026-10-19"], window), market=market, window=window)
    first = df.index[0].tz_convert(IST)
    assert first.strftime("%H:%M") == "22:00"
    assert df["bar_no"].iloc[0] == 0
    assert df["sess_minute"].iloc[0] == 0
    # The range is unknown until its last bar has closed.
    assert pd.isna(df["or_high"].iloc[0])
    assert not pd.isna(df["or_high"].iloc[2])


# ---------------------------------------------------------------- forex

def test_zero_volume_gives_a_twap_and_an_undefined_volume_average():
    market = markets.get("forex")
    df = enrich(bars_for("forex", ["2026-10-19"], volume=0.0), market=market,
                window=market.default_window())
    assert bool(df["no_volume"].iloc[0]) is True
    assert df["vwap"].notna().all(), "TWAP must stand in for VWAP, not be NaN"
    # NaN, not zero: a zero average would make "volume >= 1.5x average"
    # trivially true and fire a breakout on every single bar.
    assert df["vol_avg20"].isna().all()
    # The TWAP of a rising series sits below the latest price.
    assert df["vwap"].iloc[-1] < df["Close"].iloc[-1]


def test_the_volume_free_variants_still_trade_on_a_feed_without_volume():
    market = markets.get("forex")
    fx = markets.fx_for_session(market, ["EURUSD=X"], {"USDINR=X": 88.0})
    gated = ["vwap_reclaim", "bb_breakout", "benchmark"]
    state, _ = run_sessions("forex", ["EURUSD=X"], ["2026-10-19", "2026-10-20"],
                            fx=fx, volume=0.0, strategies=gated)
    traded = {sid for sid, book in state["books"].items() if book["stats"]["trades"]}
    assert traded, "a volume-less feed must not silently disable every strategy"


def test_volume_gated_strategies_are_renamed_on_a_market_without_volume():
    from arena.strategies import STRATEGIES
    reclaim = STRATEGIES["vwap_reclaim"]
    assert reclaim.display_name(True) == "VWAP reclaim"
    assert reclaim.display_name(False) == "VWAP reclaim · no-volume variant"
    # A strategy that never looked at volume keeps its name.
    assert STRATEGIES["ema_cross"].display_name(False) == "EMA 9/21 crossover"


def test_the_forex_spread_is_charged_in_pips_of_the_pair():
    """A pip is 0.0001 on a major and 0.01 on a yen pair. Charging 0.0001
    of a yen quote would undercharge the spread a hundredfold."""
    model = markets.COST_MODELS["forex"]
    cfg = model.merged(None)

    # EUR/USD: 1.0 pip quoted, half a pip each side.
    assert model.pip_size("EURUSD=X") == 0.0001
    buy = model.fill_price("BUY", 1.1000, cfg, "EURUSD=X")
    sell = model.fill_price("SELL", 1.1000, cfg, "EURUSD=X")
    assert buy == pytest.approx(1.10005)
    assert sell == pytest.approx(1.09995)
    assert (buy - sell) == pytest.approx(0.0001)     # the full quoted spread

    # USD/JPY: 1.0 pip is 0.01 yen, so half a pip is 0.005.
    assert model.pip_size("USDJPY=X") == 0.01
    jpy_buy = model.fill_price("BUY", 150.00, cfg, "USDJPY=X")
    assert jpy_buy == pytest.approx(150.005)

    # An unlisted pair falls back to the default spread rather than to free.
    assert model.spread_pips("NZDCAD=X", cfg) == cfg["spread_pips_default"]
    # There is no per-ticket commission; the spread IS the cost.
    assert model.leg_charges("BUY", 10_000.0, cfg, "EURUSD=X")["total"] == 0.0


def test_a_yen_pairs_spread_is_scaled_with_its_price_into_the_book_currency():
    """A yen-quoted pair is traded in a dollar book by scaling its prices.
    The pip has to be scaled by the same factor, or the spread is charged
    at 150x its real size."""
    model = markets.COST_MODELS["forex"]
    cfg = model.merged(None)
    scale = 1 / 150.0
    native_half = model.half_spread("USDJPY=X", cfg)            # 0.005 yen
    scaled_half = model.half_spread("USDJPY=X", cfg, scale)     # the same, in dollars
    assert scaled_half == pytest.approx(native_half * scale)


def test_forex_and_crypto_trade_fractional_quantities():
    forex, crypto, us = markets.get("forex"), markets.get("crypto"), markets.get("us")
    assert engine.quantise(forex, 1.237) == pytest.approx(1.23)      # 0.01 steps
    assert engine.quantise(crypto, 0.1234567) == pytest.approx(0.123456)
    assert engine.quantise(us, 2.9) == 2.0                           # whole shares
    assert engine.quantise(us, 0.9) == 0.0
    assert engine.quantise(crypto, 0.0) == 0.0

    fx = markets.fx_for_session(forex, ["EURUSD=X"], {"USDINR=X": 88.0})
    state, _ = run_sessions("forex", ["EURUSD=X"], ["2026-10-19"], fx=fx, volume=0.0)
    sizes = [t["qty"] for b in state["books"].values() for t in b["trades"]]
    assert sizes, "the forex arena must actually trade"
    assert any(abs(q - round(q)) > 1e-9 for q in sizes), "sizes should not all be whole units"


# ---------------------------------------------------------------- currency

def test_rupee_capital_is_converted_at_the_recorded_rate_and_pnl_converted_back():
    market = markets.get("us")
    fx = markets.fx_for_session(market, ["AAPL"], {"USDINR=X": 88.5})
    assert fx["inr_per_unit"] == 88.5
    assert fx["incomplete"] is False

    state = engine.new_state({"market": "us", "symbols": ["AAPL"], "daily_capital": 10_000.0})
    state["fx"] = fx
    assert engine.capital_native(state) == pytest.approx(10_000 / 88.5)
    # ...and back again.
    assert markets.to_inr(engine.capital_native(state), fx) == pytest.approx(10_000, abs=0.01)
    assert markets.to_inr(10.0, fx) == 885.0


def test_a_yen_quoted_pair_is_scaled_into_the_book_currency():
    market = markets.get("forex")
    fx = markets.fx_for_session(market, ["EURUSD=X", "USDJPY=X", "USDINR=X"],
                                {"USDINR=X": 88.0, "USDJPY=X": 150.0})
    assert market.symbol_currency("EURUSD=X") == "USD"
    assert market.symbol_currency("USDJPY=X") == "JPY"
    assert market.symbol_currency("USDINR=X") == "INR"
    assert fx["symbol_scale"]["EURUSD=X"] == 1.0
    assert fx["symbol_scale"]["USDJPY=X"] == pytest.approx(1 / 150.0)
    assert fx["symbol_scale"]["USDINR=X"] == pytest.approx(1 / 88.0)

    # Scaling happens on the way in, so nothing downstream sees yen.
    df = enrich(bars_for("forex", ["2026-10-19"], lo=150.0, hi=151.0, volume=0.0),
                market=market, window=market.default_window(), scale=1 / 150.0)
    assert df["Close"].iloc[0] == pytest.approx(1.0, abs=0.01)


def test_a_missing_fx_rate_is_reported_rather_than_guessed():
    market = markets.get("us")
    fx = markets.fx_for_session(market, ["AAPL"], {})     # Yahoo gave nothing
    assert fx["inr_per_unit"] is None
    assert fx["incomplete"] is True
    assert markets.to_inr(10.0, fx) is None

    # The arena still trades - it just cannot state the rupee figure.
    state = engine.new_state({"market": "us", "symbols": ["AAPL"], "daily_capital": 10_000.0})
    state["fx"] = fx
    assert engine.capital_native(state) == 10_000.0
    assert engine.inr_per_unit(state) is None


def test_the_nse_arena_needs_no_conversion():
    state = engine.new_state({"market": "nse", "daily_capital": 10_000.0})
    assert engine.inr_per_unit(state) == 1.0
    assert engine.capital_native(state) == 10_000.0


def test_the_session_records_the_rate_it_used():
    market = markets.get("crypto")
    fx = markets.fx_for_session(market, ["BTC-USD"], {"USDINR=X": 90.0})
    state, _ = run_sessions("crypto", ["BTC-USD"], ["2026-10-19"], fx=fx)
    day = state["history"][0]
    assert day["fx_rate"] == 90.0
    assert day["currency"] == "USD"
    assert day["market"] == "crypto"
    # A later rate change must not restate a settled session.
    state["fx"] = markets.fx_for_session(market, ["BTC-USD"], {"USDINR=X": 95.0})
    assert state["history"][0]["fx_rate"] == 90.0


# ---------------------------------------------------------------- arenas

def test_the_four_markets_are_separate_arenas(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ARENA_PATH", str(tmp_path / "arena.json"))
    client = "10.0.0.9"
    for market_id in ("nse", "us", "forex", "crypto"):
        arena_service.load_or_create(store.key(client, market_id))

    keys = sorted(store.all_ids())
    assert keys == sorted(store.key(client, m) for m in ("nse", "us", "forex", "crypto"))

    # Changing one must not touch the others.
    arena_service.update_config(store.key(client, "us"), {"daily_capital": 75_000})
    assert store.load(store.key(client, "us"))["config"]["daily_capital"] == 75_000
    assert store.load(store.key(client, "nse"))["config"]["daily_capital"] == 10_000
    for market_id in ("nse", "us", "forex", "crypto"):
        cfg = store.load(store.key(client, market_id))["config"]
        assert cfg["market"] == market_id
        assert cfg["symbols"] == markets.get(market_id).default_symbols


def test_a_config_post_cannot_move_an_arena_to_another_market(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ARENA_PATH", str(tmp_path / "arena.json"))
    key = store.key("10.0.0.9", "nse")
    arena_service.load_or_create(key)
    cfg = arena_service.update_config(key, {"market": "crypto", "daily_capital": 20_000})
    assert cfg["market"] == "nse", "the market comes from the storage key, not the request"
    assert cfg["daily_capital"] == 20_000


def test_a_pre_markets_state_migrates_to_the_nse_key(tmp_path, monkeypatch):
    """The arena predates markets, so its keys are bare client ids. Those
    are that client's NSE arena and must survive the upgrade."""
    monkeypatch.setattr(store, "ARENA_PATH", str(tmp_path / "arena.json"))
    client = "10.0.0.7"
    legacy = engine.new_state({"symbols": ["RELIANCE.NS"], "daily_capital": 12_345})
    legacy["last_settled_day"] = "2026-10-09"
    store.save(client, legacy)                      # the old, unsuffixed key
    assert store.all_ids() == [client]

    migrated = store.load(store.key(client, "nse"))
    assert migrated is not None, "the old arena must not be abandoned"
    assert migrated["config"]["daily_capital"] == 12_345
    assert migrated["config"]["market"] == "nse"
    assert migrated["last_settled_day"] == "2026-10-09"
    # Moved, not copied - and only once.
    assert store.all_ids() == [store.key(client, "nse")]
    assert store.load(client) is None


def test_a_us_key_does_not_adopt_the_legacy_nse_state(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ARENA_PATH", str(tmp_path / "arena.json"))
    client = "10.0.0.8"
    store.save(client, engine.new_state({"daily_capital": 999}))
    assert store.load(store.key(client, "us")) is None
    assert store.load(store.key(client, "nse"))["config"]["daily_capital"] == 999


def test_keys_split_back_into_client_and_market():
    assert store.split_key("1.2.3.4::us") == ("1.2.3.4", "us")
    assert store.split_key("1.2.3.4") == ("1.2.3.4", "nse")      # legacy
    assert store.split_key("bt::1.2.3.4::forex") == ("bt::1.2.3.4", "forex")


def test_each_market_ticks_on_its_own_calendar(tmp_path, monkeypatch):
    """A Saturday: the NSE and forex arenas have nothing to do; crypto does."""
    monkeypatch.setattr(store, "ARENA_PATH", str(tmp_path / "arena.json"))
    saturday_evening = dt.datetime(2026, 10, 17, 21, 0, tzinfo=IST)
    client = "10.0.0.5"
    due = {}
    for market_id in ("nse", "us", "forex", "crypto"):
        key = store.key(client, market_id)
        arena_service.load_or_create(key)
        due[market_id] = arena_service.is_due(key, saturday_evening)
    assert due["crypto"] is True, "crypto runs at weekends"
    assert due["nse"] is False and due["forex"] is False


# ---------------------------------------------------------------- backtest

@pytest.mark.parametrize("market_id,symbols", [
    ("nse", ["RELIANCE.NS"]),
    ("us", ["AAPL"]),
    ("forex", ["EURUSD=X"]),
    ("crypto", ["BTC-USD"]),
])
def test_the_backtest_replays_each_market(market_id, symbols):
    market = markets.get(market_id)
    window = market.default_window()
    fx = markets.fx_for_session(market, symbols, {"USDINR=X": 88.0, "USDJPY=X": 150.0})
    dates = markets.sessions_in_range(market, window, dt.date(2026, 10, 5), dt.date(2026, 10, 9))
    volume = 0.0 if not market.has_volume else 10_000.0
    # US mega-cap prices need more than ₹10,000 to buy one whole share.
    capital = 200_000.0 if market_id == "us" else 10_000.0
    enriched = {}
    for sym in symbols:
        scale = (fx["symbol_scale"] or {}).get(sym) or 1.0
        enriched[sym] = enrich(bars_for(market_id, dates, window, volume=volume),
                               market=market, window=window, scale=float(scale))
    cfg = engine.normalise_config({"symbols": symbols, "daily_capital": capital,
                                   "window": window}, market_id=market_id)
    result = engine.replay(cfg, enriched, days=len(dates), fx=fx)

    assert result["replay"]["sessions"] == dates
    assert result["replay"]["market"] == market_id
    assert result["replay"]["currency"] == market.quote_currency
    board = engine.leaderboard(result)
    assert board and all(r["currency"] == market.quote_currency for r in board)
    assert any(r["trades"] for r in board), f"{market_id} backtest produced no trades"


def test_the_slippage_sweep_scales_the_markets_own_cost():
    """On forex the fill cost is a spread in pips, not a percentage, so the
    sweep scales the spread - the same experiment in that market's units."""
    market = markets.get("forex")
    window = market.default_window()
    fx = markets.fx_for_session(market, ["EURUSD=X"], {"USDINR=X": 88.0})
    dates = markets.sessions_in_range(market, window, dt.date(2026, 10, 5), dt.date(2026, 10, 7))
    enriched = {"EURUSD=X": enrich(bars_for("forex", dates, window, volume=0.0),
                                   market=market, window=window)}
    cfg = engine.normalise_config({"symbols": ["EURUSD=X"], "window": window},
                                  market_id="forex")
    sweep = engine.replay_slippage_sweep(cfg, enriched, days=len(dates), fx=fx)

    assert [lv["label"] for lv in sweep["levels"]] == ["0×", "0.5×", "1×"]
    assert sweep["levels"][0]["describes"] == "perfect fills, no cost"
    assert "pips" in sweep["levels"][2]["describes"]
    assert sweep["currency"] == "USD"
    bench = next(r for r in sweep["rows"] if r["id"] == "benchmark")
    # A wider assumed spread can only cost money, never make it.
    assert bench["by_slippage"]["0×"]["net"] >= bench["by_slippage"]["1×"]["net"]


def test_scaling_the_cost_touches_every_forex_spread():
    market = markets.get("forex")
    scaled = engine._scaled_cost_overrides(market, {}, 0.5)
    assert scaled["spread_pips_eurusd"] == 0.5          # half of 1.0
    assert scaled["spread_pips_usdinr"] == 1.0          # half of 2.0
    zero = engine._scaled_cost_overrides(market, {}, 0.0)
    assert all(v == 0 for k, v in zero.items() if k.startswith("spread_pips_"))
    # On a percentage market only the one slippage key moves.
    us_scaled = engine._scaled_cost_overrides(markets.get("us"), {}, 0.5)
    assert us_scaled == {"slippage_pct": 0.0001}


# ---------------------------------------------------------------- config

def test_symbols_are_normalised_per_market():
    assert engine.normalise_config({"symbols": ["reliance"]},
                                   market_id="nse")["symbols"] == ["RELIANCE.NS"]
    assert engine.normalise_config({"symbols": ["aapl"]},
                                   market_id="us")["symbols"] == ["AAPL"]
    assert engine.normalise_config({"symbols": ["eurusd", "gbp/usd"]},
                                   market_id="forex")["symbols"] == ["EURUSD=X", "GBPUSD=X"]
    assert engine.normalise_config({"symbols": ["btc"]},
                                   market_id="crypto")["symbols"] == ["BTC-USD"]


def test_leverage_is_capped_and_only_offered_where_it_applies():
    assert engine.normalise_config({"leverage": 3}, market_id="forex")["leverage"] == 3.0
    assert engine.normalise_config({"leverage": 50}, market_id="crypto")["leverage"] == 5.0
    assert engine.normalise_config({"leverage": 0.1}, market_id="forex")["leverage"] == 1.0
    # Equities get no leverage knob at all, whatever is asked for.
    assert engine.normalise_config({"leverage": 5}, market_id="us")["leverage"] == 1.0
    assert engine.normalise_config({"leverage": 5}, market_id="nse")["leverage"] == 1.0


def test_a_bad_window_falls_back_rather_than_raising():
    market = markets.get("crypto")
    base = market.default_window()
    assert markets.normalise_window({"start": "25:99"}, market, base)["start"] == base["start"]
    assert markets.normalise_window({"tz": "Mars/Olympus"}, market, base)["tz"] == base["tz"]
    assert markets.normalise_window({"days": []}, market, base)["days"] == base["days"]
    assert markets.normalise_window({"days": [0, 9, 3]}, market, base)["days"] == [0, 3]
    good = markets.normalise_window(
        {"start": "19:30", "end": "23:30", "tz": "Asia/Kolkata", "days": [1, 2]}, market, base)
    assert good == {"start": "19:30", "end": "23:30", "tz": "Asia/Kolkata", "days": [1, 2]}


def test_an_unknown_market_falls_back_to_the_nse():
    assert engine.normalise_config({}, market_id="mars")["market"] == "nse"
    assert markets.get("mars").id == "nse"
    assert markets.is_market_id("us") is True
    assert markets.is_market_id("mars") is False


def test_the_market_catalog_reports_every_market():
    rows = markets.catalog()
    assert [r["id"] for r in rows] == ["nse", "us", "forex", "crypto"]
    by_id = {r["id"]: r for r in rows}
    assert by_id["nse"]["currency"] == "INR"
    assert by_id["forex"]["has_volume"] is False
    assert by_id["crypto"]["fractional"] is True
    assert by_id["us"]["allow_leverage"] is False
    assert by_id["forex"]["allow_leverage"] is True


def test_session_info_states_the_window_in_both_timezones():
    market = markets.get("us")
    info = markets.session_info(market, market.default_window(),
                                now=dt.datetime(2026, 10, 15, 6, 0, tzinfo=dt.timezone.utc))
    assert info["today"]["start_ist"] == "19:00"
    assert info["today"]["end_ist"] == "01:30"
    assert info["today"]["start_local"] == "09:30"
    assert info["today"]["end_local"] == "16:00"
    assert info["market_timezone"] == "America/New_York"
    assert info["next"] is not None


# ---------------------------------------------------------------- catch-up

def test_a_us_arena_catches_up_sessions_the_server_slept_through(tmp_path, monkeypatch):
    """The whole point of the feature: the US session runs through the
    Indian night, when a free instance is asleep and nobody is browsing.
    Waking on Thursday morning IST must settle Monday, Tuesday and
    Wednesday's US sessions, in order, and tag them as replayed."""
    monkeypatch.setattr(store, "ARENA_PATH", str(tmp_path / "arena.json"))
    key = store.key("10.0.0.3", "us")
    market = markets.get("us")
    window = market.default_window()

    state = engine.new_state({"market": "us", "symbols": ["AAPL"],
                              "daily_capital": 200_000.0, "window": window})
    state["day"], state["day_settled"] = "2026-10-12", True
    state["last_settled_day"] = "2026-10-12"          # last ran Monday
    store.save(key, state)

    sessions = ["2026-10-13", "2026-10-14", "2026-10-15"]
    frame = enrich(bars_for("us", sessions, window), market=market, window=window)

    def fake_fetch(symbols, period=None, **kwargs):
        return ({s: frame for s in symbols},
                {"at": "x", "symbols_ok": list(symbols), "symbols_failed": [],
                 "latest_bar_at": None})

    monkeypatch.setattr(arena_service, "_fetch_enriched", fake_fetch)
    monkeypatch.setattr(arena_service, "fetch_fx",
                        lambda m, now=None: markets.fx_for_session(m, ["AAPL"],
                                                                  {"USDINR=X": 88.0}))

    # Thursday 09:00 IST - after Wednesday's US session closed at 01:30 IST.
    arena_service.tick_client(key, dt.datetime(2026, 10, 15, 9, 0, tzinfo=IST))

    # 09:00 IST Thursday is 23:30 Wednesday in New York, so Monday's and
    # Tuesday's US sessions are both over. Wednesday's bars are in the
    # stub but its session has not started, so it must NOT be traded.
    state = store.load(key)
    settled = [h["date"] for h in state["history"]]
    assert settled == ["2026-10-13", "2026-10-14"], \
        f"US sessions must be replayed in order, got {settled}"
    assert "2026-10-15" not in settled, "a session that has not happened must not be settled"
    assert state["last_settled_day"] == "2026-10-14"
    assert all(h["caught_up"] for h in state["history"]), "replayed days must say so"
    assert state["caught_up_last"] == 2

    # Running it again changes nothing.
    before = len(state["history"])
    arena_service.tick_client(key, dt.datetime(2026, 10, 15, 9, 5, tzinfo=IST))
    assert len(store.load(key)["history"]) == before


def test_bars_outside_the_window_are_dropped(tmp_path):
    """A user trading 19:00-21:00 IST must not have 22:00 bars folded into
    their session - they would widen the day and move the square-off."""
    market = markets.get("crypto")
    narrow = {"start": "19:00", "end": "21:00", "tz": "Asia/Kolkata", "days": [0, 1, 2, 3, 4]}
    wide = {"start": "19:00", "end": "23:30", "tz": "Asia/Kolkata", "days": [0, 1, 2, 3, 4]}
    raw = bars_for("crypto", ["2026-10-19"], wide)
    kept = enrich(raw, market=market, window=narrow)

    assert len(kept) < len(raw)
    for ts in kept.index:
        assert "19:00" <= ts.tz_convert(IST).strftime("%H:%M") <= "21:00"
    assert set(kept["session"]) == {"2026-10-19"}
