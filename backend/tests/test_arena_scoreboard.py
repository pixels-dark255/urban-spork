"""The long-run record: per strategy, per day, over weeks.

These are built on hand-written day_history rows rather than on a replay,
because the point of the scoreboard is the arithmetic over many sessions -
counting green days, finding the best one, ranking by daily wins - and
hand-written rows make the expected answer obvious by inspection.
"""
import pytest

from arena import engine, scoreboard, store
from arena import service as arena_service


def arena(rows_by_strategy, market="nse", capital=10_000.0):
    """An arena whose history is exactly the rows given.

    rows_by_strategy: {strategy_id: [(date, net, trades), ...]}
    """
    ids = list(rows_by_strategy)
    state = engine.new_state({"market": market, "daily_capital": capital,
                              "strategies": ids})
    dates = sorted({d for rows in rows_by_strategy.values() for d, _n, _t in rows})
    state["history"] = [{"date": d, "books": {}, "caught_up": False} for d in dates]
    for sid, rows in rows_by_strategy.items():
        book = state["books"].setdefault(sid, engine._new_book(sid, capital))
        book["day_history"] = [
            {"date": d, "net": n, "trades": t, "equity_end": capital + n,
             "status": "active", "stopped": None,
             "skipped": {"too_expensive": 0, "costs_exceed_target": 0},
             "caught_up": False, "net_inr": None, "fx_rate": None}
            for d, n, t in rows
        ]
        book["stats"]["trades"] = sum(t for _d, _n, t in rows)
    return state


def rec(board, sid):
    return next(r for r in board["strategies"] if r["id"] == sid)


D = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09"]


# ---------------------------------------------------------------- counting

def test_green_red_and_quiet_days_are_counted_separately():
    """A day the strategy found no signal is not a flat result - calling it
    one would make sitting out look like a decision that broke even."""
    state = arena({"ema_cross": [(D[0], 50, 2), (D[1], -20, 1), (D[2], 0, 0),
                                 (D[3], 30, 1), (D[4], -10, 1)]})
    r = rec(scoreboard.scoreboard(state, "all"), "ema_cross")
    assert (r["green_days"], r["red_days"], r["quiet_days"]) == (2, 2, 1)
    assert r["sessions"] == 5
    assert r["days_traded"] == 4          # the quiet day placed no trades
    assert r["net"] == 50.0
    assert r["day_win_rate"] == 50.0      # quiet days are not in the ratio
    assert r["trades"] == 5


def test_a_strategy_that_never_traded_has_no_win_rate():
    state = arena({"ema_cross": [(d, 0, 0) for d in D]})
    r = rec(scoreboard.scoreboard(state, "all"), "ema_cross")
    assert r["day_win_rate"] is None, "0 of 0 days is not a 0% win rate"
    assert r["green_days"] == r["red_days"] == 0
    assert r["quiet_days"] == 5


def test_the_best_and_worst_day_carry_their_dates():
    state = arena({"orb": [(D[0], 10, 1), (D[1], 95, 3), (D[2], -40, 2), (D[3], 5, 1)]})
    r = rec(scoreboard.scoreboard(state, "all"), "orb")
    assert r["best_day"]["date"] == D[1] and r["best_day"]["net"] == 95
    assert r["worst_day"]["date"] == D[2] and r["worst_day"]["net"] == -40
    assert r["avg_day"] == pytest.approx((10 + 95 - 40 + 5) / 4)


# ---------------------------------------------------------------- periods

def test_the_period_takes_the_most_recent_sessions():
    rows = [(f"2026-09-{d:02d}", 10, 1) for d in range(1, 21)]
    state = arena({"ema_cross": rows})
    full = scoreboard.scoreboard(state, "all")
    assert full["period"]["sessions"] == 20
    assert full["period"]["from"] == "2026-09-01" and full["period"]["to"] == "2026-09-20"

    recent = scoreboard.scoreboard(state, "5")
    assert recent["period"]["sessions"] == 5
    assert recent["period"]["from"] == "2026-09-16"
    assert rec(recent, "ema_cross")["net"] == 50.0
    assert rec(full, "ema_cross")["net"] == 200.0


