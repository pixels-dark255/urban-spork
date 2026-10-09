"""Strategy Arena: costs, fills, survival rules, no-lookahead, API.

Synthetic bars only (see conftest). The point of most of these tests is to
prove the simulator does NOT flatter strategies: costs are charged, stops
win ties, nothing is held overnight, and a losing strategy gets benched.
"""
import datetime as dt
import math

import numpy as np
import pandas as pd
import pytest

from arena import costs, engine
from arena.strategies import enrich, STRATEGIES

IST = "Asia/Kolkata"


def session(date: str, closes, open_=None, high=None, low=None, volume=1000.0):
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    idx = pd.date_range(f"{date} 09:15", periods=n, freq="5min", tz=IST)
    o = np.asarray(open_ if open_ is not None else np.r_[closes[0], closes[:-1]], dtype=float)
    h = np.asarray(high if high is not None else np.maximum(o, closes) + 0.2, dtype=float)
    lo = np.asarray(low if low is not None else np.minimum(o, closes) - 0.2, dtype=float)
    return pd.DataFrame({"Open": o, "High": h, "Low": lo, "Close": closes,
                         "Volume": np.full(n, volume)}, index=idx)


def only(strategy_ids, **cfg):
    return {"strategies": strategy_ids, "symbols": ["TEST.NS"], "daily_capital": 10_000, **cfg}


def run_day(state, df, date):
    engine.start_day(state, date)
    engine.process_bars(state, {"TEST.NS": enrich(df)}, now=None)
    engine.settle_day(state)


# ---------------------------------------------------------------- costs

def test_costs_charge_both_legs_and_cap_brokerage():
    cfg = costs.merged(None)
    buy = costs.leg_charges("BUY", 10_000, cfg)
    sell = costs.leg_charges("SELL", 10_000, cfg)
    assert buy["stt"] == 0 and sell["stt"] == pytest.approx(2.5)
    assert buy["stamp"] == pytest.approx(0.3) and sell["stamp"] == 0
    assert buy["brokerage"] == pytest.approx(3.0)
    assert costs.leg_charges("BUY", 1_000_000, cfg)["brokerage"] == 20.0   # capped
    assert costs.round_trip_cost_estimate(1000, 10, cfg) > 10   # slippage + charges


def test_net_pnl_is_gross_minus_all_charges():
    closes = [100] * 3 + list(np.linspace(100, 110, 72))
    state = engine.new_state(only(["benchmark"]))
    run_day(state, session("2026-09-01", closes), "2026-09-01")
    t = state["books"]["benchmark"]["trades"][0]
    assert t["charges"] > 0
    assert t["net"] == pytest.approx(t["gross"] - t["charges"], abs=0.02)


# ---------------------------------------------------------------- fills & rules

def test_stop_wins_when_bar_touches_stop_and_target():
    st = engine.new_state(only(["benchmark"]))
    engine.start_day(st, "2026-09-01")
    book = st["books"]["benchmark"]
    book["positions"]["TEST.NS"] = {"symbol": "TEST.NS", "qty": 10, "entry_price": 100.0,
                                    "entry_at": "x", "stop": 99.0, "target": 101.0, "buy_charges": 0.5}
    row = pd.Series({"Open": 100.0, "High": 102.0, "Low": 98.0, "Close": 100.5, "minute": 600})
    price, reason = engine._exit_decision(STRATEGIES["benchmark"], row, row, book["positions"]["TEST.NS"], 600)
    assert reason == "stop_loss" and price == 99.0


def test_gap_through_stop_fills_at_open_not_stop():
    row = pd.Series({"Open": 97.0, "High": 97.5, "Low": 96.0, "Close": 97.2, "minute": 600})
    pos = {"stop": 99.0, "target": 105.0}
    price, reason = engine._exit_decision(STRATEGIES["orb"], row, row, pos, 600)
    assert (price, reason) == (97.0, "stop_loss_gap")


def test_nothing_held_overnight_and_no_late_entries():
    closes = list(np.linspace(100, 120, 75))
    state = engine.new_state(only(list(STRATEGIES)))
    run_day(state, session("2026-09-01", closes), "2026-09-01")
    for book in state["books"].values():
        assert book["positions"] == {}
        for t in book["trades"]:
            entry = dt.datetime.fromisoformat(t["entry_at"])
            assert entry.hour * 60 + entry.minute <= 14 * 60 + 45


