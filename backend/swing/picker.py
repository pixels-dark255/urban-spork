"""pick(as_of_date): who is the engine, who is the safe asset, today?

Re-picked quarterly, because an engine chosen once in 2022 and never
revisited is a bet on 2022. The rule is the same every quarter:

    engine  the engine candidate with the largest trend gap over the pick
            window: how much better it does above its 200-day average
            than below it
    safe    the safe candidate with the best average return on days the
            engine was in a downtrend, provided it is not simply the
            engine in disguise (correlation below 0.5). If none qualifies,
            the safe asset is rupees.

Everything is computed from bars dated on or before `as_of`. That is not
a convention, it is the only thing that makes the quarterly backtest
meaningful, and it is checked by re-running `pick` against data physically
truncated at `as_of` and requiring the same answer.

The trend gap is Phase 1's, imported rather than recomputed. Two
definitions of "trend gap" that drift apart would let the picker justify
itself with a number the study never produced.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

import asset_study
from signals import regime

from .core import CASH

PICK_YEARS = 2
CHECK_YEARS = 1

# Relaxed from the funnel's 15/6 because the pick window is two years
# rather than three. Still enough that a lineup cannot rest on a handful
# of trades.
MIN_TRADES = {"reversion": 10, "breakout": 4, "momentum": 4}

MAX_SAFE_CORRELATION = 0.5


def _window_bars(frame: pd.DataFrame, as_of: dt.date, years: int) -> pd.DataFrame:
    """Bars in the `years` before `as_of`, inclusive, and nothing after."""
    if frame is None or frame.empty:
        return pd.DataFrame()
    idx = frame.index
    local = idx.tz_convert(None) if getattr(idx, "tz", None) else idx
    start = as_of - dt.timedelta(days=int(365.25 * years))
    return frame[(local.date > start) & (local.date <= as_of)]


def _trend_gap(frame: pd.DataFrame) -> float | None:
    if frame is None or len(frame) < regime.TREND_WINDOW + 20:
        return None
    gap = asset_study.trend_gap(asset_study.prepare(frame))["gap"]
    return None if gap is None else float(gap)


def _downtrend_days(frame: pd.DataFrame) -> pd.Series:
    """Days the engine closed at or below its 200-day average."""
    close = frame["Close"]
    line = regime.trend_average(close)
    # A day with no 200-day average yet is not a downtrend day. Reusing
    # asset_study's cast keeps "unknown" distinct from "false" and avoids
    # the deprecated in-place fill on an object column.
    return asset_study._as_bool((close <= line).where(line.notna()), default=False)


def pick(bars: dict[str, pd.DataFrame], as_of: dt.date,
         engine_candidates: list[str], safe_candidates: list[str],
         warmup_years: int = 2) -> dict:
    """The lineup as it would have been chosen on `as_of`.

    `bars` may contain history after `as_of`; everything later is cut
    before a single number is computed. Supplying only earlier data must
    give the same answer, which is what the look-ahead check asserts.
    """
    # Enough history before the pick window for a 200-day average to exist
    # on its first day, and not one bar after as_of.
    span = PICK_YEARS + warmup_years
    windowed = {t: _window_bars(f, as_of, span) for t, f in bars.items()}

    engines = []
    for ticker in sorted(engine_candidates):
        gap = _trend_gap(windowed.get(ticker))
        engines.append({"ticker": ticker, "trend_gap": gap,
                        "usable": gap is not None})
    usable = [e for e in engines if e["usable"]]
    if not usable:
        return {"ok": False, "as_of": as_of.isoformat(),
                "reason": "no engine candidate has enough history by %s" % as_of,
                "engines": engines, "safes": [], "engine": None, "safe": CASH}

    best = max(usable, key=lambda e: (e["trend_gap"], e["ticker"]))
    engine = best["ticker"]

    engine_frame = windowed[engine]
    down = _downtrend_days(engine_frame)
    engine_returns = engine_frame["Close"].pct_change()

    safes = []
    for ticker in sorted(safe_candidates):
        if ticker == CASH:
            continue
        frame = windowed.get(ticker)
        if frame is None or frame.empty:
            safes.append({"ticker": ticker, "usable": False,
                          "why": "no bars by this date"})
            continue
        returns = frame["Close"].pct_change().reindex(engine_frame.index)
        on_down = returns[down.reindex(returns.index).fillna(False)]
        mean = float(on_down.dropna().mean()) if on_down.notna().any() else None
        corr = asset_study._correlation(returns, engine_returns)
        safes.append({
            "ticker": ticker,
            "mean_return_on_engine_downtrend": mean,
            "correlation": corr,
            "usable": mean is not None and corr is not None
                      and corr < MAX_SAFE_CORRELATION,
            "why": ("correlation %.2f is too high to diversify" % corr
                    if corr is not None and corr >= MAX_SAFE_CORRELATION
                    else "not enough overlapping history" if mean is None or corr is None
                    else "mean %+.4f%% a day while the engine fell" % (mean * 100)),
        })

    qualified = [s for s in safes if s["usable"]]
    if qualified:
        chosen = max(qualified,
                     key=lambda s: (s["mean_return_on_engine_downtrend"], s["ticker"]))
        safe = chosen["ticker"]
    else:
        # Rupees. Not a failure: holding cash through a downtrend is a
        # real position, and inventing a correlated "safe" asset would be
        # worse than admitting none qualified.
        safe = CASH

    return {"ok": True, "as_of": as_of.isoformat(), "engine": engine,
            "safe": safe, "engines": engines, "safes": safes,
            "pick_window_years": PICK_YEARS, "check_window_years": CHECK_YEARS,
            "min_trades": dict(MIN_TRADES)}


def lineup(result: dict) -> tuple:
    """Just the two names, for comparing one pick against another."""
    return (result.get("engine"), result.get("safe"))


def quarter_ends(first, last) -> list[dt.date]:
    """Every quarter end in the range, inclusive."""
    out = []
    year, quarter = first.year, (first.month - 1) // 3
    while True:
        month = quarter * 3 + 3
        day = dt.date(year, month, {3: 31, 6: 30, 9: 30, 12: 31}[month])
        if day > last:
            break
        if day >= first:
            out.append(day)
        quarter += 1
        if quarter == 4:
            quarter, year = 0, year + 1
    return out


def truncate(bars: dict, as_of: dt.date) -> dict:
    """The same bars with everything after `as_of` physically removed."""
    out = {}
    for ticker, frame in bars.items():
        if frame is None or frame.empty:
            continue
        idx = frame.index
        local = idx.tz_convert(None) if getattr(idx, "tz", None) else idx
        out[ticker] = frame[local.date <= as_of]
    return out


def verify_causal(bars: dict, as_of: dt.date, engine_candidates: list[str],
                  safe_candidates: list[str]) -> dict:
    """The lineup must not depend on data that did not exist yet.

    Truncation rather than inspection, for the same reason as Phase 3: a
    look-ahead bug in a picker looks exactly like a correct picker until
    the future is taken away.
    """
    full = pick(bars, as_of, engine_candidates, safe_candidates)
    cut = pick(truncate(bars, as_of), as_of, engine_candidates, safe_candidates)
    return {"as_of": as_of.isoformat(), "full": lineup(full),
            "truncated": lineup(cut), "match": lineup(full) == lineup(cut)}


def backtest_picks(bars: dict, engine_candidates: list[str],
                   safe_candidates: list[str], first: dt.date,
                   last: dt.date) -> list[dict]:
    """The lineup as it would have been chosen at every quarter end."""
    out = []
    for day in quarter_ends(first, last):
        result = pick(bars, day, engine_candidates, safe_candidates)
        out.append({"as_of": day.isoformat(), "ok": result["ok"],
                    "engine": result.get("engine"), "safe": result.get("safe"),
                    "reason": result.get("reason", "")})
    return out
