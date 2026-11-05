"""Choosing the stocks, and dividing the book between them.

Two changes that only mean anything together: screening a broad universe is
pointless if the book still only ever holds two positions, and spreading
across more positions is pointless if they are the same twelve stocks every
day.

No network: the screen's single batched download is stubbed, and the
universe is driven with synthetic daily bars whose liquidity, volatility
and price are set deliberately so the expected verdict is obvious.
"""
import datetime as dt

import numpy as np
import pandas as pd
import pytest

from arena import engine, markets, store, universe
from arena import service as arena_service


def daily(price, atr_pct, turnover_crore, days=40, seed=1):
    """Daily bars with a chosen price, daily range and turnover."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2026-09-01", periods=days)
    close = float(price) * np.exp(np.cumsum(rng.normal(0, atr_pct / 3, days)))
    return pd.DataFrame({
        "Open": close, "High": close * (1 + atr_pct / 2),
        "Low": close * (1 - atr_pct / 2), "Close": close,
        "Volume": np.full(days, turnover_crore * 1e7 / float(price)),
    }, index=idx)


GOOD = dict(price=400, atr_pct=0.025, turnover_crore=200)

# Captured at import, before conftest's autouse fixture replaces it.
_REAL_FETCH_DAILY = universe.fetch_daily


# ---------------------------------------------------------------- the screen

def test_it_keeps_liquid_moving_affordable_stocks_and_says_why_it_drops_the_rest():
    frames = {
        "GOOD.NS": daily(**GOOD),
        "ILLIQUID.NS": daily(400, 0.03, 2),        # nobody to trade with
        "COMATOSE.NS": daily(400, 0.002, 500),     # cannot pay its own costs
        "WILD.NS": daily(400, 0.15, 500),          # news or a circuit, not a trade
        "DEAR.NS": daily(9000, 0.02, 400),         # one share eats the allowance
    }
    result = universe.screen("nse", 10, allowance=12_500, frames=frames)
    assert result["symbols"] == ["GOOD.NS"]
    assert result["rejected"] == 4

    by_symbol = {r["symbol"]: r for r in universe.measure(frames, 12_500)}
    assert "turnover" in by_symbol["ILLIQUID.NS"]["excluded"][0]
    assert "daily range" in by_symbol["COMATOSE.NS"]["excluded"][0]
    assert "daily range" in by_symbol["WILD.NS"]["excluded"][0]
    assert "unused" in by_symbol["DEAR.NS"]["excluded"][0]


def test_affordability_is_judged_on_utilisation_not_share_count():
    """The two disagree exactly where it matters. Against a ₹2,000
    allowance a ₹1,750 share is one share and 88% of the money used, which
    is fine; a 'must buy 4 shares' rule would throw it out."""
    assert universe.utilisation(2_000, 1_750) == pytest.approx(0.875)
    assert universe.utilisation(10_000, 3_100) == pytest.approx(0.93)
    assert universe.utilisation(10_000, 6_000) == pytest.approx(0.60)
    assert universe.utilisation(10_000, 20_000) == 0.0      # cannot buy one
    assert universe.utilisation(0, 100) == 0.0

    small = {"OK.NS": daily(1750, 0.02, 200), "NO.NS": daily(1200, 0.02, 200)}
    picked = universe.screen("nse", 5, allowance=2_000, frames=small)
    # ₹1,750 uses 88% of the allowance; ₹1,200 uses only 60%.
    assert picked["symbols"] == ["OK.NS"]


def test_a_bigger_book_can_afford_more_of_the_market():
    """The screen is relative to the book, so it widens by itself rather
    than needing a different stock list."""
    frames = {"MID.NS": daily(3_100, 0.02, 300)}
    assert universe.screen("nse", 5, allowance=2_000, frames=frames)["symbols"] == []
    assert universe.screen("nse", 5, allowance=12_500, frames=frames)["symbols"] == ["MID.NS"]


def test_liquidity_outranks_movement():
    """An untradeable stock that moves a lot is worse than a tradeable one
    that moves less - you cannot get out of the first."""
    frames = {
        "CALM_LIQUID.NS": daily(400, 0.015, 500, seed=2),
        "LIVELY_THIN.NS": daily(400, 0.04, 30, seed=3),
    }
    picks = universe.screen("nse", 2, allowance=12_500, frames=frames)["symbols"]
    assert picks[0] == "CALM_LIQUID.NS"


def test_the_movement_band_prefers_the_middle_not_the_extreme():
    inside = universe._band_score(0.025, *universe.ATR_SWEET_SPOT)
    sluggish = universe._band_score(0.008, *universe.ATR_SWEET_SPOT)
    frantic = universe._band_score(0.08, *universe.ATR_SWEET_SPOT)
    assert inside == 1.0
    assert 0 < sluggish < 1 and 0 < frantic < 1


def test_a_stock_with_too_little_history_is_not_judged():
    frames = {"NEW.NS": daily(400, 0.025, 200, days=5)}
    assert universe.screen("nse", 5, allowance=12_500, frames=frames)["symbols"] == []


def test_the_screen_is_capped_and_ordered():
    frames = {f"S{i}.NS": daily(400, 0.025, 100 + i, seed=i) for i in range(30)}
    result = universe.screen("nse", 8, allowance=12_500, frames=frames)
    assert len(result["symbols"]) == 8
    scores = [r["score"] for r in result["rows"]]
    assert scores == sorted(scores, reverse=True), "best first"
    assert len(universe.screen("nse", 999, allowance=12_500,
                               frames=frames)["symbols"]) <= universe.MAX_SYMBOLS


def test_a_market_with_no_universe_screens_nothing():
    assert universe.screen("crypto", 5)["symbols"] == []
    assert universe.screen("us", 5)["symbols"] == []


def test_the_nse_universe_is_clean():
    assert len(universe.NSE_UNIVERSE) > 120
    assert len(set(universe.NSE_UNIVERSE)) == len(universe.NSE_UNIVERSE)
    assert all(s.endswith(".NS") and s.isupper() for s in universe.NSE_UNIVERSE)
    # A symbol that stops trading returns no data and drops out, so a stale
    # entry costs a slot in one download and nothing else.
    assert universe.screen("nse", 5, allowance=12_500, frames={})["symbols"] == []


# ---------------------------------------------------------------- allocation

def test_the_book_divides_evenly_between_its_slots():
    cfg = engine.new_state({"market": "nse"})["config"]
    assert engine.open_slots(cfg) == 4
    assert engine.position_share(cfg) == pytest.approx(0.25)
    assert cfg["risk"]["max_open_positions"] == 4
    assert cfg["risk"]["max_position_pct"] == 25.0


def test_slots_are_per_market_because_affordability_is():
    """₹50,000 split five ways is $113 a position, which will not buy one
    share of a US mega-cap. Split two ways it is $282, which will."""
    assert engine.open_slots(engine.new_state({"market": "us"})["config"]) == 2
    assert engine.open_slots(engine.new_state({"market": "crypto"})["config"]) == 3
    assert engine.open_slots(engine.new_state({"market": "nse"})["config"]) == 4


def test_an_arena_made_before_these_settings_existed_still_gets_them():
    """The point of reading them with defaults rather than storing them:
    an arena already running in Postgres picks the new behaviour up on its
    next tick, without anything rewriting the settings its owner chose."""
    old = {"market": "nse", "daily_capital": 10_000.0,
           "symbols": ["RELIANCE.NS"],
           "risk": {"max_open_positions": 2, "max_position_pct": 50.0}}
    assert engine.open_slots(old) == 4
    assert engine.position_share(old) == pytest.approx(0.25)
    assert old.get("universe", "auto") == "auto"
    # ...and the stored list is untouched, so switching back returns to it.
    assert old["symbols"] == ["RELIANCE.NS"]


def test_legacy_allocation_is_still_available():
    cfg = engine.normalise_config(
        {"allocation": "legacy", "risk": {"max_open_positions": 2, "max_position_pct": 50}},
        market_id="nse")
    assert engine.open_slots(cfg) == 2
    assert engine.position_share(cfg) == pytest.approx(0.5)


def test_the_risk_budget_grows_with_the_slots_or_the_slots_stay_empty():
    """With five equal slots and a 1% risk budget the stop sizes every
    position and four slots never fill - the arena would look like it was
    ignoring its own setting."""
    cfg = engine.normalise_config({"open_slots": 6}, market_id="nse")
    assert cfg["risk"]["risk_per_trade_pct"] > engine.DEFAULT_RISK["risk_per_trade_pct"]
    # A figure the user typed is theirs and is not inflated.
    mine = engine.normalise_config({"open_slots": 6, "risk": {"risk_per_trade_pct": 0.5}},
                                   market_id="nse")
    assert mine["risk"]["risk_per_trade_pct"] == 0.5


def test_slots_and_universe_size_are_bounded():
    assert engine.normalise_config({"open_slots": 99}, market_id="nse")["open_slots"] == 20
    # Zero slots would divide the book by nothing; clamped to one.
    assert engine.normalise_config({"open_slots": 0}, market_id="nse")["open_slots"] == 1
    assert engine.normalise_config({"universe_size": 999},
                                   market_id="nse")["universe_size"] == 25
    assert engine.normalise_config({"universe": "nonsense"},
                                   market_id="nse")["universe"] == "auto"


# ---------------------------------------------------------------- wiring

def test_the_picks_are_what_gets_traded_and_the_users_list_is_kept():
    state = engine.new_state({"market": "nse", "symbols": ["RELIANCE.NS"]})
    assert engine.active_symbols(state) == ["RELIANCE.NS"], "no picks yet"
    state["picked"] = {"symbols": ["ITC.NS", "SBIN.NS"], "session": "2026-11-05"}
    assert engine.active_symbols(state) == ["ITC.NS", "SBIN.NS"]
    assert state["config"]["symbols"] == ["RELIANCE.NS"], "the stored list is untouched"
    # Switching back to a fixed list returns to it, not to the last screen.
    state["config"]["universe"] = "fixed"
    assert engine.active_symbols(state) == ["RELIANCE.NS"]


def test_picks_are_made_once_per_session(monkeypatch):
    state = engine.new_state({"market": "nse"})
    calls = []

    def fake_screen(market_id, count, allowance=None, frames=None):
        calls.append(market_id)
        return {"symbols": ["ITC.NS"], "rows": [], "measured": 1, "rejected": 0}

    monkeypatch.setattr(universe, "screen", fake_screen)
    first = arena_service.choose_symbols(state, "2026-11-05")
    assert first["symbols"] == ["ITC.NS"] and first["session"] == "2026-11-05"
    state["picked"] = first
    # Same session again: no second screen, so every strategy in the arena
    # trades the same stocks and the day stays a fair comparison.
    assert arena_service.choose_symbols(state, "2026-11-05") is None
    assert arena_service.choose_symbols(state, "2026-11-06") is not None
    assert len(calls) == 2


def test_a_fixed_arena_is_never_screened(monkeypatch):
    state = engine.new_state({"market": "nse"})
    state["config"]["universe"] = "fixed"
    monkeypatch.setattr(universe, "screen",
                        lambda *a, **k: pytest.fail("a fixed list must not be screened"))
    assert arena_service.choose_symbols(state, "2026-11-05") is None


def test_a_failed_screen_keeps_yesterdays_list_rather_than_trading_nothing(monkeypatch):
    state = engine.new_state({"market": "nse"})

    def boom(*a, **k):
        raise RuntimeError("yahoo said no")

    monkeypatch.setattr(universe, "screen", boom)
    assert arena_service.choose_symbols(state, "2026-11-05") is None

    monkeypatch.setattr(universe, "screen", lambda *a, **k: {"symbols": []})
    assert arena_service.choose_symbols(state, "2026-11-05") is None


def test_the_screen_is_downloaded_once_per_process(monkeypatch):
    """Five arenas on one market must not pull the same 160 daily series
    five times."""
    universe._daily_cache.clear()
    calls = []

    def fake_download(symbols, period, interval, timeout=None, flatten=True):
        calls.append(symbols)
        return pd.DataFrame()

    # conftest stubs fetch_daily for the whole suite so nothing reaches
    # Yahoo; this is the one test that needs the real one, stubbed one
    # layer lower.
    monkeypatch.setattr(universe, "fetch_daily", _REAL_FETCH_DAILY)
    monkeypatch.setattr(universe.data_sources, "_yf_download", fake_download)
    for _ in range(5):
        universe.fetch_daily(["A.NS", "B.NS"])
    assert len(calls) == 1
    universe._daily_cache.clear()


def test_the_allowance_follows_the_book_and_the_slots():
    assert universe.allowance_per_position(50_000, 4) == 12_500
    assert universe.allowance_per_position(10_000, 4) == 2_500
    assert universe.allowance_per_position(50_000, 0) == 50_000     # never divides by zero


def test_an_auto_arena_on_a_market_without_a_universe_keeps_its_own_list(tmp_path,
                                                                        monkeypatch):
    monkeypatch.setattr(store, "ARENA_PATH", str(tmp_path / "arena.json"))
    state = engine.new_state({"market": "crypto"})
    assert state["config"]["universe"] == "auto"
    assert arena_service.choose_symbols(state, "2026-11-05") is None
    assert engine.active_symbols(state) == markets.get("crypto").default_symbols


def test_the_session_date_drives_the_pick_not_the_wall_clock():
    assert universe.is_stale(None, "2026-11-05") is True
    assert universe.is_stale({"symbols": []}, "2026-11-05") is True
    assert universe.is_stale({"symbols": ["X"], "session": "2026-11-04"}, "2026-11-05") is True
    assert universe.is_stale({"symbols": ["X"], "session": "2026-11-05"}, "2026-11-05") is False


def test_the_screen_never_uses_todays_bar():
    """Picking today's movers with today's data is the oldest way to make
    a backtest look brilliant and a live account lose money."""
    now = dt.datetime(2026, 11, 5, 11, 0, tzinfo=markets.IST)
    assert universe.previous_session_cutoff(now) == dt.date(2026, 11, 4)


def test_a_batched_download_is_split_back_into_one_frame_per_symbol(monkeypatch):
    """The bug this exists for: `_yf_download` flattens a column
    MultiIndex, and for a batched download the level it drops is the
    TICKER. The screen then saw one symbol's bars repeated 160 times,
    found nothing eligible, and fell back to the old list - silently, on
    a green run.
    """
    import pandas as pd

    symbols = ["A.NS", "B.NS", "C.NS"]
    idx = pd.bdate_range("2026-09-01", periods=30)
    frame = pd.DataFrame(
        {(field, sym): range(len(idx))
         for sym in symbols for field in ("Open", "High", "Low", "Close", "Volume")},
        index=idx)
    frame.columns = pd.MultiIndex.from_tuples(frame.columns)

    seen = {}

    def fake_download(tickers, period, interval, timeout=None, flatten=True):
        seen["flatten"] = flatten
        seen["tickers"] = tickers
        return frame

    universe._daily_cache.clear()
    monkeypatch.setattr(universe, "fetch_daily", _REAL_FETCH_DAILY)
    monkeypatch.setattr(universe.data_sources, "_yf_download", fake_download)
    frames = universe.fetch_daily(symbols)
    universe._daily_cache.clear()

    assert seen["flatten"] is False, "flattening destroys the ticker level"
    assert seen["tickers"] == "A.NS B.NS C.NS", "one request, not three"
    assert sorted(frames) == symbols
    for sym in symbols:
        assert list(frames[sym].columns) == ["Open", "High", "Low", "Close", "Volume"]
        assert len(frames[sym]) == 30


def test_a_single_symbol_download_still_works(monkeypatch):
    import pandas as pd

    idx = pd.bdate_range("2026-09-01", periods=30)
    flat = pd.DataFrame({"Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 1.0,
                         "Volume": 1.0}, index=idx)
    universe._daily_cache.clear()
    monkeypatch.setattr(universe, "fetch_daily", _REAL_FETCH_DAILY)
    monkeypatch.setattr(universe.data_sources,
                        "_yf_download", lambda *a, **k: flat)
    frames = universe.fetch_daily(["ONLY.NS"])
    universe._daily_cache.clear()
    assert list(frames) == ["ONLY.NS"]
