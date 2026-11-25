"""Phase 6: the harness.

Most of these assert a refusal. The harness earns its place by not
trading when it should not, and every one of those cases is a state where
trading would be wrong rather than merely unprofitable.
"""
import datetime as dt

import pandas as pd
import pytest

from harness import alerts, risk, runner
from harness import store as store_mod

IST = runner.IST
TODAY = dt.datetime(2026, 11, 26, 10, 0, tzinfo=IST)      # a Thursday


def fresh_bars(session=None, tickers=("A.NS",)):
    session = session or TODAY.date()
    idx = pd.DatetimeIndex([dt.datetime.combine(session, dt.time(10, 0))])
    return {t: pd.DataFrame({"Close": [100.0]}, index=idx) for t in tickers}


def ctx(**kw):
    store = kw.pop("store", None) or store_mod.MemoryStore()
    base = dict(store=store, sink=alerts.Sink(store), now_ist=TODAY,
                equity=100000.0, day_start_equity=100000.0,
                bars=fresh_bars(), dry_run=True)
    base.update(kw)
    return runner.Context(**base)


# ----------------------------------------------------- 1. the lock

def test_a_second_concurrent_run_exits():
    store = store_mod.MemoryStore()
    assert store.acquire_lock("harness", "first", 900)
    out = runner.run(ctx(store=store))
    assert out["stopped_at"] == "lock"


def test_the_lock_is_released_so_the_next_run_can_start():
    store = store_mod.MemoryStore()
    runner.run(ctx(store=store))
    assert store.acquire_lock("harness", "someone-else", 900)


def test_an_expired_lease_can_be_taken_over():
    """A crashed runner must not block every later run forever."""
    store = store_mod.MemoryStore()
    store.acquire_lock("harness", "dead-runner", 0)
    assert store.acquire_lock("harness", "new-runner", 900)


# ------------------------------------------------ 2. trading day

def test_a_holiday_stops_the_run():
    out = runner.run(ctx(now_ist=dt.datetime(2026, 1, 26, 10, 0, tzinfo=IST)))
    assert out["stopped_at"] == "trading_day"


def test_a_weekend_stops_the_run():
    out = runner.run(ctx(now_ist=dt.datetime(2026, 11, 28, 10, 0, tzinfo=IST)))
    assert out["stopped_at"] == "trading_day"


def test_a_holiday_does_not_raise_an_alert():
    # A holiday is the expected state on a holiday. Alerting would train
    # everyone to ignore the alerts.
    store = store_mod.MemoryStore()
    sink = alerts.Sink(store)
    runner.run(ctx(store=store, sink=sink,
                   now_ist=dt.datetime(2026, 1, 26, 10, 0, tzinfo=IST)))
    assert sink.sent == []


# -------------------------------------------------- 3. reconcile

def test_a_position_mismatch_stops_and_alerts():
    store = store_mod.MemoryStore()
    store.put_position("swing", {"ticker": "A.NS", "strategy": "s", "qty": 10})
    sink = alerts.Sink(store)
    out = runner.run(ctx(store=store, sink=sink, believed_positions=[]))
    assert out["stopped_at"] == "reconcile"
    assert sink.sent and "do not match" in sink.sent[0]["text"]


def test_matching_positions_pass():
    store = store_mod.MemoryStore()
    row = {"ticker": "A.NS", "strategy": "s", "qty": 10}
    store.put_position("swing", row)
    out = runner.run(ctx(store=store, believed_positions=[row]))
    assert out["stopped_at"] is None


# ------------------------------------------------- 4. fresh data

def test_a_stale_bar_stops_the_run():
    stale = fresh_bars(session=TODAY.date() - dt.timedelta(days=5))
    out = runner.run(ctx(bars=stale))
    assert out["stopped_at"] == "fresh_data"


def test_no_data_at_all_stops_the_run():
    # An empty dict has no stale bars in it, which must not be mistaken
    # for having checked anything.
    out = runner.run(ctx(bars={}))
    assert out["stopped_at"] == "fresh_data"


def test_one_stale_ticker_among_fresh_ones_still_stops():
    bars = fresh_bars(tickers=("A.NS", "B.NS"))
    bars["B.NS"] = list(fresh_bars(session=TODAY.date() - dt.timedelta(days=3)).values())[0]
    out = runner.run(ctx(bars=bars))
    assert out["stopped_at"] == "fresh_data"


