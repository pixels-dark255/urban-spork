"""A market whose answer is known, because we put it there.

The only way to know a backtester reports the truth is to feed it data
whose truth you already know. Two runs, identical but for one thing:

    planted   every flag day is followed by a session that rises 1.0%
              from its open to its close
    noise     the same flags, the same seed, no bump at all

A backtester that cannot recover the first has a bug. A backtester that
finds profit in the second has a worse one.

Entries fill at the NEXT session's open and exit at that session's close,
which is what AGENTS.md §1.3 requires and what arena/engine.py does not do
yet. This harness is therefore also the reference implementation of that
rule: it is small enough to read in one sitting and check by hand.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from arena import costs

# One fixed seed, one answer, every run (AGENTS.md §1.7).
DEFAULT_SEED = 20260325

# ~375 flags at this length and rate. Sized so the noise test has power:
# a per-leg sigma of 0.707% over 375 trades puts the standard error near
# 0.036%, well inside the +/-0.15% band the test asserts.
DEFAULT_BARS = 1500
DEFAULT_FLAG_RATE = 0.25
DAILY_SIGMA_PCT = 1.0

# A price where whole-share rounding and the brokerage cap are not the
# thing being measured. The result is reported per trade in percent, so
# the level only has to be sane.
START_PRICE = 1000.0
QTY = 100


def random_walk(n: int = DEFAULT_BARS, seed: int = DEFAULT_SEED,
                sigma_pct: float = DAILY_SIGMA_PCT,
                start: float = START_PRICE) -> pd.DataFrame:
    """Daily OHLC with no edge in it whatsoever.

    Each session is built as two independent halves - an overnight gap and
    an intraday move - so that an entry at the open and an exit at the
    close captures exactly the intraday half. That separation is what
    makes a planted bump measurable: anything the harness reports beyond
    the intraday move is something it invented.
    """
    rng = np.random.default_rng(seed)
    leg = (sigma_pct / 100.0) / np.sqrt(2)
    overnight = rng.normal(0.0, leg, n)
    intraday = rng.normal(0.0, leg, n)
    return _build(overnight, intraday, start, n, rng)


def _build(overnight, intraday, start, n, rng) -> pd.DataFrame:
    opens = np.empty(n)
    closes = np.empty(n)
    prev_close = start
    for i in range(n):
        opens[i] = prev_close * (1 + overnight[i])
        closes[i] = opens[i] * (1 + intraday[i])
        prev_close = closes[i]
    span = np.abs(closes - opens)
    wick = rng.uniform(0.0, 0.4, n) * np.maximum(span, opens * 0.001)
    frame = pd.DataFrame({
        "Open": opens,
        "High": np.maximum(opens, closes) + wick,
        "Low": np.minimum(opens, closes) - wick,
        "Close": closes,
        "Volume": rng.integers(50_000, 200_000, n).astype(float),
    }, index=pd.bdate_range("2019-01-01", periods=n, tz="Asia/Kolkata"))
    frame.attrs["overnight"] = overnight
    frame.attrs["intraday"] = intraday
    return frame


def planted_market(bump_pct: float, seed: int = DEFAULT_SEED,
                   n: int = DEFAULT_BARS,
                   flag_rate: float = DEFAULT_FLAG_RATE) -> tuple:
    """A market with flags, and optionally an edge after each flag.

    The flags are drawn from their own generator stream, so the SAME days
    are flagged whether or not a bump is planted. Without that, the two
    runs would differ in more than the one thing under test and neither
    number would mean anything.
    """
    base = random_walk(n, seed=seed)
    flag_rng = np.random.default_rng(seed + 1)
    flags = flag_rng.random(n) < flag_rate
    # A flag on the last bar has no following session to trade.
    flags[-1] = False

    intraday = np.array(base.attrs["intraday"], dtype=float)
    if bump_pct:
        bumped = np.roll(flags, 1)
        bumped[0] = False
        intraday[bumped] += bump_pct / 100.0
    rng = np.random.default_rng(seed + 2)
    frame = _build(base.attrs["overnight"], intraday, START_PRICE, n, rng)
    return frame, pd.Series(flags, index=frame.index)


def run_next_open_next_close(frame: pd.DataFrame, flags: pd.Series,
                             qty: int = QTY) -> dict:
    """Trade every flag: in at the next open, out at that session's close.

    Costs are the project's own Indian legs plus slippage (AGENTS.md
    §1.4). Nothing here is netted or estimated: each leg is charged the
    way the Arena charges it.
    """
    cfg = dict(costs.DEFAULT_COSTS)
    opens = frame["Open"].to_numpy()
    closes = frame["Close"].to_numpy()
    positions = np.flatnonzero(flags.to_numpy())

    gross_pcts, net_pcts, cost_pcts = [], [], []
    for i in positions:
        j = i + 1
        if j >= len(frame):
            continue
        raw_in, raw_out = opens[j], closes[j]
        buy = costs.fill_price("BUY", raw_in, cfg)
        sell = costs.fill_price("SELL", raw_out, cfg)
        charges = (costs.leg_charges("BUY", buy * qty, cfg)["total"]
                   + costs.leg_charges("SELL", sell * qty, cfg)["total"])
        notional = raw_in * qty
        gross = (raw_out - raw_in) * qty
        net = (sell - buy) * qty - charges
        gross_pcts.append(100.0 * gross / notional)
        net_pcts.append(100.0 * net / notional)
        cost_pcts.append(100.0 * (gross - net) / notional)

    return {
        "trades": len(net_pcts),
        "flag_days": int(flags.sum()),
        "avg_gross_pct": float(np.mean(gross_pcts)) if gross_pcts else 0.0,
        "avg_net_pct": float(np.mean(net_pcts)) if net_pcts else 0.0,
        "cost_pct": float(np.mean(cost_pcts)) if cost_pcts else 0.0,
    }


def run_planted_edge(bump_pct: float, seed: int = DEFAULT_SEED) -> dict:
    frame, flags = planted_market(bump_pct, seed=seed)
    out = run_next_open_next_close(frame, flags)
    out["bump_pct"] = bump_pct
    out["seed"] = seed
    return out


# ------------------------------------------------- signals for the guard

def honest_signal(frame: pd.DataFrame, when) -> float:
    """A causal signal: it reads only bars up to and including `when`."""
    upto = frame.loc[:when, "Close"]
    if len(upto) < 20:
        return 0.0
    return float(upto.iloc[-1] - upto.tail(20).mean())


def cheating_signal(frame: pd.DataFrame, when) -> float:
    """A signal that peeks at tomorrow, which is the thing being hunted.

    It is deliberately subtle: it still mostly uses today's data, and on a
    truncated frame it silently falls back to today's close rather than
    raising. A guard that only catches a blatant cheat catches nothing
    real, because a real look-ahead bug looks exactly like this.
    """
    upto = frame.loc[:when]
    if len(upto) < 20:
        return 0.0
    position = frame.index.get_loc(when)
    if position + 1 < len(frame):
        tomorrow = float(frame["Close"].iloc[position + 1])
    else:
        tomorrow = float(frame["Close"].iloc[position])
    return float(tomorrow - upto["Close"].tail(20).mean())