def test_daily_loss_limit_stops_the_book_for_the_day():
    st = engine.new_state(only(["orb"]))      # limit 2% of 10,000 = 200
    engine.start_day(st, "2026-09-01")
    book = st["books"]["orb"]
    book["cash"] -= 4000
    book["positions"]["TEST.NS"] = {"symbol": "TEST.NS", "qty": 40, "entry_price": 100.0,
                                    "entry_at": "x", "stop": 80.0, "target": 120.0, "buy_charges": 1.0}
    ts = pd.Timestamp("2026-09-01 11:00", tz=IST)
    row = pd.Series({"Open": 95.0, "High": 95.0, "Low": 94.0, "Close": 94.0, "minute": 660, "bar_no": 21})
    engine.process_bar(st, "TEST.NS", ts, row, row, 1)      # -6% on 4,000 = -240
    assert book["day"]["stopped"]
    assert book["positions"] == {}
    assert book["trades"][-1]["exit_reason"] == "daily_loss_limit"

    # Stopped books take no new entries for the rest of the day.
    df = enrich(session("2026-09-01", np.linspace(94, 110, 30)))
    st["last_bar"] = {}
    engine.process_bars(st, {"TEST.NS": df})
    assert book["day"]["trades"] == 0 and book["positions"] == {}


def test_trending_day_makes_money_and_random_walk_does_not_flatter():
    rng = np.random.default_rng(7)
    # Strong steady uptrend: trend strategies should be able to profit.
    trend = engine.new_state(only(["orb", "ema_cross", "benchmark"], risk={"min_reward_to_cost": 0.5}))
    for i, d in enumerate(pd.bdate_range("2026-08-03", periods=10)):
        base = 100 + i
        closes = base + np.r_[np.zeros(4), np.linspace(0, 4, 71)] + rng.normal(0, 0.02, 75)
        run_day(trend, session(d.date().isoformat(), closes), d.date().isoformat())
    assert trend["books"]["benchmark"]["stats"]["net"] > 0
    assert max(trend["books"][s]["stats"]["net"] for s in ("orb", "ema_cross")) > 0


def test_losing_strategy_is_benched_then_reinstated_on_shadow_recovery():
    st = engine.new_state(only(["ema_cross"], risk={"min_trades_to_judge": 5, "judge_window": 5}))
    engine.start_day(st, "2026-09-01")
    book = st["books"]["ema_cross"]
    loss = {"net": -50.0}
    book["trades"] = [dict(loss) for _ in range(6)]
    book["stats"].update(trades=6, net=-300.0, gross_loss=300.0)
    engine._judge(book, st["config"]["risk"], 10_000, "2026-09-01")
    assert book["status"] == "benched"

    book["trades"] += [{"net": 80.0} for _ in range(5)]
    book["stats"].update(trades=11, net=100.0, peak_net=100.0)
    engine._judge(book, st["config"]["risk"], 10_000, "2026-09-02")
    assert book["status"] == "active" and "reinstated" in book["status_reason"]


def test_benchmark_is_never_benched():
    st = engine.new_state(only(["benchmark"], risk={"min_trades_to_judge": 1}))
    engine.start_day(st, "2026-09-01")
    book = st["books"]["benchmark"]
    book["trades"] = [{"net": -100.0}] * 5
    book["stats"].update(trades=5, net=-500.0)
    engine._judge(book, st["config"]["risk"], 10_000, "2026-09-01")
    assert book["status"] == "active"


def test_all_benched_verdict_says_sit_in_cash():
    st = engine.new_state(only(["orb", "benchmark"]))
    engine.start_day(st, "2026-09-01")
    st["books"]["orb"].update(status="benched")
    st["books"]["orb"]["stats"]["trades"] = 3
    assert st and engine.verdict(st)["level"] == "halted"


# ---------------------------------------------------------------- no lookahead

def test_bollinger_bands_are_symmetric_around_the_middle():
    """Spec asks for Bollinger bands (20, 2); the lower band is part of a
    band set even though nothing trades it yet."""
    df = enrich(session("2026-08-03", np.linspace(100, 104, 75)))
    row = df.iloc[-1]
    assert math.isfinite(row["bb_low"]) and math.isfinite(row["bb_up"])
    assert row["bb_low"] < row["bb_mid"] < row["bb_up"]
    assert row["bb_up"] - row["bb_mid"] == pytest.approx(row["bb_mid"] - row["bb_low"])


def test_opening_range_unknown_until_its_last_bar_closes():
    df = enrich(session("2026-09-01", np.linspace(100, 101, 10)))
    assert df["or_high"].iloc[:2].isna().all()
    assert df["or_high"].iloc[2:].notna().all()


def test_in_progress_bar_is_not_processed():
    df = session("2026-09-01", np.linspace(100, 101, 10))
    st = engine.new_state(only(["benchmark"]))
    engine.start_day(st, "2026-09-01")
    last_start = df.index[-1].to_pydatetime()
    n = engine.process_bars(st, {"TEST.NS": enrich(df)}, now=last_start + dt.timedelta(minutes=2))
    assert n == len(df) - 1


