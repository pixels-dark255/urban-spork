"""
Who actually advances the arenas: the scheduled job, or this process.

The arena's simulation moved out of the Render web service and into a
scheduled GitHub Actions job (`backend/run_daily.py`). A free Render
instance has a tiny CPU and sleeps after fifteen minutes, so the first
request after a sleep woke it straight into a multi-arena catch-up, it fell
over under the load, restarted, and began the same catch-up again - which
is what produced 503s at exactly the moment anyone looked.

So the web service is now a dashboard. It reads arenas and never advances
them. `ARENA_RUNNER` says so explicitly rather than leaving it implicit in
whichever code path happens to be reachable:

    github     (default) the scheduled job owns the simulation. Nothing in
               the web process ticks an arena - no scheduler job, and no
               background tick from a page load.
    inprocess  the old behaviour, for running the whole thing on one
               machine.

Defaulting to `github` means a deploy that forgets the variable is the
safe way round: a dashboard that shows slightly stale numbers, rather than
two writers racing over the same Postgres rows.
"""
from __future__ import annotations

import datetime as dt
import os

import timeutil

GITHUB = "github"
INPROCESS = "inprocess"
MODES = (GITHUB, INPROCESS)

# Must match the schedule in .github/workflows/arena-runner.yml.
CRON_MINUTE = 17
CRON_EVERY_HOURS = 2
# A scheduled run that settles yesterday is still correct, but a dashboard
# showing nothing newer than this on a weekday means the job has stopped.
STALE_HOURS = 6


def mode() -> str:
    """Read at call time, not import time, so tests and a local .env can
    change it without reloading half the app."""
    raw = (os.getenv("ARENA_RUNNER") or "").strip().lower()
    return raw if raw in MODES else GITHUB


def ticks_in_process() -> bool:
    return mode() == INPROCESS


def next_run(now: dt.datetime | None = None) -> dt.datetime:
    """The next time the scheduled job is due, in UTC.

    GitHub's scheduler is best-effort and frequently several minutes late;
    this is when it is *asked* to run, which is the honest thing to show.
    """
    now = timeutil.ensure_aware(now or timeutil.utc_now())
    candidate = now.replace(minute=CRON_MINUTE, second=0, microsecond=0)
    if candidate.hour % CRON_EVERY_HOURS:
        candidate += dt.timedelta(hours=1)
        candidate = candidate.replace(minute=CRON_MINUTE, second=0, microsecond=0)
    while candidate <= now or candidate.hour % CRON_EVERY_HOURS:
        candidate += dt.timedelta(hours=1)
    return candidate


def status(state: dict | None = None, now: dt.datetime | None = None) -> dict:
    """What the dashboard needs to explain itself.

    The old UI said "the server was asleep", which is no longer the right
    story: the web service being asleep is now expected and harmless. What
    matters is whether the RUNNER is still running.
    """
    now = timeutil.ensure_aware(now or timeutil.utc_now())
    state = state or {}
    last = timeutil.parse_utc(state.get("last_tick_at"))
    age_hours = None if last is None else round((now - last).total_seconds() / 3600.0, 2)
    upcoming = next_run(now)
    current = mode()
    # Only flagged on a weekday: a Saturday with no NSE or US session has
    # nothing for the runner to settle, and nagging about it would train
    # the banner to be ignored.
    weekday = now.astimezone(timeutil.IST).weekday() < 5
    return {
        "mode": current,
        "ticks_in_process": current == INPROCESS,
        "last_tick_at": state.get("last_tick_at"),
        "last_tick": state.get("last_tick"),
        "age_hours": age_hours,
        "next_run_at": timeutil.to_iso(upcoming),
        "next_run_ist": upcoming.astimezone(timeutil.IST).strftime("%H:%M"),
        "schedule": f"every {CRON_EVERY_HOURS}h at :{CRON_MINUTE:02d} UTC",
        "stale": bool(current == GITHUB and weekday
                      and (age_hours is None or age_hours > STALE_HOURS)),
        "stale_after_hours": STALE_HOURS,
    }
