"""Second-wave strategies + short selling.

The key properties: shorts are charged the right legs and mirror the long
fill rules; every indicator is causal (no lookahead); each new strategy can
actually fire; and existing arenas get the new strategies switched on once.
"""
import numpy as np
import pandas as pd
import pytest

from arena import engine
from arena import strategies as S
from arena.strategies import enrich, STRATEGIES

IST = "Asia/Kolkata"


def bars(date, closes, gap_from=None, volume=1000.0):
    closes = np.asarray(closes, dtype=float)
    idx = pd.date_range(f"{date} 09:15", periods=len(closes), freq="5min", tz=IST)
    o = np.r_[closes[0] if gap_from is None else closes[0], closes[:-1]]
    return pd.DataFrame({"Open": o, "High": np.maximum(o, closes) + 0.1,
                         "Low": np.minimum(o, closes) - 0.1, "Close": closes,
                         "Volume": np.full(len(closes), volume)}, index=idx)


def two_days(day1, day2):
    return pd.concat([bars("2026-09-01", day1), bars("2026-09-02", day2)])


def cfg(ids, **risk):
    return {"strategies": ids, "symbols": ["T.NS"], "daily_capital": 10_000,
            "risk": {"min_reward_to_cost": 0.5, **risk}}


def run(state, df, day):
    engine.start_day(state, day)
    engine.process_bars(state, {"T.NS": enrich(df)}, now=None)
    engine.settle_day(state)


# ------------------------------------------------------------- shorts

def test_short_exit_rules_mirror_longs():
    pos = {"side": "SHORT", "stop": 101.0, "target": 98.0}
    strat = STRATEGIES["orb_both"]
    touch_both = pd.Series({"Open": 100.0, "High": 101.5, "Low": 97.5, "Close": 99.0})
    assert engine._exit_decision(strat, touch_both, touch_both, pos, 600) == (101.0, "stop_loss")
    gap_up = pd.Series({"Open": 102.0, "High": 102.5, "Low": 101.8, "Close": 102.2})
    assert engine._exit_decision(strat, gap_up, gap_up, pos, 600) == (102.0, "stop_loss_gap")
    hit_target = pd.Series({"Open": 99.0, "High": 99.5, "Low": 97.9, "Close": 98.5})
    assert engine._exit_decision(strat, hit_target, hit_target, pos, 600) == (98.0, "target")


def test_short_trade_cash_and_pnl_add_up():
    st = engine.new_state(cfg(["orb_both"]))
    engine.start_day(st, "2026-09-01")
    book = st["books"]["orb_both"]
    start_cash = book["cash"]

    class Shorty(S.Strategy):
        id = "orb_both"
        def entry(self, row, prev):
            return {"side": "SHORT", "stop": row["Close"] * 1.02, "target": row["Close"] * 0.97, "reason": "t"}

    real = STRATEGIES["orb_both"]
    STRATEGIES["orb_both"] = Shorty()
    try:
        ts = pd.Timestamp("2026-09-01 10:00", tz=IST)
        row = pd.Series({"Open": 100.0, "High": 100.1, "Low": 99.9, "Close": 100.0, "minute": 600, "bar_no": 9})
        engine.process_bar(st, "T.NS", ts, row, row, 1)
        pos = book["positions"]["T.NS"]
        assert pos["side"] == "SHORT"
        # price falls 2%: the short should be worth more than it cost
        st["marks"]["T.NS"] = {"price": 98.0, "at": "x"}
        assert engine._equity(st, book) > start_cash
        trade = engine._close(st, book, "T.NS", 98.0, "manual", "x")
    finally:
        STRATEGIES["orb_both"] = real
    assert trade["side"] == "SHORT" and trade["gross"] > 0
    assert trade["net"] == pytest.approx(trade["gross"] - trade["charges"], abs=0.02)
    assert book["cash"] == pytest.approx(start_cash + trade["net"], abs=0.05)


def test_short_pays_stt_on_entry_and_stamp_on_exit():
    from arena import costs
    c = costs.merged(None)
    assert costs.leg_charges("SELL", 10_000, c)["stt"] > 0          # short entry
    assert costs.leg_charges("BUY", 10_000, c)["stamp"] > 0         # short cover


# ------------------------------------------------------------- no lookahead