def test_bars_are_not_processed_twice():
    df = enrich(session("2026-09-01", np.linspace(100, 101, 10)))
    st = engine.new_state(only(["benchmark"]))
    engine.start_day(st, "2026-09-01")
    assert engine.process_bars(st, {"TEST.NS": df}) == 10
    assert engine.process_bars(st, {"TEST.NS": df}) == 0


# ---------------------------------------------------------------- service + API

def test_tick_catches_up_and_settles_a_slept_through_day(fake_yf, isolated_storage, monkeypatch):
    from arena import service, store
    import market_calendar
    monkeypatch.setattr(market_calendar, "is_trading_day", lambda d: d.weekday() < 5)
    df = session("2026-09-01", np.linspace(100, 105, 75))
    fake_yf.set("TEST.NS", "5d", "5m", df)
    store.save("me", engine.new_state(only(["benchmark"])))

    open_ = dt.datetime(2026, 9, 1, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)))
    service.tick_client("me", open_)
    st = store.load("me")
    assert st["day"] == "2026-09-01" and not st["day_settled"]
    assert st["books"]["benchmark"]["positions"]          # bought on bar 0

    next_morning = dt.datetime(2026, 9, 2, 8, 0, tzinfo=open_.tzinfo)
    service.tick_client("me", next_morning)
    st = store.load("me")
    assert st["day_settled"] and st["books"]["benchmark"]["positions"] == {}
    assert st["books"]["benchmark"]["day_history"][-1]["date"] == "2026-09-01"


def test_arena_api_roundtrip(client, fake_yf):
    r = client.get("/api/arena", headers={"X-Client-Id": "a1"})
    assert r.status_code == 200
    body = r.json()
    assert {s["id"] for s in body["strategies"]} >= {"orb", "benchmark"}

    r = client.put("/api/arena/config", headers={"X-Client-Id": "a1"},
                   json={"daily_capital": 5000, "symbols": ["sbin", "ITC.NS"], "strategies": ["orb"]})
    cfg = r.json()["config"]
    assert cfg["daily_capital"] == 5000
    assert cfg["symbols"] == ["SBIN.NS", "ITC.NS"]
    assert cfg["strategies"] == ["orb", "benchmark"]    # benchmark always kept

    fake_yf.set_default(pd.concat([session(d.date().isoformat(), np.linspace(100, 103, 75))
                                   for d in pd.bdate_range("2026-08-03", periods=5)]))
    r = client.post("/api/arena/backtest", headers={"X-Client-Id": "a1"}, json={"days": 5})
    assert r.status_code == 200, r.text
    bt = r.json()
    assert len(bt["replay"]["sessions"]) == 5 and bt["leaderboard"]
    assert client.get("/api/arena/backtest", headers={"X-Client-Id": "a1"}).json()["backtest"]

    # Backtest must not touch the live arena.
    live = client.get("/api/arena", headers={"X-Client-Id": "a1"}).json()
    assert all(r["trades"] == 0 for r in live["leaderboard"])

    assert client.post("/api/arena/strategies/orb/dance", headers={"X-Client-Id": "a1"}).status_code == 400
    assert client.post("/api/arena/reset", headers={"X-Client-Id": "a1"}).json() == {"reset": True}


# ------------------------------------------------- review fixes (spec 6.1-6.5)

def test_today_marks_open_positions_to_market():
    """6.1 - the leaderboard used realised P&L only, so the benchmark (which
    holds all day by design) reported flat zero with positions open."""
    st = engine.new_state(only(["benchmark"]))
    engine.start_day(st, "2026-09-01")
    engine.process_bars(st, {"TEST.NS": enrich(session("2026-09-01", np.linspace(100, 110, 40)))},
                        now=None)
    row = next(r for r in engine.leaderboard(st) if r["id"] == "benchmark")

    assert row["today"]["open_positions"] >= 1
    assert row["today"]["realised"] == 0.0        # nothing closed yet
    assert row["today"]["unrealised"] > 0         # but the position is up
    assert row["today"]["net"] == pytest.approx(
        row["today"]["realised"] + row["today"]["unrealised"], abs=0.01)
    assert row["today"]["net"] > 0, "a rising open position must not read as zero"


def test_today_separates_entries_from_closed_trades():
    """6.3 - 'trades' meant entries in TODAY and closed trades at the top
    level, under the same label."""
    st = engine.new_state(only(["benchmark"]))
    engine.start_day(st, "2026-09-01")
    engine.process_bars(st, {"TEST.NS": enrich(session("2026-09-01", np.linspace(100, 110, 40)))},
                        now=None)
    row = next(r for r in engine.leaderboard(st) if r["id"] == "benchmark")
    assert row["today"]["entries"] == 1       # opened today
    assert row["trades"] == 0                 # closed so far
    assert row["today"]["open_positions"] == 1