# -------------------------------------------- 6. the risk limits

def gate(orders, equity, day_start=100000.0, peak=100000.0, positions=None,
         now=None, intraday=False, store=None):
    context = risk.Context(account="swing", equity=equity,
                           day_start_equity=day_start,
                           worst_backtest_drawdown_pct=28.52,
                           positions=positions or [],
                           now_ist=now or TODAY, intraday=intraday)
    return risk.apply(orders, context, peak, store)


ENTRY = {"ticker": "A.NS", "strategy": "s", "side": "BUY", "qty": 10, "target_qty": 10}
EXIT = {"ticker": "A.NS", "strategy": "s", "side": "SELL", "qty": 4}


def test_an_order_is_capped_at_its_target():
    big = dict(ENTRY, qty=50)
    out = gate([big], 100000.0)
    assert out["orders"][0]["qty"] == 10
    assert out["actions"][0]["rule"] == "target_cap"


def test_three_percent_loss_stops_new_entries_but_not_exits():
    out = gate([ENTRY, EXIT], 96500.0)
    sides = [o["side"] for o in out["orders"]]
    assert sides == ["SELL"]
    assert any(a["rule"] == "soft_loss" for a in out["actions"])


def test_five_percent_loss_sets_the_pause_flag():
    store = store_mod.MemoryStore()
    out = gate([ENTRY, EXIT], 94000.0, store=store)
    assert out["paused"]
    assert (store.get_flag(store_mod.PAUSE) or {})["value"] == "on"
    assert [o["side"] for o in out["orders"]] == ["SELL"]


def test_a_pause_flag_keeps_blocking_until_a_human_clears_it():
    store = store_mod.MemoryStore()
    store.set_flag(store_mod.PAUSE, "on", "set yesterday")
    out = gate([ENTRY], 100000.0, store=store)      # no loss today at all
    assert out["orders"] == []
    assert any(a["rule"] == "pause_flag" for a in out["actions"])


def test_clearing_the_pause_flag_lets_entries_through_again():
    store = store_mod.MemoryStore()
    store.set_flag(store_mod.PAUSE, "on")
    store.clear_flag(store_mod.PAUSE)
    assert gate([ENTRY], 100000.0, store=store)["orders"]


def test_a_drawdown_past_the_worst_tested_halves_new_sizes():
    out = gate([ENTRY], 65000.0, day_start=65000.0, peak=100000.0)
    assert out["orders"][0]["qty"] == 5
    assert any(a["rule"] == "drawdown_halve" for a in out["actions"])


def test_a_drawdown_within_the_worst_tested_changes_nothing():
    out = gate([ENTRY], 80000.0, day_start=80000.0, peak=100000.0)
    assert out["orders"][0]["qty"] == 10


def test_intraday_is_squared_off_at_1515_even_with_no_exit_planned():
    positions = [{"ticker": "A.NS", "strategy": "s", "qty": 7, "stop": 90.0}]
    out = gate([ENTRY], 100000.0, positions=positions, intraday=True,
               now=dt.datetime(2026, 11, 26, 15, 20, tzinfo=IST))
    sides = {o["side"] for o in out["orders"]}
    assert sides == {"SELL"}
    assert any(a["rule"] == "square_off" for a in out["actions"])


def test_before_1515_nothing_is_forced():
    positions = [{"ticker": "A.NS", "strategy": "s", "qty": 7, "stop": 90.0}]
    out = gate([ENTRY], 100000.0, positions=positions, intraday=True,
               now=dt.datetime(2026, 11, 26, 14, 0, tzinfo=IST))
    assert not any(a["rule"] == "square_off" for a in out["actions"])


def test_a_position_without_a_stop_is_flagged_for_one():
    positions = [{"ticker": "A.NS", "strategy": "s", "qty": 7, "stop": None}]
    out = gate([], 100000.0, positions=positions)
    assert out["stops_to_restore"] == ["A.NS"]


# ------------------------------------ the gate may only reduce

@pytest.mark.parametrize("equity,peak,intraday", [
    (100000.0, 100000.0, False), (96500.0, 100000.0, False),
    (94000.0, 100000.0, False), (65000.0, 100000.0, False),
    (100000.0, 100000.0, True)])
