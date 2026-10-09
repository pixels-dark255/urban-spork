"""Waking up: the free-tier Render problem.

The arena ran on a server that sleeps after ~15 minutes without visitors.
While it slept the scheduler did not run, so the session was never opened
and whole trading days were skipped - permanently, because only the single
still-open day was ever caught up.

These tests cover the fix: tick on wake, replay every missed session in
order, and tell the user honestly what happened.
"""
import datetime as dt

import numpy as np
import pandas as pd
import pytest

import market_calendar
import timeutil
from arena import engine, service, store
from arena.strategies import enrich

IST = "Asia/Kolkata"

def session_df(date: str, closes, volume=1000.0):
    closes = np.asarray(closes, dtype=float)
    idx = pd.date_range(f"{date} 09:15", periods=len(closes), freq="5min", tz=IST)
    o = np.r_[closes[0], closes[:-1]]
    return pd.DataFrame({"Open": o, "High": np.maximum(o, closes) + 0.2,
                         "Low": np.minimum(o, closes) - 0.2, "Close": closes,
                         "Volume": np.full(len(closes), volume)}, index=idx)

def multi_session(dates, n=75):
    """One enriched frame spanning several sessions, as Yahoo returns."""
    frames = [session_df(d, np.linspace(100, 103, n)) for d in dates]
    return enrich(pd.concat(frames))

def ist_at(date: str, hh: int, mm: int) -> dt.datetime:
    return dt.datetime.fromisoformat(f"{date}T{hh:02d}:{mm:02d}:00").replace(
        tzinfo=timeutil.IST).astimezone(dt.timezone.utc)

@pytest.fixture
def arena_client(isolated_storage, monkeypatch):
    """A configured arena with no network behind it."""
    ip = "waker"
    cfg = {"symbols": ["TEST.NS"], "strategies": ["orb", "benchmark"], "daily_capital": 10_000}
    state = engine.new_state(cfg)
    store.save(ip, state)
    return ip

def stub_bars(monkeypatch, frame):
    """Replace the fetch, and record what period the service asked for."""
    calls = []

    def fake(symbols, period=None):
        calls.append(period)
        return ({s: frame for s in symbols},
                {"at": timeutil.iso_now(), "symbols_ok": list(symbols),
                 "symbols_failed": [], "latest_bar_at": None})

    monkeypatch.setattr(service, "_fetch_enriched", fake)
    return calls

# --------------------------------------------------------------- startup tick

def test_arena_job_is_scheduled_to_run_immediately(monkeypatch):
    """A free instance is started by the first visitor after a sleep. With
    the default first run a whole interval away, that visitor sees an empty
    arena and leaves before anything happens."""
    import scheduler

    jobs = {}
    monkeypatch.setattr(scheduler.scheduler, "add_job",
                        lambda fn, *a, **kw: jobs.__setitem__(kw.get("id"), kw))
    monkeypatch.setattr(scheduler.scheduler, "start", lambda: None)
    monkeypatch.setattr(type(scheduler.scheduler), "running", property(lambda self: False))
    scheduler.start_scheduler()

    assert "arena_tick" in jobs
    arena = jobs["arena_tick"]
    assert arena["max_instances"] == 1 and arena["coalesce"] is True
    first_run = arena["next_run_time"]
    assert first_run is not None
    age = (timeutil.utc_now() - timeutil.ensure_aware(first_run)).total_seconds()
    assert abs(age) < 60, "arena_tick should run on startup, not an interval later"

# ------------------------------------------------------- due / background tick

def test_is_due_false_for_a_brand_new_arena_outside_hours(arena_client):
    saturday = ist_at("2026-10-03", 11, 0)        # weekend
    assert service.is_due(arena_client, saturday) is False

def test_is_due_true_when_a_session_should_be_open(arena_client):
    weekday = ist_at("2026-10-01", 11, 0)
    assert service.is_due(arena_client, weekday) is True

def test_is_due_true_when_a_past_day_is_unsettled(arena_client):
    state = store.load(arena_client)
    state["day"], state["day_settled"] = "2026-09-28", False
    store.save(arena_client, state)
    assert service.is_due(arena_client, ist_at("2026-10-03", 11, 0)) is True

def test_get_arena_starts_a_background_tick_when_due(client, monkeypatch):
    started = {}
    monkeypatch.setattr(service, "is_due", lambda ip, now=None: True)
    monkeypatch.setattr(service, "tick_client",
                        lambda ip, now=None: started.setdefault("ran", True))
    body = client.get("/api/arena", headers={"X-Client-Id": "bg1"}).json()
    assert body["tick_started"] is True
    for _ in range(100):                       # the thread is daemonised
        if started.get("ran"):
            break
        import time; time.sleep(0.02)
    assert started.get("ran") is True

