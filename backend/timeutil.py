"""
One source of truth for time.

The bug this module exists to kill: the backend used naive
``datetime.utcnow()`` everywhere. A naive datetime carries no offset, so
``.timestamp()`` interprets it as *local* time. On a machine set to IST that
is 5h30m off - measured at exactly -19800 seconds - which is why the
forecast chart started five and a half hours in the past. The frontend then
made the mirror-image mistake, parsing those offset-less strings as local
time, so target times and BUY/SELL markers were off by the same amount in
the other direction.

Rules:
  - ``utc_now()`` is the only way to ask what time it is.
  - ``iso_now()`` / ``to_iso()`` always emit an offset, so a string written
    today is unambiguous to anything that reads it.
  - ``parse_utc()`` reads both formats. Stored data written before this fix
    is naive-UTC with no offset, and it must keep working: a naive string is
    therefore interpreted as UTC, which is what it always meant.
  - Timestamps are compared as datetimes, never as strings. Lexicographic
    comparison silently gives the wrong answer the moment the two sides are
    in different formats, which is exactly what a migration produces.
"""
from __future__ import annotations

import datetime as dt

UTC = dt.timezone.utc
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def utc_now() -> dt.datetime:
    """Timezone-aware current UTC."""
    return dt.datetime.now(UTC)


def iso_now() -> str:
    """Current UTC as an ISO string carrying its offset."""
    return utc_now().isoformat()


def to_iso(value: dt.datetime) -> str:
    """ISO string with an offset. A naive input is assumed to be UTC, which
    is what every naive timestamp in this codebase has always meant."""
    return ensure_aware(value).isoformat()


def ensure_aware(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def parse_utc(value) -> dt.datetime | None:
    """Parse a stored timestamp into aware UTC, accepting both formats.

    Returns None rather than raising: a corrupt timestamp in one stored
    prediction should not take down the request that happened to read it.
    """
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return ensure_aware(value).astimezone(UTC)
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None
    # fromisoformat on Python 3.11+ handles "Z", but older strings in the
    # store may use it and older interpreters choke, so normalise first.
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return ensure_aware(parsed).astimezone(UTC)


def epoch_seconds(value: dt.datetime) -> int:
    """Unix timestamp, correct regardless of the machine's local timezone."""
    return int(ensure_aware(value).timestamp())


def is_before_or_equal(candidate, reference) -> bool:
    """Compare two stored timestamps safely.

    Either side may be naive-UTC (old data) or offset-carrying (new data);
    comparing the raw strings would be wrong whenever the formats differ.
    An unparseable candidate returns False - "not due yet" is the safe
    answer, because the alternative is resolving a prediction against a
    timestamp nobody can read.
    """
    left, right = parse_utc(candidate), parse_utc(reference)
    if left is None or right is None:
        return False
    return left <= right
