"""The runner loop. Fixed order, and any step may stop the run.

Stopping is the safe outcome and it is the common one. A harness that
trades through a failed check is worse than one that does nothing,
because doing nothing is visible and trading on a stale price is not.

    1  lock            a second concurrent run exits
    2  trading day     the NSE calendar decides, not the clock
    3  reconcile       what Neon holds vs what the runner believes
    4  fresh data      no bar from this session means no trading
    5  plan            regime, signals, (AI: off), weights, share counts
    6  risk gate       may only reduce; never changes a rule
    7  record          paper fills, or nothing in a dry run
    8  alerts          failure, or the end-of-day summary

Steps 1 to 4 are refusals. They exist to catch the states where trading
would be wrong rather than merely unprofitable: two runners racing, a
holiday, a position the runner does not know it has, a price from
yesterday.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import uuid

import market_calendar

from . import alerts as alerts_mod, risk
from .store import PAUSE, order_id

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
LOCK_NAME = "harness"
LOCK_SECONDS = 900

# Trust ladder. Paper trading only (AGENTS.md §1.1): even `auto` writes a
# paper fill to Neon and nothing else. Live trading is out of scope and
# there is no code path towards it.
LADDER = ("alerts", "approve", "auto")
DEFAULT_LADDER = "auto"
APPROVE_FLAG = "APPROVED"


@dataclasses.dataclass
class StepResult:
    name: str
    ok: bool
    stop: bool = False
    alert: str | None = None
    detail: dict = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class Context:
    store: object
    sink: object
    now_ist: dt.datetime
    account: str = "swing"
    dry_run: bool = True
    trust: str = DEFAULT_LADDER
    equity: float = 0.0
    day_start_equity: float = 0.0
    worst_backtest_drawdown_pct: float = 0.0
    peak_equity: float | None = None
    intraday: bool = False
    bars: dict = dataclasses.field(default_factory=dict)
    believed_positions: list = dataclasses.field(default_factory=list)
    planned: list = dataclasses.field(default_factory=list)
    plan_reason: str = ""
    run_id: str = dataclasses.field(default_factory=lambda: uuid.uuid4().hex[:12])
    holder: str = dataclasses.field(default_factory=lambda: uuid.uuid4().hex[:12])


# --------------------------------------------------------------- steps

def step_lock(ctx: Context) -> StepResult:
    got = ctx.store.acquire_lock(LOCK_NAME, ctx.holder, LOCK_SECONDS)
    return StepResult("lock", ok=got, stop=not got,
                      alert=None if got else "another run holds the lock",
                      detail={"holder": ctx.holder, "acquired": got})


def step_trading_day(ctx: Context) -> StepResult:
    day = ctx.now_ist.date()
    holiday = market_calendar.is_trading_holiday(day)
    weekend = day.weekday() >= 5
    trading = not (holiday or weekend)
    why = "weekend" if weekend else ("NSE holiday" if holiday else "trading day")
    # Not an alert: a holiday is the expected state on a holiday, and
    # alerting on it would train everyone to ignore the alerts.
    return StepResult("trading_day", ok=True, stop=not trading,
                      detail={"date": day.isoformat(), "why": why})


def step_reconcile(ctx: Context) -> StepResult:
    stored = {(p["ticker"], p["strategy"]): int(p["qty"])
              for p in ctx.store.positions(ctx.account)}
    believed = {(p["ticker"], p["strategy"]): int(p["qty"])
                for p in ctx.believed_positions}
    if stored == believed:
        return StepResult("reconcile", ok=True, detail={"positions": len(stored)})
    diff = {"|".join(k): {"stored": stored.get(k, 0), "believed": believed.get(k, 0)}
            for k in sorted(set(stored) | set(believed))
            if stored.get(k, 0) != believed.get(k, 0)}
    return StepResult("reconcile", ok=False, stop=True,
                      alert="positions in Neon do not match what the runner "
                            "believes it holds: %s" % diff,
                      detail={"mismatch": diff})


def step_fresh_data(ctx: Context) -> StepResult:
    """Never trade on a price from a previous session."""
    session = ctx.now_ist.date()
    if not ctx.bars:
        # No data is not the same as no stale data. An empty dict would
        # otherwise sail through this check and the run would proceed to
        # trade on prices it never fetched.
        return StepResult("fresh_data", ok=False, stop=True,
                          alert="no market data was supplied; refusing to trade",
                          detail={"tickers": 0})
    stale = {}
    for ticker, frame in sorted(ctx.bars.items()):
        if frame is None or len(frame) == 0:
            stale[ticker] = "no bars at all"
            continue
        last = frame.index[-1]
        last_date = last.date() if hasattr(last, "date") else None
        if last_date != session:
            stale[ticker] = "last bar %s, session %s" % (last_date, session)
    if stale:
        return StepResult("fresh_data", ok=False, stop=True,
                          alert="stale prices, refusing to trade: %s" % stale,
                          detail={"stale": stale})
    return StepResult("fresh_data", ok=True, detail={"tickers": len(ctx.bars)})


def step_plan(ctx: Context) -> StepResult:
    """Regime, signals, (AI off by default), target weights, share counts."""
    return StepResult("plan", ok=True,
                      detail={"orders": len(ctx.planned),
                              "reason": ctx.plan_reason,
                              "ai_call": "off (Phase 7)"})


def step_risk(ctx: Context) -> StepResult:
    gate_ctx = risk.Context(
        account=ctx.account, equity=ctx.equity,
        day_start_equity=ctx.day_start_equity,
        worst_backtest_drawdown_pct=ctx.worst_backtest_drawdown_pct,
        positions=ctx.store.positions(ctx.account),
        now_ist=ctx.now_ist, intraday=ctx.intraday)
    out = risk.apply(ctx.planned, gate_ctx, ctx.peak_equity, ctx.store)
    violations = risk.check_invariant(ctx.planned, out["orders"])
    if violations:
        # The gate broke its own contract. That is a code fault, not a
        # market condition, and trading on it would be trading on a bug.
        return StepResult("risk", ok=False, stop=True,
                          alert="risk gate raised exposure: %s" % violations,
                          detail={"violations": violations})
    ctx.planned = out["orders"]
    return StepResult("risk", ok=True, detail=out)


def step_record(ctx: Context) -> StepResult:
    """Write paper fills. A dry run does everything except this."""
    if ctx.trust == "alerts":
        return StepResult("record", ok=True,
                          detail={"placed": 0, "why": "trust ladder is "
                                  "'alerts': nothing is placed"})
    if ctx.trust == "approve":
        approved = (ctx.store.get_flag(APPROVE_FLAG) or {}).get("value") == "on"
        if not approved:
            return StepResult("record", ok=True,
                              detail={"placed": 0, "why": "trust ladder is "
                                      "'approve' and no approval is set"})

    day = ctx.now_ist.date()
    placed, duplicates = [], []
    for order in ctx.planned:
        oid = order_id(day, order["ticker"], order["side"], order.get("strategy", "?"))
        row = {"order_id": oid, "date": day.isoformat(), "ticker": order["ticker"],
               "side": order["side"], "strategy": order.get("strategy", "?"),
               "qty": int(order["qty"]), "price": order.get("price"),
               "status": "dry_run" if ctx.dry_run else "paper"}
        if ctx.dry_run:
            placed.append(row)
            continue
        if ctx.store.record_order(row):
            placed.append(row)
        else:
            duplicates.append(oid)
    return StepResult("record", ok=True,
                      detail={"placed": len(placed), "duplicates": duplicates,
                              "dry_run": ctx.dry_run, "orders": placed})


STEPS = [step_lock, step_trading_day, step_reconcile, step_fresh_data,
         step_plan, step_risk, step_record]


# --------------------------------------------------------------- the loop

def run(ctx: Context) -> dict:
    results = []
    stopped_at = None
    try:
        for step in STEPS:
            try:
                result = step(ctx)
            except Exception as exc:
                result = StepResult(step.__name__.replace("step_", ""),
                                    ok=False, stop=True,
                                    alert="%s: %s" % (type(exc).__name__, exc),
                                    detail={"error": repr(exc)})
            results.append(result)
            ctx.store.log(ctx.run_id, result.name,
                          "ok" if result.ok else ("stop" if result.stop else "warn"),
                          alerts_mod.redact(result.detail) if isinstance(result.detail, str)
                          else result.detail)
            if result.stop:
                stopped_at = result.name
                if result.alert:
                    key, text = alerts_mod.failure(ctx.run_id, result.name,
                                                   result.alert)
                    ctx.sink.send(key, text)
                break
    finally:
        ctx.store.release_lock(LOCK_NAME, ctx.holder)

    return {"run_id": ctx.run_id, "stopped_at": stopped_at,
            "steps": [dataclasses.asdict(r) for r in results],
            "orders": ctx.planned if stopped_at is None else [],
            "dry_run": ctx.dry_run, "trust": ctx.trust,
            "paused": (ctx.store.get_flag(PAUSE) or {}).get("value") == "on"}


def check_missed_run(store, sink, now_ist: dt.datetime,
                     deadline=dt.time(16, 30)) -> dict:
    """Alert if the end-of-day run has not happened by 16:30 IST."""
    day = now_ist.date()
    if day.weekday() >= 5 or market_calendar.is_trading_holiday(day):
        return {"checked": False, "why": "not a trading day"}
    if now_ist.timetz() < deadline.replace(tzinfo=IST):
        return {"checked": False, "why": "before the deadline"}
    last = store.last_run_at()
    ran_today = bool(last and last.astimezone(IST).date() == day)
    if not ran_today:
        key, text = alerts_mod.missed_run(day.isoformat(),
                                          deadline.strftime("%H:%M"))
        sink.send(key, text)
    return {"checked": True, "ran_today": ran_today,
            "last_run": last.isoformat() if last else None}