def test_get_arena_does_not_tick_when_not_due(client, monkeypatch):
    monkeypatch.setattr(service, "is_due", lambda ip, now=None: False)
    monkeypatch.setattr(service, "tick_client",
                        lambda ip, now=None: pytest.fail("should not have ticked"))
    assert client.get("/api/arena", headers={"X-Client-Id": "bg2"}).json()["tick_started"] is False

def test_only_one_background_tick_runs_at_a_time(arena_client, monkeypatch):
    """Each poll of a slow page would otherwise start another catch-up over
    the same sessions."""
    import threading, time

    monkeypatch.setattr(service, "is_due", lambda ip, now=None: True)
    running = threading.Event()
    release = threading.Event()
    calls = []

    def slow(ip, now=None):
        calls.append(1)
        running.set()
        release.wait(timeout=5)

    monkeypatch.setattr(service, "tick_client", slow)
    assert service.tick_in_background(arena_client) is True
    running.wait(timeout=5)
    assert service.tick_in_background(arena_client) is False   # already running
    release.set()
    time.sleep(0.1)
    assert len(calls) == 1

# ------------------------------------------------------------ multi-day catch-up

def test_catches_up_every_missed_session_in_order(arena_client, monkeypatch):
    """The headline case: last run Monday, next wake Friday 16:00. Tue, Wed,
    Thu and Fri must all be settled, in date order."""
    week = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09"]  # Mon-Fri
    for d in week:
        assert market_calendar.is_trading_day(dt.date.fromisoformat(d))

    state = store.load(arena_client)
    state["day"], state["day_settled"], state["last_settled_day"] = week[0], True, week[0]
    store.save(arena_client, state)

    stub_bars(monkeypatch, multi_session(week))
    service.tick_client(arena_client, ist_at(week[-1], 16, 0))

    state = store.load(arena_client)
    settled = [h["date"] for h in state["history"]]
    assert settled == week[1:], f"expected Tue-Fri settled in order, got {settled}"
    assert state["last_settled_day"] == week[-1]
    assert state["caught_up_last"] == 4
    assert all(h["caught_up"] for h in state["history"])

def test_catch_up_skips_weekends_and_holidays(arena_client, monkeypatch):
    """Friday to the next Thursday, with Monday a real NSE holiday."""
    holiday = dt.date(2026, 10, 2)                       # Mahatma Gandhi Jayanti
    assert market_calendar.is_trading_holiday(holiday)

    state = store.load(arena_client)
    state["day"], state["day_settled"], state["last_settled_day"] = "2026-10-01", True, "2026-10-01"
    store.save(arena_client, state)

    expected = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08"]   # Mon-Thu
    stub_bars(monkeypatch, multi_session(expected))
    service.tick_client(arena_client, ist_at("2026-10-08", 16, 0))

    settled = [h["date"] for h in store.load(arena_client)["history"]]
    assert settled == expected
    assert "2026-10-02" not in settled      # holiday
    assert "2026-10-03" not in settled      # Saturday
    assert "2026-10-04" not in settled      # Sunday

def test_catch_up_is_idempotent(arena_client, monkeypatch):
    """Two visitors hitting a just-woken server must not double-settle."""
    week = ["2026-10-05", "2026-10-06", "2026-10-07"]
    state = store.load(arena_client)
    state["day"], state["day_settled"], state["last_settled_day"] = week[0], True, week[0]
    store.save(arena_client, state)

    stub_bars(monkeypatch, multi_session(week))
    at = ist_at(week[-1], 16, 0)
    service.tick_client(arena_client, at)
    first = store.load(arena_client)["history"]

    service.tick_client(arena_client, at)
    second = store.load(arena_client)["history"]
    assert [h["date"] for h in second] == [h["date"] for h in first]
    assert store.load(arena_client)["caught_up_last"] == 0

def test_a_brand_new_arena_does_not_backfill(arena_client, monkeypatch):
    """No history means no missed days - start from today, don't invent a
    month of trades the user never ran."""
    state = store.load(arena_client)
    assert state["last_settled_day"] is None and state["day"] is None

    stub_bars(monkeypatch, multi_session(["2026-10-05", "2026-10-06", "2026-10-07"]))
    service.tick_client(arena_client, ist_at("2026-10-07", 16, 0))

    state = store.load(arena_client)
    assert [h["date"] for h in state["history"]] == ["2026-10-07"]
    assert state["caught_up_last"] == 0

def test_catch_up_is_capped(arena_client, monkeypatch):
    state = store.load(arena_client)
    state["day"], state["day_settled"], state["last_settled_day"] = "2025-01-02", True, "2025-01-02"
    store.save(arena_client, state)
    missed = service._missed_sessions(state, ist_at("2026-10-09", 16, 0).astimezone(timeutil.IST))
    assert len(missed) <= service.MAX_CATCHUP_SESSIONS