def test_the_gate_never_raises_exposure(equity, peak, intraday):
    """The one property that makes a risk gate a risk gate.

    A gate that could raise a size would be a strategy wearing a safety
    jacket, and the rule it overrode would be the one that was tested.
    """
    orders = [dict(ENTRY), dict(EXIT),
              {"ticker": "B.NS", "strategy": "t", "side": "SHORT", "qty": 3,
               "target_qty": 3}]
    out = gate(orders, equity, day_start=equity, peak=peak,
               positions=[{"ticker": "A.NS", "strategy": "s", "qty": 5,
                           "stop": 90.0}],
               intraday=intraday,
               now=dt.datetime(2026, 11, 26, 15, 30, tzinfo=IST),
               store=store_mod.MemoryStore())
    assert risk.check_invariant(orders, out["orders"]) == []


def test_the_invariant_catches_a_gate_that_raised_a_size():
    before = [dict(ENTRY, qty=5)]
    after = [dict(ENTRY, qty=50)]
    assert risk.check_invariant(before, after)


# ------------------------------------------ idempotency and reruns

def test_an_order_id_is_deterministic():
    a = store_mod.order_id(dt.date(2026, 11, 25), "tcs.ns", "buy", "orb")
    b = store_mod.order_id("2026-11-25", "TCS.NS", "BUY", "orb")
    assert a == b == "2026-11-25|TCS.NS|BUY|orb"


def test_a_rerun_creates_no_second_order():
    store = store_mod.MemoryStore()
    order = {"ticker": "A.NS", "strategy": "s", "side": "BUY", "qty": 1,
             "target_qty": 1}
    counts = []
    for _ in range(2):
        out = runner.run(ctx(store=store, dry_run=False, planned=[dict(order)]))
        record = next(s for s in out["steps"] if s["name"] == "record")
        counts.append(record["detail"]["placed"])
    assert counts == [1, 0]
    assert len(store.orders_for(TODAY.date())) == 1


def test_a_dry_run_writes_no_order():
    store = store_mod.MemoryStore()
    runner.run(ctx(store=store, dry_run=True,
                   planned=[{"ticker": "A.NS", "strategy": "s", "side": "BUY",
                             "qty": 1, "target_qty": 1}]))
    assert store.orders_for(TODAY.date()) == []


def test_the_same_alert_is_delivered_once():
    store = store_mod.MemoryStore()
    sink = alerts.Sink(store)
    assert sink.send("k", "first") is True
    assert sink.send("k", "again") is False
    assert len(sink.sent) == 1


# ------------------------------------------------ the trust ladder

def test_the_alerts_rung_places_nothing():
    store = store_mod.MemoryStore()
    out = runner.run(ctx(store=store, dry_run=False, trust="alerts",
                         planned=[{"ticker": "A.NS", "strategy": "s",
                                   "side": "BUY", "qty": 1, "target_qty": 1}]))
    record = next(s for s in out["steps"] if s["name"] == "record")
    assert record["detail"]["placed"] == 0
    assert store.orders_for(TODAY.date()) == []


def test_the_approve_rung_waits_for_an_approval():
    store = store_mod.MemoryStore()
    plan = [{"ticker": "A.NS", "strategy": "s", "side": "BUY", "qty": 1,
             "target_qty": 1}]
    out = runner.run(ctx(store=store, dry_run=False, trust="approve",
                         planned=[dict(p) for p in plan]))
    assert next(s for s in out["steps"] if s["name"] == "record")["detail"]["placed"] == 0
    store.set_flag(runner.APPROVE_FLAG, "on")
    out = runner.run(ctx(store=store, dry_run=False, trust="approve",
                         planned=[dict(p) for p in plan]))
    assert next(s for s in out["steps"] if s["name"] == "record")["detail"]["placed"] == 1


def test_the_default_rung_is_auto_for_paper():
    assert runner.DEFAULT_LADDER == "auto"
    assert set(runner.LADDER) == {"alerts", "approve", "auto"}


# ------------------------------------------------------- secrets

def test_a_bot_token_is_never_written_down():
    token = "987654321:AAHsomethingLongEnoughToLookRealAbcdefgh"
    text = "GET https://api.telegram.org/bot%s/sendMessage failed" % token
    assert token not in alerts.redact(text, token)
    assert "<redacted>" in alerts.redact(text, token)


