"""
NSE market calendar - the single source of truth for "is the market open?".

Both `scheduler.is_market_hours()` and the frontend's `isMarketOpenNow()`
answered this question independently, and both ignored trading holidays
entirely. Duplicated logic drifts, so the rule lives here once and the
frontend reads it from /api/market-status instead of reimplementing it.

The holiday dates themselves are data, not code: see nse_holidays.json.
They are loaded at import and can be reloaded without a restart.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import threading

import timeutil

IST = timeutil.IST

MARKET_OPEN = dt.time(9, 15)
MARKET_CLOSE = dt.time(15, 30)

HOLIDAY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "nse_holidays.json")

_lock = threading.Lock()
_holidays: dict[str, str] = {}      # "YYYY-MM-DD" -> description
_years_loaded: set[str] = set()
_meta: dict = {}


def _load_holidays() -> None:
    global _holidays, _years_loaded, _meta
    try:
        with open(HOLIDAY_FILE) as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[warn] could not read {HOLIDAY_FILE}: {e}")
        payload = {}

    holidays, years = {}, set()
    for year, entries in (payload.get("holidays") or {}).items():
        if not entries:
            continue          # year present but unpopulated - see the file
        years.add(str(year))
        for entry in entries:
            date = (entry or {}).get("date")
            if not date:
                continue
            try:
                parsed = dt.date.fromisoformat(date)
            except (TypeError, ValueError):
                print(f"[warn] ignoring unparseable holiday date {date!r}")
                continue
            # NSE never publishes a weekend date as a trading holiday - the
            # market is already shut. A weekend entry means the list is a
            # festival calendar rather than a trading calendar, which is how
            # a bad 2027 list was caught. Loading it would imply the rest of
            # that list is trustworthy, so it is rejected loudly instead.
            if parsed.weekday() >= 5:
                print(f"[warn] ignoring weekend holiday {date} "
                      f"({entry.get('description', '?')}) - NSE does not list "
                      f"weekend dates as trading holidays; check the source list")
                continue
            if str(parsed.year) != str(year):
                print(f"[warn] ignoring {date}: filed under {year}")
                continue
            holidays[date] = entry.get("description", "Trading holiday")

    with _lock:
        _holidays = holidays
        _years_loaded = years
        _meta = {"source": payload.get("source", ""),
                 "updated": payload.get("updated", "")}


_load_holidays()


def reload_holidays() -> dict:
    """Re-read the holiday file. Useful after editing it on a live service."""
    _load_holidays()
    return holiday_status()


def holiday_status() -> dict:
    with _lock:
        return {
            "years_loaded": sorted(_years_loaded),
            "holiday_count": len(_holidays),
            "source": _meta.get("source", ""),
            "updated": _meta.get("updated", ""),
        }


def holidays_loaded_for(year: int | str) -> bool:
    """Whether we actually know this year's holidays.

    Reported honestly rather than assumed: with no list for a year, every
    weekday looks like a trading day, and the caller deserves to know that
    is what it is being told.
    """
    with _lock:
        return str(year) in _years_loaded


def is_trading_holiday(day: dt.date) -> bool:
    with _lock:
        return day.isoformat() in _holidays


def holiday_name(day: dt.date) -> str | None:
    with _lock:
        return _holidays.get(day.isoformat())


def now_ist(now: dt.datetime | None = None) -> dt.datetime:
    return (now or timeutil.utc_now()).astimezone(IST)


def is_weekend(day: dt.date) -> bool:
    return day.weekday() >= 5


def is_trading_day(day: dt.date) -> bool:
    return not is_weekend(day) and not is_trading_holiday(day)


def is_market_open(now: dt.datetime | None = None) -> bool:
    """Open means: a trading day, and inside 09:15-15:30 IST."""
    moment = now_ist(now)
    if not is_trading_day(moment.date()):
        return False
    return MARKET_OPEN <= moment.time() <= MARKET_CLOSE


def next_trading_day(day: dt.date, limit: int = 30) -> dt.date | None:
    candidate = day + dt.timedelta(days=1)
    for _ in range(limit):
        if is_trading_day(candidate):
            return candidate
        candidate += dt.timedelta(days=1)
    return None


def market_status(now: dt.datetime | None = None) -> dict:
    """Everything the UI needs to render the market pill, computed once here
    so the frontend does not duplicate the rule."""
    moment = now_ist(now)
    today = moment.date()
    holiday = holiday_name(today)
    known = holidays_loaded_for(today.year)

    if is_weekend(today):
        reason = "weekend"
    elif holiday:
        reason = "holiday"
    elif not (MARKET_OPEN <= moment.time() <= MARKET_CLOSE):
        reason = "outside_session_hours"
    else:
        reason = "open"

    upcoming = next_trading_day(today)
    return {
        "is_open": is_market_open(now),
        "reason": reason,
        "holiday_name": holiday,
        "now_ist": moment.isoformat(),
        "session": {"open": MARKET_OPEN.strftime("%H:%M"),
                    "close": MARKET_CLOSE.strftime("%H:%M"),
                    "timezone": "Asia/Kolkata"},
        "is_trading_day": is_trading_day(today),
        "next_trading_day": upcoming.isoformat() if upcoming else None,
        # False means holidays for this year have not been loaded, so a
        # holiday would currently read as a normal trading day.
        "holidays_known_for_year": known,
        "holidays": holiday_status(),
    }
