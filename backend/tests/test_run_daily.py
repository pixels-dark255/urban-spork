"""The scheduled runner: the job that actually advances the arenas.

It is short-lived and unattended, which changes what matters. Nobody is
watching when it is killed by a timeout halfway through a catch-up, so
finished sessions have to be on disk before the next one starts. Nobody is
watching when Yahoo refuses it either, so a session with no data must be
left alone rather than settled empty - settling it would record a day of
zero trades for every strategy and lose the real session for good.
"""
import datetime as dt

import numpy as np
import pandas as pd
import pytest

import run_daily
import storage
from arena import engine, markets, store
from arena import service as arena_service
from arena.strategies import enrich

IST = markets.IST


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ARENA_PATH", str(tmp_path / "arena.json"))
    return tmp_path


def make_arena(client, market_id, symbols=None, last_settled=None, window=None):
    market = markets.get(market_id)
    cfg = {"market": market_id, "symbols": symbols or ["TEST.NS"],
           "daily_capital": 10_000.0, "window": window or market.default_window()}
    state = engine.new_state(cfg)
    if last_settled:
        state["day"], state["day_settled"] = last_settled, True
        state["last_settled_day"] = last_settled
    key = store.key(client, market_id)
    store.save(key, state)
    return key


def bars_for(market_id, dates, window=None):
    market = markets.get(market_id)
    window = window or market.default_window()
    frames = []
    for date in dates:
        start, end = markets.active_session(market, window, dt.date.fromisoformat(date))
        idx = pd.date_range(start.astimezone(dt.timezone.utc),
                            (end - dt.timedelta(minutes=5)).astimezone(dt.timezone.utc),
                            freq="5min")
        px = np.linspace(100, 104, len(idx))
        frames.append(pd.DataFrame({"Open": px, "High": px * 1.002, "Low": px * 0.998,
                                    "Close": px, "Volume": np.full(len(idx), 10_000.0)},
                                   index=idx))
    return enrich(pd.concat(frames), market=market, window=window)


def stub_fetch(monkeypatch, frame, failed=()):
    """Serve the same frame for every symbol, minus any named as failing."""
    def fake(symbols, period=None, market=None, window=None, fx=None):
        ok = [s for s in symbols if s not in failed]
        return ({s: frame for s in ok},
                {"at": "x", "symbols_ok": ok, "symbols_failed": list(failed),
                 "latest_bar_at": "2026-10-09T15:25:00+05:30"})
    monkeypatch.setattr(arena_service, "_fetch_enriched", fake)
    monkeypatch.setattr(arena_service, "fetch_fx",
                        lambda m, now=None: markets.fx_for_session(m, [], {"USDINR=X": 88.0}))


# ---------------------------------------------------------------- safety

def test_it_refuses_the_file_backend(db, monkeypatch, capsys):
    """In CI a file backend means every settled session is written to a
    container that is deleted when the job ends: it would all appear to
    work and nothing would ever be saved."""
    monkeypatch.setattr(storage, "_pg_pool", None)
    assert run_daily.main(["--market", "all"]) == 2
    err = capsys.readouterr().err
    assert "JSON-file backend" in err and "DATABASE_URL" in err


def test_allow_file_is_the_way_round_it(db, monkeypatch):
    monkeypatch.setattr(storage, "_pg_pool", None)
    assert run_daily.main(["--allow-file"]) == 0        # nothing stored yet


def test_nothing_to_do_is_a_success(db, monkeypatch):
    """A quiet run is the normal case - it fires every two hours and most
    of those have nothing due. A red X then would be noise."""
    monkeypatch.setattr(storage, "_pg_pool", None)
    make_arena("u1", "nse", last_settled="2026-10-09")
    stub_fetch(monkeypatch, bars_for("nse", ["2026-10-09"]))
    assert run_daily.main(["--allow-file", "--now", "2026-10-09T18:00:00+05:30"]) == 0


# ---------------------------------------------------------------- selection

def test_market_filter_touches_only_that_market(db, monkeypatch):
    for market_id in ("nse", "us", "forex", "crypto"):
        make_arena("u1", market_id)
    keys = run_daily.arena_keys("us")
    assert keys == [store.key("u1", "us")]
    assert len(run_daily.arena_keys("all")) == 4


def test_backtest_results_are_not_arenas(db, monkeypatch):
    make_arena("u1", "nse")
    store.save(arena_service.BACKTEST_PREFIX + store.key("u1", "nse"), {"replay": {}})
    assert run_daily.arena_keys("all") == [store.key("u1", "nse")]


def test_arenas_run_in_a_fixed_order(db):
    """A run that hits its time budget must always get through the same
    arenas first, or one at the back of an unordered set could go days
    without being advanced."""
    for market_id in ("crypto", "nse", "forex", "us"):
        make_arena("u1", market_id)
    markets_in_order = [store.split_key(k)[1] for k in run_daily.arena_keys("all")]
    assert markets_in_order == ["nse", "us", "forex", "crypto"]
    assert run_daily.arena_keys("all") == run_daily.arena_keys("all")


# ---------------------------------------------------------------- resilience