def test_an_unknown_period_falls_back_rather_than_raising():
    state = arena({"ema_cross": [(D[0], 10, 1)]})
    assert scoreboard.scoreboard(state, "banana")["period"]["id"] == scoreboard.DEFAULT_PERIOD
    assert scoreboard.scoreboard(state, None)["period"]["id"] == scoreboard.DEFAULT_PERIOD


def test_an_arena_with_no_history_does_not_crash():
    state = engine.new_state({"market": "nse"})
    board = scoreboard.scoreboard(state, "all")
    assert board["period"]["sessions"] == 0
    assert board["daily"] == []
    assert board["highlights"].get("most_profitable") is None


# ---------------------------------------------------------------- streaks

def test_streaks_count_runs_and_a_quiet_day_breaks_them():
    state = arena({"ema_cross": [(D[0], 5, 1), (D[1], 5, 1), (D[2], 0, 0),
                                 (D[3], 5, 1), (D[4], 5, 1)]})
    s = rec(scoreboard.scoreboard(state, "all"), "ema_cross")["streaks"]
    assert s["current"] == 2, "the quiet day breaks the run rather than extending it"
    assert s["longest_green"] == 2
    assert s["longest_red"] == 0


def test_a_losing_run_reports_a_negative_current_streak():
    state = arena({"ema_cross": [(D[0], 50, 1), (D[1], -5, 1), (D[2], -5, 1), (D[3], -5, 1)]})
    s = rec(scoreboard.scoreboard(state, "all"), "ema_cross")["streaks"]
    assert s["current"] == -3
    assert s["longest_red"] == 3
    assert s["longest_green"] == 1


# ---------------------------------------------------------------- daily winners

def test_the_daily_winner_is_the_best_non_benchmark_strategy():
    state = arena({
        "ema_cross": [(D[0], 100, 2), (D[1], -10, 1)],
        "orb":       [(D[0], 20, 1),  (D[1], 60, 2)],
        "benchmark": [(D[0], 500, 1), (D[1], 500, 1)],
    })
    board = scoreboard.scoreboard(state, "all")
    by_date = {d["date"]: d for d in board["daily"]}
    assert by_date[D[0]]["winner"] == "ema_cross"
    assert by_date[D[1]]["winner"] == "orb"
    # The benchmark is what they are measured against, not a competitor.
    assert by_date[D[0]]["benchmark_net"] == 500
    assert rec(board, "benchmark")["daily_wins"] == 0
    assert rec(board, "ema_cross")["daily_wins"] == 1
    assert rec(board, "orb")["daily_wins"] == 1


def test_a_day_nobody_traded_has_no_winner():
    """Awarding a win for doing nothing would put a strategy top of the
    'wins most days' table on days it never placed an order."""
    state = arena({"ema_cross": [(D[0], 0, 0)], "orb": [(D[0], 0, 0)]})
    board = scoreboard.scoreboard(state, "all")
    assert board["daily"][0]["winner"] is None
    assert rec(board, "ema_cross")["daily_wins"] == 0


def test_a_day_everyone_lost_has_a_least_bad_strategy_but_no_winner():
    state = arena({"ema_cross": [(D[0], -5, 1)], "orb": [(D[0], -50, 1)]})
    board = scoreboard.scoreboard(state, "all")
    day = board["daily"][0]
    assert day["winner"] == "ema_cross" and day["winner_net"] == -5
    assert day["loser"] == "orb"
    # ...but losing least is not winning, so it earns no tally mark.
    assert rec(board, "ema_cross")["daily_wins"] == 0


def test_beating_the_benchmark_is_counted_per_day():
    state = arena({
        "ema_cross": [(D[0], 10, 1), (D[1], 10, 1), (D[2], -5, 1)],
        "benchmark": [(D[0], 5, 1),  (D[1], 50, 1), (D[2], -90, 1)],
    })
    board = scoreboard.scoreboard(state, "all")
    # Day 1 beat it, day 2 did not, day 3 beat it by losing less.
    assert rec(board, "ema_cross")["beat_benchmark_days"] == 2


# ---------------------------------------------------------------- highlights

def test_the_headline_claims_ignore_strategies_that_never_traded():
    """Zero beats a loss, so without this the 'most profitable strategy' is
    the one that sat out the whole month."""
    state = arena({
        "ema_cross": [(d, -10, 2) for d in D],      # traded, lost
        "orb":       [(d, -50, 2) for d in D],      # traded, lost more
        "bb_breakout": [(d, 0, 0) for d in D],      # never traded
    })
    h = scoreboard.scoreboard(state, "all")["highlights"]
    assert h["most_profitable"]["id"] == "ema_cross"
    assert h["most_losing_days"]["id"] == "orb"
    assert [x["id"] for x in h["idle"]] == ["bb_breakout"]


