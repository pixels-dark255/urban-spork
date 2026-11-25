"""Does a signal change when you take tomorrow away?

The test is blunt and hard to argue with. Compute a signal for a date from
the whole history, then compute it again from a copy of the data truncated
at that date. A causal signal cannot tell the difference. Anything that
can is reading the future, whatever its author believes.

Truncation rather than inspection, because look-ahead almost never looks
like look-ahead in the source. It arrives as a rolling window that is
centred instead of trailing, a groupby that spans a whole session, a
`max()` over a column that has not finished yet. Every one of those reads
innocently and fails this check.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

DEFAULT_DATES = 30

# Below this there is not enough history for most indicators to exist, and
# a NaN on both sides would "match" without proving anything.
MIN_WARMUP = 120


class LookAheadError(AssertionError):
    """A signal changed when the future was removed."""


def sample_dates(frame: pd.DataFrame, dates: int = DEFAULT_DATES,
                 seed: int = 0, warmup: int = MIN_WARMUP) -> list:
    """Dates to truncate at, drawn deterministically.

    The last bar is excluded: truncating there removes nothing, so it
    would pass for free and quietly weaken the sample.
    """
    usable = len(frame) - 1
    if usable <= warmup:
        return []
    rng = np.random.default_rng(seed)
    pool = np.arange(warmup, usable)
    take = min(dates, len(pool))
    picked = sorted(rng.choice(pool, size=take, replace=False).tolist())
    return [frame.index[i] for i in picked]


def _same(a, b, tol: float = 1e-9) -> bool:
    """Equal, treating two unknowns as agreeing."""
    if a is None and b is None:
        return True
    if isinstance(a, float) and isinstance(b, float):
        if np.isnan(a) and np.isnan(b):
            return True
        return abs(a - b) <= tol * max(1.0, abs(a), abs(b))
    try:
        if pd.isna(a) and pd.isna(b):
            return True
    except (TypeError, ValueError):
        pass
    return a == b


def check(compute, frame: pd.DataFrame, dates: int = DEFAULT_DATES,
          seed: int = 0, label: str = "signal") -> dict:
    """Compare each date's signal computed on full data and on truncated.

    `compute(frame, when)` must return the signal for `when` given `frame`.
    """
    picks = sample_dates(frame, dates, seed)
    mismatches = []
    for when in picks:
        try:
            full = compute(frame, when)
        except Exception as exc:
            mismatches.append({"date": str(when), "error": "full: %r" % exc})
            continue
        truncated_frame = frame.loc[:when]
        try:
            truncated = compute(truncated_frame, when)
        except Exception as exc:
            mismatches.append({"date": str(when), "error": "truncated: %r" % exc})
            continue
        if not _same(full, truncated):
            mismatches.append({"date": str(when),
                               "full": _plain(full), "truncated": _plain(truncated)})
    return {"label": label, "checked": len(picks), "mismatches": mismatches,
            "ok": not mismatches and bool(picks)}


def _plain(value):
    if isinstance(value, (np.floating, np.integer)):
        return value.item()
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in sorted(value.items())}
    return value


def assert_causal(compute, frame: pd.DataFrame, dates: int = DEFAULT_DATES,
                  seed: int = 0, label: str = "signal") -> dict:
    report = check(compute, frame, dates, seed, label)
    if not report["ok"]:
        raise LookAheadError(
            "%s is not causal: %d of %d sampled dates changed when the "
            "future was removed. First: %r"
            % (label, len(report["mismatches"]), report["checked"],
               report["mismatches"][0] if report["mismatches"] else None))
    return report
