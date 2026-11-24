"""Accumulate intraday history in Postgres, because Yahoo will not keep it.

Yahoo serves roughly 60 days of 5m/15m bars and about two years of 1h. Sixty
days is nowhere near enough to prove an intraday strategy, and the window
slides, so a backtest run next month silently covers different sessions than
the one run today.

The fix is to store each day's bars as they are still available. Once a bar
size has at least `min_months_for_funnel` months of stored history it becomes
eligible for the funnel; until then, every result on it is labelled UNPROVEN
(AGENTS.md §2).

Reuses storage.py's connection pool rather than opening a second one, and
fetches through data_sources, the only module allowed to talk to Yahoo.
"""
from __future__ import annotations

import datetime

import pandas as pd

import data_sources
import settings
import storage

TABLE = "intraday_bars"

# What Yahoo will still serve for each bar size. Asking for more is not an
# error, it just returns what it has.
FETCH_PERIOD = {"5m": "1mo", "15m": "1mo", "1h": "6mo", "1d": "2y"}

# Bounded, so one unanswered symbol cannot eat the job's whole budget.
FETCH_TIMEOUT = 45.0

DDL = """
CREATE TABLE IF NOT EXISTS intraday_bars (
    symbol    TEXT        NOT NULL,
    bar_size  TEXT        NOT NULL,
    ts        TIMESTAMPTZ NOT NULL,
    open      DOUBLE PRECISION,
    high      DOUBLE PRECISION,
    low       DOUBLE PRECISION,
    close     DOUBLE PRECISION,
    volume    DOUBLE PRECISION,
    PRIMARY KEY (symbol, bar_size, ts)
)
"""


class NoDatabase(RuntimeError):
    """Archiving to a JSON file would defeat the point: the file is wiped."""


def _pool():
    if not storage._pg_pool:
        raise NoDatabase(
            "DATABASE_URL is not set, so there is nowhere durable to append "
            "bars. A JSON file on a free dyno is wiped on restart, which "
            "would quietly lose the history this job exists to build.")
    return storage._pg_pool


def ensure_table() -> None:
    conn = _pool().getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(DDL)
        conn.commit()
    finally:
        _pool().putconn(conn)


def _rows(symbol: str, bar_size: str, frame: pd.DataFrame) -> list[tuple]:
    """Bars as tuples, skipping any row with no close.

    A bar with a blank close is not a quiet zero - it is Yahoo telling us it
    has nothing for that minute, and storing it as a number would invent a
    price that never traded.
    """
    out = []
    for ts, row in frame.iterrows():
        close = row.get("Close")
        if close is None or pd.isna(close):
            continue
        stamp = ts.to_pydatetime() if isinstance(ts, pd.Timestamp) else ts
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=datetime.timezone.utc)
        out.append((
            symbol, bar_size, stamp,
            _f(row.get("Open")), _f(row.get("High")),
            _f(row.get("Low")), float(close), _f(row.get("Volume")),
        ))
    return out


def _f(value):
    if value is None or pd.isna(value):
        return None
    return float(value)


def append(symbol: str, bar_size: str, frame: pd.DataFrame) -> int:
    """Upsert one symbol's bars. Returns how many rows were written.

    ON CONFLICT DO NOTHING, so re-running the job on the same day is free
    and a bar already stored is never rewritten by a later, revised copy.
    """
    rows = _rows(symbol, bar_size, frame)
    if not rows:
        return 0
    conn = _pool().getconn()
    try:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO intraday_bars "
                "(symbol, bar_size, ts, open, high, low, close, volume) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (symbol, bar_size, ts) DO NOTHING", rows)
        conn.commit()
    finally:
        _pool().putconn(conn)
    return len(rows)


def coverage() -> dict:
    """Per bar size: rows stored, first and last bar, and months spanned."""
    conn = _pool().getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT bar_size, COUNT(*), MIN(ts), MAX(ts), "
                "COUNT(DISTINCT symbol) FROM intraday_bars GROUP BY bar_size")
            rows = cur.fetchall()
    finally:
        _pool().putconn(conn)
    out = {}
    for bar_size, count, first, last, symbols in sorted(rows):
        out[bar_size] = {
            "rows": int(count), "symbols": int(symbols),
            "first": first.isoformat() if first else None,
            "last": last.isoformat() if last else None,
            "months": months_between(first, last),
        }
    return out


def months_between(first, last) -> float:
    if not first or not last:
        return 0.0
    return round((last - first).days / 30.44, 2)


def eligible(cfg: dict, cover: dict) -> dict:
    """Which bar sizes have earned their way into the funnel.

    A bar size listed as unproven stays unproven until enough history is
    stored. One that was never unproven (daily) is eligible from the start.
    """
    out = {}
    for bar_size in cfg["bar_sizes"]:
        if not settings.is_unproven(cfg, bar_size):
            out[bar_size] = {"eligible": True, "months": None,
                             "why": "long history available from the source"}
            continue
        months = (cover.get(bar_size) or {}).get("months", 0.0)
        need = cfg["min_months_for_funnel"]
        out[bar_size] = {
            "eligible": months >= need,
            "months": months,
            "why": ("%.1f of %d months stored" % (months, need)
                    if months < need else "%.1f months stored" % months),
        }
    return out


def run(cfg: dict | None = None, bar_sizes: list[str] | None = None) -> dict:
    """Append today's bars for every study ticker. Safe to re-run."""
    cfg = cfg or settings.load()
    bar_sizes = bar_sizes or [b for b in cfg["bar_sizes"] if b != "1d"]
    ensure_table()
    written, failed = {}, {}
    for bar_size in sorted(bar_sizes):
        total = 0
        for symbol in sorted(cfg["study_tickers"]):
            try:
                frame = data_sources._yf_download(
                    symbol, FETCH_PERIOD.get(bar_size, "1mo"), bar_size,
                    timeout=FETCH_TIMEOUT)
                total += append(symbol, bar_size, frame)
            except Exception as exc:
                failed["%s/%s" % (symbol, bar_size)] = "%s: %s" % (
                    type(exc).__name__, exc)
        written[bar_size] = total
    cover = coverage()
    return {"written": written, "failed": failed, "coverage": cover,
            "eligible": eligible(cfg, cover)}