def test_most_money_and_most_winning_days_can_be_different_strategies():
    """The distinction the whole section exists for: one big day is not a
    record, and the table should not let it look like one."""
    state = arena({
        "ema_cross": [(D[0], 900, 1), (D[1], -20, 1), (D[2], -20, 1),
                      (D[3], -20, 1), (D[4], -20, 1)],
        "orb":       [(D[0], 10, 1), (D[1], 12, 1), (D[2], 11, 1),
                      (D[3], 9, 1), (D[4], 13, 1)],
    })
    h = scoreboard.scoreboard(state, "all")["highlights"]
    assert h["most_profitable"]["id"] == "ema_cross"      # one enormous day
    assert h["most_winning_days"]["id"] == "orb"          # four more green days
    assert h["most_winning_days"]["daily_wins"] == 4


def test_the_best_and_worst_single_days_name_the_strategy_and_date():
    state = arena({
        "ema_cross": [(D[0], 120, 1), (D[1], -30, 1)],
        "orb":       [(D[0], 10, 1), (D[1], -200, 1)],
    })
    h = scoreboard.scoreboard(state, "all")["highlights"]
    assert h["best_single_day"]["id"] == "ema_cross"
    assert h["best_single_day"]["date"] == D[0] and h["best_single_day"]["net"] == 120
    assert h["worst_single_day"]["id"] == "orb"
    assert h["worst_single_day"]["date"] == D[1] and h["worst_single_day"]["net"] == -200


def test_an_all_losing_arena_reports_no_best_day_rather_than_a_fake_one():
    state = arena({"ema_cross": [(D[0], -5, 1), (D[1], -9, 1)]})
    h = scoreboard.scoreboard(state, "all")["highlights"]
    assert h["best_single_day"] is None, "a loss is not a best day"
    assert h["worst_single_day"]["net"] == -9


# ---------------------------------------------------------------- currency

def test_rupee_totals_are_only_summed_when_every_day_has_a_rate():
    """A partial sum would quietly mean 'most of the period', which reads
    as a total and is not one."""
    state = arena({"ema_cross": [(D[0], 10, 1), (D[1], 10, 1)]}, market="us")
    book = state["books"]["ema_cross"]
    book["day_history"][0]["net_inr"] = 885.0
    board = scoreboard.scoreboard(state, "all")
    assert rec(board, "ema_cross")["net_inr"] is None

    book["day_history"][1]["net_inr"] = 885.0
    board = scoreboard.scoreboard(state, "all")
    assert rec(board, "ema_cross")["net_inr"] == 1770.0


def test_an_estimated_rupee_total_is_labelled_separately():
    state = arena({"ema_cross": [(D[0], 10, 1)]}, market="us")
    board = scoreboard.scoreboard(state, "all")
    assert rec(board, "ema_cross")["net_inr"] is None
    scoreboard.to_inr_totals(board, {"inr_per_unit": 88.0})
    # Never written into net_inr: it restates history at today's rate.
    assert rec(board, "ema_cross")["net_inr"] is None
    assert rec(board, "ema_cross")["net_inr_estimated"] == 880.0


def test_the_board_reports_the_markets_currency():
    for market_id, code in (("nse", "INR"), ("us", "USD"), ("forex", "USD")):
        state = arena({"ema_cross": [(D[0], 1, 1)]}, market=market_id)
        board = scoreboard.scoreboard(state, "all")
        assert board["currency"]["code"] == code
        assert board["market"] == market_id


# ---------------------------------------------------------------- service

def test_the_service_serves_a_scoreboard_per_market(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ARENA_PATH", str(tmp_path / "arena.json"))
    key = store.key("10.0.0.1", "crypto")
    arena_service.load_or_create(key)
    board = arena_service.scoreboard(key, "10")
    assert board["market"] == "crypto"
    assert board["currency"]["code"] == "USD"
    assert board["period"]["id"] == "10"
    assert [p["id"] for p in board["periods"]] == ["5", "10", "30", "90", "all"]