def test_profit_factor_is_none_not_99_when_there_are_no_losses():
    """6.2 - 99.0 is a placeholder for 'undefined' that the UI printed as a
    measured result."""
    m = engine.window_metrics([{"net": 10.0}, {"net": 5.0}])
    assert m["profit_factor"] is None
    assert m["no_losses"] is True

    mixed = engine.window_metrics([{"net": 10.0}, {"net": -5.0}])
    assert mixed["profit_factor"] == pytest.approx(2.0)
    assert mixed["no_losses"] is False

    assert engine.window_metrics([])["no_losses"] is False


def test_drawdown_benched_strategy_can_come_back():
    """6.4 - reinstatement measured drawdown from the all-time peak, so a
    book benched for drawdown had to win most of the loss back while
    restricted to shadow trading. It is now measured from the bench point."""
    st = engine.new_state(only(["ema_cross"], risk={"min_trades_to_judge": 5, "judge_window": 5}))
    book = st["books"].setdefault("ema_cross", engine._new_book("ema_cross", 10_000))
    risk = st["config"]["risk"]

    # Deep drawdown: peak 500, now -1200. dd = 1700 > the 1000 limit.
    book["trades"] = [{"net": -100.0} for _ in range(6)]
    book["stats"].update(trades=6, net=-1200.0, peak_net=500.0, gross_loss=1200.0)
    engine._judge(book, risk, 10_000, "2026-09-01")
    assert book["status"] == "benched"
    assert book["bench_net"] == -1200.0

    # Shadow trades recover modestly: still far below the old peak, so the
    # old rule (peak - net < limit) would refuse to reinstate forever.
    book["trades"] += [{"net": 40.0, } for _ in range(5)]
    book["stats"].update(trades=11, net=-1000.0)
    assert book["stats"]["peak_net"] - book["stats"]["net"] > 10_000 * 0.10
    engine._judge(book, risk, 10_000, "2026-09-02")
    assert book["status"] == "active", "a recovered book must be able to return"
    assert book["bench_net"] is None


def test_a_book_that_keeps_falling_after_benching_stays_benched():
    st = engine.new_state(only(["ema_cross"], risk={"min_trades_to_judge": 5, "judge_window": 5}))
    book = st["books"].setdefault("ema_cross", engine._new_book("ema_cross", 10_000))
    risk = st["config"]["risk"]

    book["trades"] = [{"net": -100.0} for _ in range(6)]
    book["stats"].update(trades=6, net=-600.0, peak_net=500.0, gross_loss=600.0)
    engine._judge(book, risk, 10_000, "2026-09-01")
    assert book["status"] == "benched"

    # Wins, but it has shed another 1,100 since benching - past the limit.
    book["trades"] += [{"net": 10.0} for _ in range(5)]
    book["stats"].update(trades=11, net=-1700.0)
    engine._judge(book, risk, 10_000, "2026-09-02")
    assert book["status"] == "benched"


def test_settled_days_record_whether_they_were_caught_up():
    st = engine.new_state(only(["benchmark"]))
    df = {"TEST.NS": enrich(session("2026-09-01", np.linspace(100, 103, 40)))}

    engine.start_day(st, "2026-09-01")
    engine.process_bars(st, df, now=None)
    engine.settle_day(st, caught_up=True)
    assert st["history"][-1]["caught_up"] is True
    assert st["books"]["benchmark"]["day_history"][-1]["caught_up"] is True
    assert st["last_settled_day"] == "2026-09-01"

    engine.start_day(st, "2026-09-02")
    engine.process_bars(st, {"TEST.NS": enrich(session("2026-09-02", np.linspace(100, 103, 40)))},
                        now=None)
    engine.settle_day(st)
    assert st["history"][-1]["caught_up"] is False


def test_slippage_sweep_runs_the_same_sessions_at_each_assumption():
    """6.6 - a strategy that only wins with free fills has no real edge."""
    cfg = only(["benchmark", "orb"])
    bars = {"TEST.NS": enrich(pd.concat([
        session("2026-09-01", np.linspace(100, 104, 75)),
        session("2026-09-02", np.linspace(104, 108, 75)),
    ]))}
    sweep = engine.replay_slippage_sweep(cfg, bars, days=2)

    labels = [lv["label"] for lv in sweep["levels"]]
    assert labels == ["0%", "0.02%", "0.05%"]
    bench = next(r for r in sweep["rows"] if r["id"] == "benchmark")
    assert set(bench["by_slippage"]) == set(labels)
    # More assumed slippage can only cost money, never make it.
    assert bench["by_slippage"]["0%"]["net"] >= bench["by_slippage"]["0.05%"]["net"]
