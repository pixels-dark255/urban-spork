"""Phase 1: how does each ticker actually move?

Not a strategy. This labels each instrument by its observed behaviour over
the training window, so later phases pick assets on evidence rather than on
the fact that everyone trades Reliance.

Three labels and two measures per ticker:

  keeps going   a new 50-day high in an uptrend is followed by a better
                20-day return than the ticker's own average
  snaps back    a sharp drop in an uptrend is followed by a better 5-day
                return than average
  safe          it holds up while the engines are in a downtrend, and does
                not simply move with them

  trend gap     how much better the ticker does above its 200-day average
                than below it
  calm/volatile how much bigger its daily move is in volatile stretches

Two deliberate choices that change the numbers:

1. Every forward return is measured from the NEXT session's open, not from
   the close that produced the signal. AGENTS.md §1.3 forbids acting on the
   bar that generated a signal, and a study that quietly measured what you
   could not have traded would flatter every label here.

2. The 200-day average needs 200 days. `indicators.sma` deliberately starts
   emitting after half a window so a live screen is not blank for a
   quarter, which is right there and wrong here: it would invent an uptrend
   out of the first hundred bars and manufacture events inside the warm-up.
   So the rolling means here are strict, and the warm-up is simply NaN.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import indicators

# Thresholds from the Phase 1 brief. A label needs enough events to be more
# than an anecdote; "it worked both times" is not evidence.
MOMENTUM_LOOKBACK = 50
MOMENTUM_HORIZON = 20
MOMENTUM_MIN_EVENTS = 6

REVERSION_RSI = 2
REVERSION_RSI_MAX = 10.0
REVERSION_HORIZON = 5
REVERSION_MIN_EVENTS = 12

TREND_WINDOW = 200
VOL_WINDOW = 20
VOL_MEDIAN_WINDOW = 252

# "Does not simply move with the engines." Above this, a ticker that looks
# calm is mostly just the index in disguise.
SAFE_MAX_CORRELATION = 0.5


# ------------------------------------------------------------- preparation

def prepare(daily: pd.DataFrame) -> pd.DataFrame:
    """Add the columns every measure below reads.

    `fwd_*` are open-to-open returns starting at the next session, which is
    the earliest entry a signal on today's close could actually get.
    """
    out = pd.DataFrame(index=daily.index)
    out["close"] = pd.to_numeric(daily["Close"], errors="coerce")
    out["open"] = pd.to_numeric(daily.get("Open", daily["Close"]), errors="coerce")
    out["ret"] = out["close"].pct_change()

    out["sma200"] = out["close"].rolling(TREND_WINDOW, min_periods=TREND_WINDOW).mean()
    out["above"] = out["close"] > out["sma200"]
    out["high50"] = out["close"].rolling(
        MOMENTUM_LOOKBACK, min_periods=MOMENTUM_LOOKBACK).max()
    out["rsi2"] = indicators.rsi(out["close"], REVERSION_RSI)

    out["vol20"] = out["ret"].rolling(VOL_WINDOW, min_periods=VOL_WINDOW).std()
    out["vol_median"] = out["vol20"].rolling(
        VOL_MEDIAN_WINDOW, min_periods=VOL_WINDOW).median()

    entry = out["open"].shift(-1)                 # tomorrow's open
    for horizon in (MOMENTUM_HORIZON, REVERSION_HORIZON):
        exit_ = out["open"].shift(-(horizon + 1))
        out["fwd%d" % horizon] = exit_ / entry - 1
    return out


def _as_bool(series: pd.Series, default: bool = False) -> pd.Series:
    """A shifted boolean column comes back as object dtype holding NaN.

    Filling that in place is deprecated and will change behaviour, so the
    cast is explicit: a missing state is not a state, and `default` says
    which way an unknown day is counted.
    """
    return series.astype("object").where(series.notna(), default).astype(bool)


def _mean(series: pd.Series) -> float | None:
    clean = series.dropna()
    return float(clean.mean()) if len(clean) else None


# ------------------------------------------------------------ the labels

def momentum(frame: pd.DataFrame) -> dict:
    """"Keeps going": a new 50-day high while above the 200-day average."""
    events = (frame["close"] >= frame["high50"]) & frame["above"]
    return _compare(frame, events, MOMENTUM_HORIZON, MOMENTUM_MIN_EVENTS,
                    "keeps going")


def reversion(frame: pd.DataFrame) -> dict:
    """"Snaps back": a sharp drop while still above the 200-day average."""
    events = (frame["rsi2"] < REVERSION_RSI_MAX) & frame["above"]
    return _compare(frame, events, REVERSION_HORIZON, REVERSION_MIN_EVENTS,
                    "snaps back")


def _compare(frame: pd.DataFrame, events: pd.Series, horizon: int,
             min_events: int, name: str) -> dict:
    """Conditional forward return against the unconditional one."""
    column = "fwd%d" % horizon
    forward = frame[column]
    # An event in the last `horizon` sessions has no forward return yet.
    # Dropping it is not optional: counting it as flat would drag every
    # average toward zero in exactly the busiest recent stretch.
    hit = forward[_as_bool(events) & forward.notna()]
    base = _mean(forward)
    conditional = _mean(hit)
    count = int(len(hit))
    edge = (None if conditional is None or base is None
            else conditional - base)
    return {
        "label": name,
        "events": count,
        "conditional": conditional,
        "unconditional": base,
        "edge": edge,
        "min_events": min_events,
        "horizon": horizon,
        "labelled": bool(count >= min_events and edge is not None and edge > 0),
        "why": _why(count, min_events, edge),
    }


def _why(count: int, min_events: int, edge: float | None) -> str:
    if count < min_events:
        return "only %d events, needs %d" % (count, min_events)
    if edge is None:
        return "no forward returns available"
    if edge <= 0:
        return "%d events but %.2f%% worse than average" % (count, edge * 100)
    return "%d events, %.2f%% better than average" % (count, edge * 100)


# ----------------------------------------------------------- the measures

def trend_gap(frame: pd.DataFrame) -> dict:
    """Return above the 200-day average minus return below it.

    The state is taken from the PREVIOUS close. Using the same day's close
    to both decide "above" and supply the return is circular: a large up
    day is itself what pushes the close above the average, so the gap would
    partly measure its own definition.
    """
    shifted = frame["above"].shift(1)
    known = frame["sma200"].shift(1).notna()
    above = _as_bool(shifted) & known
    below = _as_bool(shifted, default=True).eq(False) & known
    ret = frame["ret"]
    hi, lo = _mean(ret[above]), _mean(ret[below])
    return {"above": hi, "below": lo,
            "gap": None if hi is None or lo is None else hi - lo,
            "days_above": int(above.sum()), "days_below": int(below.sum())}


def calm_vs_volatile(frame: pd.DataFrame) -> dict:
    """Average absolute daily move in calm stretches versus volatile ones."""
    vol, median = frame["vol20"].shift(1), frame["vol_median"].shift(1)
    known = vol.notna() & median.notna()
    size = frame["ret"].abs()
    calm = _mean(size[known & (vol <= median)])
    rough = _mean(size[known & (vol > median)])
    return {"calm": calm, "volatile": rough,
            "ratio": None if not calm or rough is None else rough / calm}


def safety(frame: pd.DataFrame, engines: dict[str, pd.DataFrame]) -> dict:
    """Does it hold up when the engines are falling, without tracking them?

    "The engines are below their 200-day average" is taken as at least half
    of them being below, rather than all four. Requiring all four makes the
    condition fire only in the deepest drawdowns, which is too rare a sample
    to call anything safe.
    """
    if not engines:
        return {"labelled": False, "events": 0, "mean_return": None,
                "max_correlation": None, "why": "no engine candidates given"}

    flags, returns = [], {}
    for name, engine in engines.items():
        flags.append((~engine["above"]).astype(float).reindex(frame.index))
        returns[name] = engine["ret"].reindex(frame.index)
    downtrend = (pd.concat(flags, axis=1).mean(axis=1) >= 0.5).shift(1)

    mine = frame["ret"]
    on_days = mine[_as_bool(downtrend) & mine.notna()]
    correlations = {name: _correlation(mine, series)
                    for name, series in returns.items()}
    known = [c for c in correlations.values() if c is not None]
    worst = max(known) if known else None
    mean_return = _mean(on_days)

    labelled = bool(mean_return is not None and mean_return > 0
                    and worst is not None and worst < SAFE_MAX_CORRELATION)
    return {
        "labelled": labelled, "events": int(len(on_days)),
        "mean_return": mean_return, "max_correlation": worst,
        "correlations": correlations,
        "why": _safe_why(mean_return, worst, len(on_days)),
    }


def _safe_why(mean_return, worst, days) -> str:
    if not days:
        return "the engines were never in a downtrend in this window"
    if mean_return is None or worst is None:
        return "not enough overlapping history"
    parts = ["%.3f%% a day while engines fell" % (mean_return * 100)]
    parts.append("correlation %.2f" % worst)
    if mean_return <= 0:
        parts.append("loses money in exactly the stretch it is meant to help")
    elif worst >= SAFE_MAX_CORRELATION:
        parts.append("too correlated to be a diversifier")
    return ", ".join(parts)


def _correlation(a: pd.Series, b: pd.Series) -> float | None:
    joined = pd.concat([a, b], axis=1).dropna()
    if len(joined) < 30 or joined.iloc[:, 0].std() == 0 or joined.iloc[:, 1].std() == 0:
        return None
    value = float(joined.iloc[:, 0].corr(joined.iloc[:, 1]))
    return None if np.isnan(value) else value


def correlation_matrix(frames: dict[str, pd.DataFrame]) -> dict:
    """Daily-return correlation between every pair, for the heatmap."""
    names = sorted(frames)
    returns = pd.DataFrame({n: frames[n]["ret"] for n in names})
    matrix = returns.corr()
    rows = []
    for a in names:
        rows.append([None if pd.isna(matrix.loc[a, b]) else round(float(matrix.loc[a, b]), 3)
                     for b in names])
    return {"tickers": names, "matrix": rows}


# ------------------------------------------------------------ the summary

def _pct(value, digits=2) -> str:
    return "n/a" if value is None else "%.*f%%" % (digits, value * 100)


def describe(result: dict) -> str:
    """One plain-English line. What a person would say about this ticker."""
    parts = []
    mom, rev = result["momentum"], result["reversion"]
    if mom["labelled"]:
        parts.append("keeps going after a new %d-day high (%s better over %d days, %d times)"
                     % (MOMENTUM_LOOKBACK, _pct(mom["edge"]), mom["horizon"], mom["events"]))
    if rev["labelled"]:
        parts.append("snaps back from sharp drops (%s better over %d days, %d times)"
                     % (_pct(rev["edge"]), rev["horizon"], rev["events"]))
    if not parts:
        parts.append("shows neither a momentum nor a snap-back edge worth the name")

    gap = result["trend_gap"]["gap"]
    if gap is not None:
        direction = "better" if gap > 0 else "worse"
        parts.append("does %s a day %s above its 200-day average than below"
                     % (_pct(abs(gap), 3), direction))

    ratio = result["calm_vs_volatile"]["ratio"]
    if ratio is not None:
        parts.append("moves %.1fx bigger in volatile stretches" % ratio)

    safe = result["safety"]
    if safe["labelled"]:
        parts.append("and holds up when the engines fall (correlation %.2f), so it can act as ballast"
                     % safe["max_correlation"])
    elif safe["max_correlation"] is not None:
        parts.append("and is not ballast: %s" % safe["why"])

    return "%s %s." % (result["ticker"], "; ".join(parts))


def labels_of(result: dict) -> list[str]:
    out = []
    if result["momentum"]["labelled"]:
        out.append("keeps going")
    if result["reversion"]["labelled"]:
        out.append("snaps back")
    if result["safety"]["labelled"]:
        out.append("safe")
    return out


def study_one(ticker: str, frame: pd.DataFrame,
              engines: dict[str, pd.DataFrame]) -> dict:
    result = {
        "ticker": ticker,
        "bars": int(frame["close"].notna().sum()),
        "momentum": momentum(frame),
        "reversion": reversion(frame),
        "trend_gap": trend_gap(frame),
        "calm_vs_volatile": calm_vs_volatile(frame),
        "safety": safety(frame, engines),
    }
    result["labels"] = labels_of(result)
    result["summary"] = describe(result)
    return result


def restrict(frame: pd.DataFrame, start, end) -> pd.DataFrame:
    """Keep only rows inside the window, after the columns are computed.

    Order matters. The 200-day average and the 50-day high must be built
    from bars BEFORE the window opens, or the first year of the window has
    no usable state and a third of the events never fire. But the rows that
    get labelled must lie inside the window, so the warm-up is cut away
    afterwards rather than before.

    Nothing after `end` is supplied at all, which is what keeps the locked
    window sealed: an event in the last few sessions of training simply has
    no forward return, and `_compare` drops it rather than peeking.
    """
    idx = frame.index
    local = idx.tz_convert(None) if getattr(idx, "tz", None) else idx
    return frame[(local.date >= start) & (local.date <= end)]


def study(daily: dict[str, pd.DataFrame], study_tickers: list[str],
          engine_tickers: list[str], window: tuple | None = None) -> dict:
    """Every study ticker, labelled. Sorted, so two runs agree.

    `daily` should already include the warm-up bars before `window`.
    """
    prepared = {name: prepare(frame) for name, frame in sorted(daily.items())
                if frame is not None and not frame.empty}
    if window:
        prepared = {name: restrict(f, window[0], window[1])
                    for name, f in prepared.items()}
        prepared = {name: f for name, f in prepared.items() if not f.empty}
    engines = {name: prepared[name] for name in sorted(engine_tickers)
               if name in prepared}

    rows, missing = [], []
    for ticker in sorted(study_tickers):
        if ticker not in prepared:
            missing.append(ticker)
            continue
        rows.append(study_one(ticker, prepared[ticker], engines))

    heat = correlation_matrix({t: prepared[t] for t in sorted(study_tickers)
                               if t in prepared})
    return {
        "rows": rows,
        "missing": missing,
        "engines": sorted(engines),
        "heatmap": heat,
        "thresholds": {
            "momentum_min_events": MOMENTUM_MIN_EVENTS,
            "reversion_min_events": REVERSION_MIN_EVENTS,
            "safe_max_correlation": SAFE_MAX_CORRELATION,
        },
    }


def fingerprint_values(result: dict) -> dict:
    """The numbers that define this phase's result."""
    values = {"tickers": len(result["rows"]), "missing": len(result["missing"])}
    for row in result["rows"]:
        key = row["ticker"]
        values[key + ":labels"] = ",".join(row["labels"]) or "none"
        values[key + ":mom_events"] = row["momentum"]["events"]
        values[key + ":rev_events"] = row["reversion"]["events"]
        values[key + ":gap"] = row["trend_gap"]["gap"]
        values[key + ":corr"] = row["safety"]["max_correlation"]
    return values