def test_a_long_gap_asks_for_the_wider_history_window(arena_client, monkeypatch):
    """The 5-day intraday fetch cannot cover a two-week sleep."""
    state = store.load(arena_client)
    state["day"], state["day_settled"], state["last_settled_day"] = "2026-09-21", True, "2026-09-21"
    store.save(arena_client, state)

    periods = stub_bars(monkeypatch, multi_session(["2026-10-05", "2026-10-06"]))
    service.tick_client(arena_client, ist_at("2026-10-09", 16, 0))
    assert periods and periods[0] == service.REPLAY_PERIOD

def test_a_short_gap_uses_the_normal_intraday_fetch(arena_client, monkeypatch):
    state = store.load(arena_client)
    state["day"], state["day_settled"], state["last_settled_day"] = "2026-10-07", True, "2026-10-07"
    store.save(arena_client, state)

    periods = stub_bars(monkeypatch, multi_session(["2026-10-08", "2026-10-09"]))
    service.tick_client(arena_client, ist_at("2026-10-09", 16, 0))
    assert periods and periods[0] is None

def test_today_is_not_settled_while_the_session_is_still_running(arena_client, monkeypatch):
    today = "2026-10-09"
    state = store.load(arena_client)
    state["day"], state["day_settled"], state["last_settled_day"] = "2026-10-08", True, "2026-10-08"
    store.save(arena_client, state)

    stub_bars(monkeypatch, multi_session([today]))
    service.tick_client(arena_client, ist_at(today, 12, 0))     # mid-session

    state = store.load(arena_client)
    assert state["day"] == today and state["day_settled"] is False
    assert today not in [h["date"] for h in state["history"]]

# ---------------------------------------------------------------- visibility

def test_storage_backend_is_reported(arena_client, monkeypatch):
    body = service.view(arena_client)
    assert body["storage"]["backend"] == "json_file"
    assert body["storage"]["ephemeral"] is True

def test_storage_backend_reads_postgres_when_the_pool_exists(monkeypatch):
    """Only the flag is under test - a real pool is not needed to know which
    backend the arena would be using."""
    monkeypatch.setattr(store.storage, "_pg_pool", object())
    assert store.backend_name() == "postgres"

def test_freshness_reports_last_tick_and_failures(arena_client, monkeypatch):
    stub = multi_session(["2026-10-09"])

    def fake(symbols, period=None):
        return ({"TEST.NS": stub},
                {"at": timeutil.iso_now(), "symbols_ok": ["TEST.NS"],
                 "symbols_failed": ["BROKEN.NS"], "latest_bar_at": "2026-10-09T11:05:00+05:30"})

    monkeypatch.setattr(service, "_fetch_enriched", fake)
    service.tick_client(arena_client, ist_at("2026-10-09", 12, 0))

    fresh = service.view(arena_client)["freshness"]
    assert fresh["last_tick_at"] is not None
    assert fresh["symbols_failed"] == ["BROKEN.NS"]
    assert fresh["symbols_ok"] == ["TEST.NS"]
    assert fresh["latest_bar_at"] == "2026-10-09T11:05:00+05:30"
    assert fresh["age_minutes"] is not None

def test_freshness_flags_a_stale_tick_during_market_hours(arena_client, monkeypatch):
    state = store.load(arena_client)
    state["last_tick_at"] = timeutil.to_iso(timeutil.utc_now() - dt.timedelta(hours=3))
    store.save(arena_client, state)
    monkeypatch.setattr(market_calendar, "is_market_open", lambda now=None: True)
    assert service.freshness(store.load(arena_client))["stale"] is True

def test_freshness_is_not_stale_when_the_market_is_shut(arena_client, monkeypatch):
    monkeypatch.setattr(market_calendar, "is_market_open", lambda now=None: False)
    assert service.freshness(store.load(arena_client))["stale"] is False

def test_fetch_diagnostics_record_failed_symbols(monkeypatch):
    import data_sources

    def fake_download(sym, *a, **k):
        if sym == "BAD.NS":
            raise RuntimeError("rate limited")
        return session_df("2026-10-09", np.linspace(100, 101, 30))

    monkeypatch.setattr(data_sources, "fetch_intraday_bars", fake_download)
    bars, diag = service._fetch_enriched(["GOOD.NS", "BAD.NS"])
    assert list(bars) == ["GOOD.NS"]
    assert diag["symbols_ok"] == ["GOOD.NS"]
    assert diag["symbols_failed"] == ["BAD.NS"]
    assert diag["latest_bar_at"] is not None
