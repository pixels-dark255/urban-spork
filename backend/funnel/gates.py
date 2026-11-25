"""The seven gates, in order.

Every gate returns the same shape: did it pass, and why not. "Why not" is
the point of the whole exercise. A funnel that reports "3 of 13 survived"
tells you nothing you can act on; one that says "eight died at the
neighbour gate because their edge lives at a single parameter value" tells
you what the strategies are actually worth.

Gates are evaluated in order and stop at the first failure, because a
later gate's number is meaningless once an earlier one has failed: a
profit factor computed from four trades is not a profit factor.
"""
from __future__ import annotations

import json
import pathlib

LABELS_PATH = (pathlib.Path(__file__).resolve().parent.parent.parent
               / "results" / "asset_labels.json")

# Which Phase 1 label an idea needs from the assets it trades.
KIND_WANTS_LABEL = {
    "breakout": "keeps_going",
    "momentum": "keeps_going",
    "reversion": "snaps_back",
}

# Gate 3. The brief gives daily and hourly; it does not give 5m or 15m, so
# those reuse the hourly floor. That is a floor and not a calibration: a
# 5-minute strategy sees far more bars than an hourly one and ought to
# clear a higher bar, so passing here is weaker evidence than passing on
# daily. Recorded rather than silently invented.
TRADE_MINIMUMS = {
    "1d": {"reversion": 15, "breakout": 6, "momentum": 6},
    "1h": {"reversion": 40, "breakout": 15, "momentum": 15},
}
INTRADAY_FLOOR = TRADE_MINIMUMS["1h"]

MIN_PROFIT_FACTOR = 1.3
DOUBLE_COST = 2.0


def load_labels(path: pathlib.Path | None = None) -> dict:
    return json.loads((path or LABELS_PATH).read_text())


def minimum_trades(kind: str, bar_size: str) -> int:
    table = TRADE_MINIMUMS.get(bar_size, INTRADAY_FLOOR)
    return table.get(kind, max(table.values()))


# ------------------------------------------------------------- the gates

def gate_fits_asset(idea, labels: dict) -> dict:
    """1. The asset must behave the way the idea assumes it does."""
    kind = idea.kind
    if kind == "benchmark":
        return {"pass": False, "why": "a yardstick, not an idea: never given capital",
                "symbols": []}
    want = KIND_WANTS_LABEL.get(kind)
    if not want:
        return {"pass": False,
                "why": "kind %r matches no asset label, so there is no set of "
                       "assets this idea is entitled to trade" % kind,
                "symbols": []}
    matching = sorted(t for t, row in labels["labels"].items() if row.get(want))
    if not matching:
        return {"pass": False,
                "why": "no studied ticker is labelled %r" % want, "symbols": []}
    return {"pass": True, "symbols": matching,
            "why": "%d ticker(s) labelled %r: %s"
                   % (len(matching), want, ", ".join(matching))}


def gate_training_profit(result: dict) -> dict:
    """2. Profitable on the training window, after costs."""
    if not result.get("ok"):
        return {"pass": False, "why": result.get("reason", "no result")}
    net = result["net"]
    return {"pass": net > 0,
            "why": "net %+.2f after costs over %d trades" % (net, result["trades"])}


def gate_enough_trades(idea, result: dict) -> dict:
    """3. Enough trades for the number to mean anything."""
    need = minimum_trades(idea.kind, idea.bar_size)
    n = result.get("trades", 0)
    return {"pass": n >= need, "need": need,
            "why": "%d trades, needs %d for a %s idea on %s bars"
                   % (n, need, idea.kind, idea.bar_size)}


def gate_profit_factor(result: dict) -> dict:
    """4. Profit factor at or above 1.3."""
    pf = result.get("profit_factor")
    if pf is None:
        # No losing trades at all. That is not an infinite profit factor,
        # it is too few trades to have met a loss yet.
        if result.get("no_losses"):
            return {"pass": False,
                    "why": "no losing trades yet, so the profit factor is "
                           "undefined rather than excellent"}
        return {"pass": False, "why": "no profit factor (no trades)"}
    return {"pass": pf >= MIN_PROFIT_FACTOR,
            "why": "profit factor %.2f, needs %.2f" % (pf, MIN_PROFIT_FACTOR)}


def gate_neighbours(neighbour_results: list[dict]) -> dict:
    """5. Every neighbour profitable: a single-spike optimum fails.

    An edge that exists at RSI 30 and vanishes at 25 and 35 is not an
    edge, it is a coincidence that happened to be found. The gate is
    deliberately unforgiving about this because it is the single most
    common way a backtest flatters a rule.
    """
    if not neighbour_results:
        return {"pass": False,
                "why": "no varyable parameters, so a single-spike optimum "
                       "cannot be ruled out"}
    losers = [n for n in neighbour_results if not n["ok"] or n["net"] <= 0]
    return {"pass": not losers,
            "tested": len(neighbour_results),
            "losers": [n["label"] for n in losers],
            "why": ("all %d neighbours profitable" % len(neighbour_results)
                    if not losers else
                    "%d of %d neighbours lose money: %s"
                    % (len(losers), len(neighbour_results),
                       ", ".join(n["label"] for n in losers[:4])))}


def gate_double_cost(result: dict) -> dict:
    """6. Still profitable when costs are doubled."""
    if not result.get("ok"):
        return {"pass": False, "why": result.get("reason", "no result")}
    net = result["net"]
    return {"pass": net > 0, "why": "net %+.2f at 2x cost" % net}


def gate_locked(result: dict | None) -> dict:
    """7. The locked window, run once and logged (AGENTS.md §1.6)."""
    if result is None:
        return {"pass": False, "skipped": True,
                "why": "locked test not run: it may be run ONCE per idea, so "
                       "it is never spent on a dry run or on synthetic bars"}
    if not result.get("ok"):
        return {"pass": False, "why": result.get("reason", "no result")}
    net = result["net"]
    return {"pass": net > 0,
            "why": "net %+.2f on the locked window" % net}


ORDER = ["fits_asset", "training_profit", "enough_trades", "profit_factor",
         "neighbours", "double_cost", "locked"]

TITLES = {
    "fits_asset": "1. Fits the asset",
    "training_profit": "2. Profitable in training",
    "enough_trades": "3. Enough trades",
    "profit_factor": "4. Profit factor >= 1.3",
    "neighbours": "5. Neighbours all profitable",
    "double_cost": "6. Survives 2x cost",
    "locked": "7. Locked test",
}