def test_finished_sessions_survive_a_crash_midway(db, monkeypatch):
    """The failure that made Render redo the whole catch-up after every
    restart: saving once at the end throws away everything replayed so far.
    Sessions 1-2 must stay settled, and the next run must finish 3-5
    without re-settling the first two."""
    week = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09"]
    key = make_arena("u1", "nse", last_settled="2026-10-02")
    monkeypatch.setattr(storage, "_pg_pool", None)
    stub_fetch(monkeypatch, bars_for("nse", week))

    real_settle = engine.settle_day
    calls = {"n": 0}

    def exploding(state, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("killed by the job timeout")
        return real_settle(state, *a, **kw)

    monkeypatch.setattr(engine, "settle_day", exploding)
    assert run_daily.main(["--allow-file", "--now", "2026-10-09T18:00:00+05:30"]) == 1

    partial = [h["date"] for h in store.load(key)["history"]]
    assert partial == week[:2], f"the first two must be on disk, got {partial}"

    # The next run finishes the job, and does not redo what is done.
    monkeypatch.setattr(engine, "settle_day", real_settle)
    assert run_daily.main(["--allow-file", "--now", "2026-10-09T18:05:00+05:30"]) == 0
    done = [h["date"] for h in store.load(key)["history"]]
    assert done == week, f"expected every session exactly once, got {done}"
    assert len(done) == len(set(done)), "no session may be settled twice"


def test_a_session_with_no_data_is_left_for_the_next_run(db, monkeypatch):
    """Settling it would write a day of zero trades for every strategy and
    mark it done - losing a real session whose bars Yahoo still has."""
    key = make_arena("u1", "nse", last_settled="2026-10-08")
    monkeypatch.setattr(storage, "_pg_pool", None)
    stub_fetch(monkeypatch, bars_for("nse", ["2026-10-09"]), failed=("TEST.NS",))

    assert run_daily.main(["--allow-file", "--now", "2026-10-09T18:00:00+05:30"]) == 1
    state = store.load(key)
    assert state["history"] == [], "nothing may be settled without data"
    assert state["last_tick"]["settled_nothing"] is True
    assert state["last_tick"]["symbols_failed"] == ["TEST.NS"]
    assert state["last_tick_at"], "the attempt is still recorded"

    # Data comes back: the session settles normally.
    stub_fetch(monkeypatch, bars_for("nse", ["2026-10-09"]))
    assert run_daily.main(["--allow-file", "--now", "2026-10-09T18:05:00+05:30"]) == 0
    assert [h["date"] for h in store.load(key)["history"]] == ["2026-10-09"]


def test_a_partial_failure_settles_but_records_what_was_missing(db, monkeypatch):
    key = make_arena("u1", "nse", symbols=["TEST.NS", "GONE.NS"],
                     last_settled="2026-10-08")
    monkeypatch.setattr(storage, "_pg_pool", None)
    stub_fetch(monkeypatch, bars_for("nse", ["2026-10-09"]), failed=("GONE.NS",))

    assert run_daily.main(["--allow-file", "--now", "2026-10-09T18:00:00+05:30"]) == 0
    state = store.load(key)
    day = state["history"][0]
    assert day["date"] == "2026-10-09"
    # Settled on less than the arena was configured to trade, and saying so.
    assert day["missing_symbols"] == ["GONE.NS"]
    book = next(iter(state["books"].values()))
    assert book["day_history"][0]["missing_symbols"] == ["GONE.NS"]


def test_the_time_budget_stops_starting_new_arenas(db, monkeypatch):
    for client in ("u1", "u2", "u3"):
        make_arena(client, "nse")
    monkeypatch.setattr(storage, "_pg_pool", None)

    started = []

    def slow(key, now):
        started.append(key)
        # Push the clock past the budget after the first arena.
        base = run_daily.timeutil.utc_now
        monkeypatch.setattr(run_daily.timeutil, "utc_now",
                            lambda: base() + dt.timedelta(minutes=30))
        return {"key": key, "client": key, "market": "nse", "settled": [],
                "caught_up": 0, "symbols_failed": [], "latest_bar_at": None,
                "net": 0, "currency": "INR", "error": None, "skipped": None}

    monkeypatch.setattr(run_daily, "advance", slow)
    assert run_daily.main(["--allow-file", "--max-minutes", "1"]) == 0
    assert len(started) == 1, "it must stop starting arenas, not race through them"


def test_an_arena_that_raises_fails_the_run_but_not_the_others(db, monkeypatch):
    make_arena("u1", "nse")
    make_arena("u2", "nse")
    monkeypatch.setattr(storage, "_pg_pool", None)

    def boom(key, now=None):
        if "u1" in key:
            raise RuntimeError("bad state")

    monkeypatch.setattr(arena_service, "tick_client", boom)
    assert run_daily.main(["--allow-file"]) == 1        # red, as it should be


def test_a_disabled_arena_is_skipped_not_ticked(db, monkeypatch):
    key = make_arena("u1", "nse")
    state = store.load(key)
    state["config"]["enabled"] = False
    store.save(key, state)
    monkeypatch.setattr(storage, "_pg_pool", None)
    monkeypatch.setattr(arena_service, "tick_client",
                        lambda *a, **k: pytest.fail("a disabled arena must not tick"))
    assert run_daily.main(["--allow-file"]) == 0


# ---------------------------------------------------------------- reporting

def test_the_summary_is_written_even_when_nothing_happened(db, monkeypatch, tmp_path):
    """A silent run is indistinguishable from a broken one."""
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(storage, "_pg_pool", None)
    assert run_daily.main(["--allow-file"]) == 0
    text = summary.read_text()
    assert "## Arena runner" in text and "No arenas stored yet" in text


def test_the_summary_tabulates_each_arena(db, monkeypatch, tmp_path):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(storage, "_pg_pool", None)
    make_arena("u1", "nse", last_settled="2026-10-08")
    stub_fetch(monkeypatch, bars_for("nse", ["2026-10-09"]))
    assert run_daily.main(["--allow-file", "--now", "2026-10-09T18:00:00+05:30"]) == 0

    text = summary.read_text()
    assert "| arena | market | settled |" in text
    assert "2026-10-09" in text and "nse" in text


def test_a_bad_now_is_rejected_rather_than_silently_meaning_today():
    with pytest.raises(SystemExit):
        run_daily.main(["--allow-file", "--now", "yesterday-ish"])