def test_a_token_is_scrubbed_even_when_it_is_not_the_configured_one():
    # A traceback can carry a token from somewhere other than this env.
    other = "111111111:BBanotherTokenShapedStringThatIsLong"
    assert other not in alerts.redact("oops " + other, token="")


def test_an_unconfigured_alerter_cannot_break_a_run():
    sink = alerts.NullSink(store_mod.MemoryStore())
    assert not sink.configured()
    assert sink.send("k", "text") is True       # recorded, delivered nowhere


def test_the_harness_never_reads_the_env_file():
    """§1.1: secrets arrive through the environment, never by reading the file.

    Matching a bare ".env" substring is useless here - `os.environ`
    contains it - so this looks for the two things that actually open it.
    """
    import pathlib
    import re
    folder = pathlib.Path(__file__).resolve().parent.parent / "harness"
    for path in folder.glob("*.py"):
        text = path.read_text()
        assert "load_dotenv" not in text, "%s loads .env itself" % path.name
        quoted = re.findall(r"""['"]([^'"]*\.env)['"]""", text)
        assert not [q for q in quoted if not q.endswith(".env.example")], (
            "%s opens an .env path: %s" % (path.name, quoted))


# ----------------------------------------------------- missed run

def test_a_missed_end_of_day_run_alerts():
    store = store_mod.MemoryStore()
    sink = alerts.Sink(store)
    out = runner.check_missed_run(
        store, sink, dt.datetime(2026, 11, 26, 17, 0, tzinfo=IST))
    assert out["checked"] and not out["ran_today"]
    assert sink.sent and "no end-of-day run" in sink.sent[0]["text"]


def test_a_run_that_happened_raises_no_alert():
    # The log stamps real wall-clock time, so "today" here has to be the
    # real today rather than a fixed date, or the comparison is against
    # a day the log entry could never carry.
    store = store_mod.MemoryStore()
    sink = alerts.Sink(store)
    store.log("r1", "lock", "ok", {})
    now = dt.datetime.now(IST).replace(hour=17, minute=0)
    if now.weekday() >= 5:
        pytest.skip("the missed-run check does not apply at the weekend")
    out = runner.check_missed_run(store, sink, now)
    assert out["ran_today"] and sink.sent == []


def test_nothing_is_checked_before_the_deadline():
    store = store_mod.MemoryStore()
    sink = alerts.Sink(store)
    out = runner.check_missed_run(
        store, sink, dt.datetime(2026, 11, 26, 12, 0, tzinfo=IST))
    assert not out["checked"] and sink.sent == []


def test_no_missed_run_alert_on_a_holiday():
    store = store_mod.MemoryStore()
    sink = alerts.Sink(store)
    out = runner.check_missed_run(
        store, sink, dt.datetime(2026, 1, 26, 17, 0, tzinfo=IST))
    assert not out["checked"] and sink.sent == []


# ------------------------------------------------------- logging

def test_every_step_is_logged():
    store = store_mod.MemoryStore()
    out = runner.run(ctx(store=store))
    logged = [r["step"] for r in store.logs(out["run_id"])]
    assert logged == [s["name"] for s in out["steps"]]


def test_a_stopped_run_still_logs_the_step_that_stopped_it():
    store = store_mod.MemoryStore()
    out = runner.run(ctx(store=store, bars={}))
    assert store.logs(out["run_id"])[-1]["step"] == "fresh_data"
    assert store.logs(out["run_id"])[-1]["status"] == "stop"


def test_a_step_that_raises_stops_the_run_rather_than_crashing_it():
    store = store_mod.MemoryStore()

    def boom(c):
        raise RuntimeError("something broke")

    original = list(runner.STEPS)
    runner.STEPS.insert(1, boom)
    try:
        out = runner.run(ctx(store=store))
    finally:
        runner.STEPS[:] = original
    assert out["stopped_at"] == "boom" or out["steps"][1]["stop"]


# ---------------------------------------------- paper trading only

def test_nothing_in_the_harness_can_place_a_live_order():
    import pathlib
    folder = pathlib.Path(__file__).resolve().parent.parent / "harness"
    for path in folder.glob("*.py"):
        text = path.read_text().lower()
        for needle in ("place_order", "submit_order", "kiteconnect",
                       "alpaca", "import ccxt", "broker_api"):
            assert needle not in text, "%s: %s" % (path.name, needle)
