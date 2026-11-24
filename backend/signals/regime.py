"""What kind of market is this bar in?

Two readings per ticker per bar, both from daily data:

    trend   up    close is above its 200-day average
            down  it is not
    vol     volatile  20-day realised vol is above its own 252-day median
            calm      it is not

Nothing here predicts anything. It describes the weather, so a strategy can
say "only in an uptrend" or "half size when it is wild" and mean something
precise by it.

One source of truth. asset_study.py imports these rather than computing its
own 200-day average, because two definitions of "uptrend" that drift apart
is how a filter starts disagreeing with the study that justified it.

India VIX is NOT included. See `vix_state` below for what it would take.
"""
from __future__ import annotations

import pandas as pd

TREND_UP = "up"
TREND_DOWN = "down"
VOL_CALM = "calm"
VOL_VOLATILE = "volatile"

TREND_WINDOW = 200
VOL_WINDOW = 20
VOL_MEDIAN_WINDOW = 252

# Phase 2: a sizing rule every strategy may use.
VOLATILE_SIZE_MULTIPLE = 0.5


def trend_average(close: pd.Series) -> pd.Series:
    """The 200-day average, strictly.

    `indicators.sma` starts emitting after half a window so a live screen is
    not blank for a quarter. That is right for a screen and wrong for a
    regime: it would call the warm-up an uptrend and let a filter pass on
    evidence that does not exist yet.
    """
    return close.rolling(TREND_WINDOW, min_periods=TREND_WINDOW).mean()


def realised_vol(close: pd.Series) -> pd.Series:
    return close.pct_change().rolling(VOL_WINDOW, min_periods=VOL_WINDOW).std()


def vol_median(vol: pd.Series) -> pd.Series:
    """The rolling one-year median of realised vol.

    A median rather than a mean: volatility is spiky, and one crisis month
    drags a mean up far enough that the months after it read as "calm".
    """
    return vol.rolling(VOL_MEDIAN_WINDOW, min_periods=VOL_WINDOW).median()


def classify(daily: pd.DataFrame) -> pd.DataFrame:
    """Per-bar trend and vol, plus the raw numbers behind them.

    Rows without enough history get None rather than a guess. "Unknown" and
    "calm" must not be the same value: a filter that treats the warm-up as
    calm would size up through exactly the stretch it cannot see.
    """
    close = pd.to_numeric(daily["Close"], errors="coerce")
    out = pd.DataFrame(index=daily.index)
    out["close"] = close
    out["sma200"] = trend_average(close)
    out["vol20"] = realised_vol(close)
    out["vol_median"] = vol_median(out["vol20"])

    known_trend = out["sma200"].notna() & close.notna()
    out["trend"] = pd.Series(
        [None] * len(out), index=out.index, dtype="object")
    out.loc[known_trend & (close > out["sma200"]), "trend"] = TREND_UP
    out.loc[known_trend & (close <= out["sma200"]), "trend"] = TREND_DOWN

    known_vol = out["vol20"].notna() & out["vol_median"].notna()
    out["vol"] = pd.Series([None] * len(out), index=out.index, dtype="object")
    out.loc[known_vol & (out["vol20"] > out["vol_median"]), "vol"] = VOL_VOLATILE
    out.loc[known_vol & (out["vol20"] <= out["vol_median"]), "vol"] = VOL_CALM
    return out


def at(regimes: pd.DataFrame, when) -> dict:
    """The regime as known at the close of `when`.

    A strategy acting on the next session must use the state from the
    session that has finished, never the one it is trading into.
    """
    if when not in regimes.index:
        return {"trend": None, "vol": None}
    row = regimes.loc[when]
    return {"trend": row["trend"], "vol": row["vol"]}


def size_multiple(vol_state) -> float:
    """Phase 2's sizing rule: half size when the market is volatile.

    An unknown regime is treated as volatile. Not knowing is not the same
    as calm, and the cheaper mistake is to be half-sized in a quiet market
    rather than full-sized in a wild one.
    """
    return 1.0 if vol_state == VOL_CALM else VOLATILE_SIZE_MULTIPLE


def passes(filter_spec, regime: dict) -> bool:
    """Does this bar's regime satisfy a rule's filter?

    `filter_spec` is None or "none" for a strategy that takes every bar,
    or a dict like {"trend": "up"} / {"vol": "calm"}. An unknown regime
    fails any filter that asks about it: a filter nobody can evaluate must
    not quietly pass.
    """
    if not filter_spec or filter_spec == "none":
        return True
    for key in ("trend", "vol"):
        want = filter_spec.get(key)
        if want and regime.get(key) != want:
            return False
    return True


# --------------------------------------------------------------- India VIX

VIX_SYMBOL = "^INDIAVIX"
VIX_MEDIAN_WINDOW = 252


def vix_state(vix_close: pd.Series) -> pd.Series:
    """High or low against its own one-year median.

    NOT WIRED IN. The brief says to include India VIX only if a free,
    reliable source exists, and this environment cannot reach a data source
    to find out: Yahoo is blocked by the container's egress policy, so
    claiming `^INDIAVIX` works would be a guess dressed as a finding.

    The function is here, pure and tested, so that turning it on is a
    one-line change once the symbol has actually been resolved in CI. Until
    then `classify` does not call it and no rule may filter on vix.
    """
    median = vix_close.rolling(VIX_MEDIAN_WINDOW, min_periods=30).median()
    out = pd.Series([None] * len(vix_close), index=vix_close.index, dtype="object")
    known = vix_close.notna() & median.notna()
    out[known & (vix_close > median)] = "high"
    out[known & (vix_close <= median)] = "low"
    return out