def test_every_indicator_is_causal():
    rng = np.random.default_rng(3)
    df = pd.concat([bars(d, 100 * np.exp(np.cumsum(rng.normal(0, 0.003, 75))))
                    for d in ["2026-09-01", "2026-09-02"]])
    full = enrich(df)
    cols = [c for c in full.columns if c not in ("session",) and full[c].dtype != object]
    for k in (20, 76, 90, 120, 149):
        part = enrich(df.iloc[:k + 1])
        a, b = part.iloc[-1][cols].astype(float), full.iloc[k][cols].astype(float)
        both_nan = a.isna() & b.isna()
        diff = (a - b).abs()[~both_nan]
        assert (diff < 1e-9).all(), f"lookahead at bar {k}: {diff[diff >= 1e-9].to_dict()}"


# ------------------------------------------------------------- each strategy fires

def test_orb_both_shorts_a_breakdown():
    closes = [100, 100.2, 99.9] + list(np.linspace(99.8, 97, 30)) + [97] * 42
    st = engine.new_state(cfg(["orb_both"]))
    run(st, bars("2026-09-01", closes), "2026-09-01")
    t = st["books"]["orb_both"]["trades"]
    assert t and t[0]["side"] == "SHORT"


def test_supertrend_trades_a_reversal_both_ways():
    closes = list(np.linspace(100, 106, 35)) + list(np.linspace(106, 98, 40))
    st = engine.new_state(cfg(["supertrend"]))
    df = pd.concat([bars("2026-08-31", np.linspace(100, 100.5, 75)), bars("2026-09-01", closes)])
    engine.start_day(st, "2026-09-01")
    engine.process_bars(st, {"T.NS": enrich(df)}, now=None)
    engine.settle_day(st)
    sides = {t["side"] for t in st["books"]["supertrend"]["trades"]}
    assert "SHORT" in sides


def test_gap_fade_shorts_a_failing_gap_up():
    day1 = np.full(75, 100.0)
    day2 = [101.5, 101.6, 101.4, 101.0, 100.8, 100.6] + list(np.linspace(100.5, 99.8, 69))
    df = two_days(day1, day2)
    st = engine.new_state(cfg(["gap_fade"]))
    engine.start_day(st, "2026-09-02")
    engine.process_bars(st, {"T.NS": enrich(df)}, now=None)
    engine.settle_day(st)
    t = st["books"]["gap_fade"]["trades"]
    assert t and t[0]["side"] == "SHORT" and t[0]["exit_reason"] == "target"


def test_intraday_momentum_enters_at_1415_only():
    day1 = np.full(75, 100.0)
    day2 = list(np.linspace(100, 101, 6)) + list(np.linspace(101, 102, 69))   # +1% by 09:45
    df = two_days(day1, day2)
    st = engine.new_state(cfg(["intraday_momentum"]))
    engine.start_day(st, "2026-09-02")
    engine.process_bars(st, {"T.NS": enrich(df)}, now=None)
    engine.settle_day(st)
    t = st["books"]["intraday_momentum"]["trades"]
    assert len(t) == 1 and t[0]["side"] == "LONG"
    assert "T14:15" in t[0]["entry_at"]


def test_new_strategies_never_crash_on_random_data():
    rng = np.random.default_rng(11)
    df = pd.concat([bars(d.date().isoformat(), 500 * np.exp(np.cumsum(rng.normal(0, 0.004, 75))))
                    for d in pd.bdate_range("2026-08-03", periods=8)])
    ids = [s for s in STRATEGIES if s not in S.ORIGINAL_STRATEGY_IDS] + ["benchmark"]
    st = engine.replay(cfg(ids), {"T.NS": enrich(df)}, days=8)
    assert all(b["positions"] == {} for b in st["books"].values())
    assert sum(b["stats"]["trades"] for b in st["books"].values()) > 0


# ------------------------------------------------------------- adoption

def test_existing_arena_gets_new_strategies_once_and_opt_out_sticks():
    st = engine.new_state(cfg(["orb", "ema_cross", "benchmark"]))
    st["config"].pop("known_strategies")          # an arena from before the update
    added = engine.adopt_new_strategies(st)
    assert set(added) == set(STRATEGIES) - set(S.ORIGINAL_STRATEGY_IDS)
    assert st["config"]["strategies"][-1] == "benchmark"

    st["config"]["strategies"].remove("gap_fade")   # user unticks one
    assert engine.adopt_new_strategies(st) == []
    assert "gap_fade" not in st["config"]["strategies"]


def test_brand_new_arena_has_everything_and_nothing_to_adopt():
    st = engine.new_state()
    assert engine.adopt_new_strategies(st) == []
    assert set(st["config"]["strategies"]) == set(STRATEGIES)
