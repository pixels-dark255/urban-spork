"""The look-ahead guard, pointed at the strategies that actually run.

Each Arena strategy is wrapped as `compute(bars, when) -> signal`, where
the bars are enriched first. Truncating at `when` therefore re-runs
`enrich` as well, which is the point: most look-ahead lives in indicator
construction rather than in the rule that reads it. A groupby spanning a
whole session, a max over a column that has not finished, a window that
is centred instead of trailing - none of them look wrong in the source,
and every one of them changes when the future is removed.

A strategy that fails is reported and disabled. It is not silently
dropped and it is not left running: a rule that reads the future produces
results that cannot be acted on, and leaving it in the leaderboard means
comparing a real strategy against a cheating one.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from arena import markets as markets_mod
from arena.strategies import STRATEGIES, enrich
from signals import regime

from . import lookahead

SESSION_BARS = 75          # 09:15 to 15:30 IST in five-minute bars


def intraday_bars(sessions: int = 12, seed: int = 20260325,
                  start: str = "2026-09-01", sigma_pct: float = 0.15):
    """Synthetic 5-minute NSE sessions.

    Real bars would be better and are not available here (the container
    cannot reach a data source), but for this guard synthetic bars are
    not a compromise: the question is whether a value at bar N changes
    when bars after N are removed, and that is a property of the code, not
    of the prices.
    """
    rng = np.random.default_rng(seed)
    stamps, opens, closes = [], [], []
    price = 1000.0
    # A stdlib timedelta: pandas is deprecating the implicit unit that
    # a bare integer carries, and this sidesteps it entirely.
    one_day = dt.timedelta(days=1)
    day = pd.Timestamp(start, tz="Asia/Kolkata")
    made = 0
    while made < sessions:
        if day.weekday() >= 5:
            day += one_day
            continue
        # date_range rather than a Timedelta per bar: pandas is deprecating
        # the implicit unit on bare-integer arithmetic, and this is both
        # future-proof and the clearer statement of "75 five-minute bars".
        stamps.extend(pd.date_range(day.replace(hour=9, minute=15),
                                    periods=SESSION_BARS, freq="5min"))
        price *= 1 + rng.normal(0, 0.004)          # overnight gap
        for _ in range(SESSION_BARS):
            o = price
            price = o * (1 + rng.normal(0, sigma_pct / 100.0))
            opens.append(o)
            closes.append(price)
        made += 1
        day += one_day

    opens = np.array(opens)
    closes = np.array(closes)
    span = np.abs(closes - opens)
    wick = rng.uniform(0, 0.5, len(opens)) * np.maximum(span, opens * 0.0005)
    return pd.DataFrame({
        "Open": opens,
        "High": np.maximum(opens, closes) + wick,
        "Low": np.minimum(opens, closes) - wick,
        "Close": closes,
        "Volume": rng.integers(1_000, 20_000, len(opens)).astype(float),
    }, index=pd.DatetimeIndex(stamps))


def _signal_of(strategy):
    """Wrap a strategy as compute(bars, when) for the guard."""
    market = markets_mod.get("nse")

    def compute(bars, when):
        frame = enrich(bars, market=market)
        if frame.empty or when not in frame.index:
            return None
        position = frame.index.get_loc(when)
        if position < 1:
            return None
        row, prev = frame.iloc[position], frame.iloc[position - 1]
        signal = strategy.entry(row, prev)
        if not signal:
            return None
        # Round: the question is whether the DECISION changed, not whether
        # a float moved in its last bits through a different summation
        # order on a shorter array.
        return {"side": signal.get("side", "LONG"),
                "stop": round(float(signal["stop"]), 4),
                "target": round(float(signal["target"]), 4)}

    return compute


def _regime_signal(bars, when):
    daily = bars["Close"].resample("1D").last().dropna()
    frame = pd.DataFrame({"Close": daily})
    out = regime.classify(frame)
    day = pd.Timestamp(when).normalize()
    if day not in out.index:
        return None
    row = out.loc[day]
    return {"trend": row["trend"], "vol": row["vol"]}


def cheating_compute(bars, when):
    """The control. If this passes, the guard has no teeth.

    "Every strategy passed" means nothing unless the same harness can be
    shown to fail. So this does what a real look-ahead bug does: it builds
    an indicator with a groupby over the whole session - a session high
    that includes bars which have not happened yet - and reads it. The
    rule itself is innocent; the column is not.
    """
    frame = enrich(bars, market=markets_mod.get("nse"))
    if frame.empty or when not in frame.index:
        return None
    # Session high over the ENTIRE session, future bars included.
    peek = frame["High"].groupby(frame["session"]).transform("max")
    position = frame.index.get_loc(when)
    return {"side": "LONG", "stop": round(float(peek.iloc[position]), 4),
            "target": 0.0}


def run(sessions: int = 12, dates: int = 30, seed: int = 20260325) -> dict:
    """Guard every Arena strategy, plus the regime layer."""
    bars = intraday_bars(sessions=sessions, seed=seed)
    results, failed = [], []
    for sid in sorted(STRATEGIES):
        strategy = STRATEGIES[sid]
        report = lookahead.check(_signal_of(strategy), bars,
                                 dates=dates, seed=seed, label=sid)
        results.append({"id": sid, "name": strategy.name,
                        "checked": report["checked"],
                        "ok": report["ok"],
                        "mismatches": len(report["mismatches"]),
                        "first": report["mismatches"][0] if report["mismatches"] else None})
        if not report["ok"]:
            failed.append(sid)

    # The regime layer is daily, so it is sampled on its own frame.
    daily = bars["Close"].resample("1D").last().dropna()
    regime_report = lookahead.check(
        _regime_signal, bars.loc[bars.index.normalize().isin(daily.index)],
        dates=min(dates, max(0, len(daily) - 2)), seed=seed, label="regime")

    # The control: the same harness, pointed at a signal that is known to
    # cheat. If this does not fail, nothing above is evidence of anything.
    control = lookahead.check(cheating_compute, bars, dates=dates,
                              seed=seed, label="control (deliberate cheat)")

    return {"strategies": results, "failed": sorted(failed),
            "control": {"caught": not control["ok"],
                        "checked": control["checked"],
                        "mismatches": len(control["mismatches"])},
            "regime": {"ok": regime_report["ok"],
                       "checked": regime_report["checked"],
                       "mismatches": len(regime_report["mismatches"])},
            "sessions": sessions, "dates": dates, "seed": seed}
