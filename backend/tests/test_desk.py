"""The desk: capital accounting, and the session payload the replay reads.

The point of the capital numbers is to show money that is NOT working.
A strategy that made Rs200 on a quarter of the book and one that made
Rs200 on all of it look identical on a net-rupees leaderboard, and these
are the numbers that tell them apart.
"""
import pytest

from arena import engine, markets as markets_mod
from arena import service as service_mod
from arena.strategies import enrich
from validation import arena_guard

SEED = 20260325


def _state(capital=50000.0, sessions=6):
    market = markets_mod.get("nse")
    bars = {sym: enrich(arena_guard.intraday_bars(sessions=sessions, seed=SEED + i),
                        market=market)
            for i, sym in enumerate(["A.NS", "B.NS", "C.NS", "D.NS"])}
    cfg = {"market": "nse", "symbols": sorted(bars), "daily_capital": capital}
    return engine.replay(cfg, bars, days=sessions)


# --- peak_deployed ----------------------------------------------------------

def test_peak_is_the_most_held_at_once_not_the_sum_of_everything():
    trades = [
        {"qty": 1, "entry_price": 100, "entry_at": "T1", "exit_at": "T4"},
        {"qty": 1, "entry_price": 200, "entry_at": "T2", "exit_at": "T3"},
        {"qty": 1, "entry_price": 50, "entry_at": "T5", "exit_at": "T6"},
    ]
    # 100 and 200 overlap; the 50 comes after both are closed.
    assert engine.peak_deployed(trades) == 300.0


def test_a_slot_freed_and_refilled_on_the_same_bar_is_not_counted_twice():
    trades = [
        {"qty": 1, "entry_price": 100, "entry_at": "T1", "exit_at": "T2"},
        {"qty": 1, "entry_price": 100, "entry_at": "T2", "exit_at": "T3"},
    ]
    assert engine.peak_deployed(trades) == 100.0


def test_a_position_still_open_stays_on_the_books_to_the_end():
    trades = [{"qty": 2, "entry_price": 150, "entry_at": "T1", "exit_at": None}]
    assert engine.peak_deployed(trades) == 300.0


def test_peak_of_nothing_is_zero():
    assert engine.peak_deployed([]) == 0.0
    assert engine.peak_deployed([{"qty": 0, "entry_price": 100, "entry_at": "T1"}]) == 0.0


# --- capital ----------------------------------------------------------------

def test_one_position_can_only_use_its_slots_share_of_the_book():
    state = _state(capital=50000.0)
    cap = engine.capital(state)
    assert cap["book"] == 50000.0
    assert cap["slots"] == 4
    assert cap["per_slot"] == pytest.approx(12500.0)
    assert cap["share_pct"] == pytest.approx(25.0)

    traded = [b for b in cap["books"] if b["trades"] and b["strategy"] != "benchmark"]
    assert traded, "the fixture should produce trades"
    for row in traded:
        # The cap is per position, so a book with four slots full can reach
        # the whole book; no single entry may exceed one slot by more than
        # rounding to a whole share.
        assert row["peak_deployed"] <= cap["book"] * 1.01


def test_capital_reports_the_idle_money_rather_than_only_the_net():
    state = _state(capital=50000.0)
    cap = engine.capital(state)
    row = next(b for b in cap["books"] if b["strategy"] == "bb_breakout")
    # This is the finding the panel exists for: a strategy that only ever
    # had a quarter of the book at work.
    assert 0 < row["peak_pct"] < 100
    assert row["idle"] > 0


def test_capital_ignores_a_strategy_the_user_unticked():
    state = _state()
    state["config"]["strategies"] = ["orb"]
    assert [b["strategy"] for b in engine.capital(state)["books"]] == ["orb"]


def test_capital_scales_with_the_book():
    small = engine.capital(_state(capital=10000.0))
    big = engine.capital(_state(capital=50000.0))
    assert big["per_slot"] == pytest.approx(5 * small["per_slot"])


# --- the desk payload -------------------------------------------------------

def test_desk_says_so_plainly_when_nothing_has_traded(monkeypatch):
    blank = engine.new_state({"market": "nse", "symbols": ["A.NS"]})
    monkeypatch.setattr(service_mod, "load_or_create", lambda ip: blank)
    out = service_mod.desk("ip")
    assert out["ok"] is False
    assert out["reason"] == "no_trades"
    # The capital block is still there: the book exists even with no trades.
    assert out["capital"]["book"] > 0


def test_desk_returns_one_session_of_bars_and_the_trades_inside_it(monkeypatch):
    state = _state(sessions=6)
    market = markets_mod.get("nse")
    bars = {sym: enrich(arena_guard.intraday_bars(sessions=6, seed=SEED + i), market=market)
            for i, sym in enumerate(["A.NS", "B.NS", "C.NS", "D.NS"])}

    monkeypatch.setattr(service_mod, "load_or_create", lambda ip: state)
    monkeypatch.setattr(service_mod, "_market_window", lambda s: (market, None))
    monkeypatch.setattr(service_mod, "_fetch_enriched",
                        lambda syms, *a, **kw: ({s: bars[s] for s in syms if s in bars},
                                                {"latest_bar_at": None}))
    saved = {}
    monkeypatch.setattr(service_mod.store, "load", lambda key: saved.get(key))
    monkeypatch.setattr(service_mod.store, "save",
                        lambda key, value: saved.__setitem__(key, value))

    out = service_mod.desk("ip")
    assert out["ok"] is True
    assert out["session"]
    assert out["trades"], "the last traded session should have trades"
    # Every trade in the payload belongs to the session being replayed, and
    # every bar in it is from that same day. A replay that mixed days would
    # show trades landing on bars that never produced them.
    for trade in out["trades"]:
        assert trade["entry_at"][:10] == out["session"]
        assert trade["notional"] == pytest.approx(trade["qty"] * trade["entry_price"])
    for rows in out["bars"].values():
        assert rows
        for bar in rows:
            assert bar["t"][:10] == out["session"]
    # Trades come in the order they happened, which is the order a tape plays.
    entries = [t["entry_at"] for t in out["trades"]]
    assert entries == sorted(entries)
    # The second call is served from the cache rather than fetched again.
    monkeypatch.setattr(service_mod, "_fetch_enriched",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("refetched")))
    assert service_mod.desk("ip")["session"] == out["session"]
