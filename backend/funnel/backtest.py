"""Running one idea through the engine the Phase 3 proof vouched for.

Nothing here re-implements a rule or a fill. It drives `engine.replay`,
which Phase 3 showed produces trade-for-trade identical results to the
live drip-feed, so a funnel verdict is about the idea rather than about a
second backtester nobody has checked.

Variants are the awkward part. The engine looks a strategy up by id in the
module-level STRATEGIES registry, so a neighbour - the same rule with one
parameter moved - has to be visible there while it runs. `registered()`
puts it in under a temporary id and takes it out again, under a lock. A
registry parameter threaded through the engine would be tidier and would
touch process_bar, leaderboard and adopt_new_strategies; this is contained
to one context manager that can be read in full.
"""
from __future__ import annotations

import contextlib
import copy
import threading

from arena import engine, markets as markets_mod
from arena.strategies import STRATEGIES

_registry_lock = threading.RLock()


@contextlib.contextmanager
def registered(strategy, variant_id: str):
    """Make `strategy` visible to the engine as `variant_id`, briefly."""
    with _registry_lock:
        if variant_id in STRATEGIES:
            raise ValueError("%s is already a strategy id" % variant_id)
        clone = copy.copy(strategy)
        clone.id = variant_id
        STRATEGIES[variant_id] = clone
        try:
            yield clone
        finally:
            STRATEGIES.pop(variant_id, None)


# Every funnel number on record was measured at four slots, so four is what
# the funnel keeps using. Changing it is a new measurement, not a setting.
FUNNEL_SLOTS = 4


def _costs_at(multiple: float) -> dict | None:
    """Cost overrides at a multiple of the real ones (AGENTS.md §1.4)."""
    if multiple == 1.0:
        return None
    market = markets_mod.get("nse")
    return engine._scaled_cost_overrides(market, None, multiple)


def max_drawdown(trades: list[dict]) -> float:
    """Worst peak-to-trough fall of the cumulative net, in money.

    Measured on closed trades rather than on a mark-to-market equity
    curve. It understates an intraday dip, and it is the series the
    engine actually records; calling it the maximum drawdown of the
    equity curve would be a stronger claim than the data supports.
    """
    peak = running = worst = 0.0
    for trade in trades:
        running += float(trade.get("net", 0.0))
        peak = max(peak, running)
        worst = min(worst, running - peak)
    return round(worst, 2)


def run(strategy, symbols: list[str], enriched: dict, days: int,
        cost_multiple: float = 1.0, capital: float | None = None) -> dict:
    """One idea, one set of symbols, one cost assumption."""
    symbols = sorted(s for s in symbols if s in enriched)
    if not symbols:
        return {"ok": False, "reason": "no bars for any matching symbol",
                "trades": 0, "net": 0.0, "profit_factor": None,
                "max_drawdown": 0.0, "return_pct": None}

    variant_id = "funnel__%s" % strategy.id
    with registered(strategy, variant_id):
        config = {"market": "nse", "symbols": symbols,
                  "strategies": [variant_id], "enabled": True,
                  # Pinned, not inherited. The market's default is what the
                  # live dashboard happens to use today, and a recorded
                  # Phase 4 result that silently changes when someone
                  # retunes the dashboard is not a result at all.
                  "open_slots": FUNNEL_SLOTS}
        if capital:
            config["daily_capital"] = capital
        overrides = _costs_at(cost_multiple)
        if overrides:
            config["costs"] = overrides
        state = engine.replay(config, {s: enriched[s] for s in symbols}, days=days)
        book = state["books"].get(variant_id) or {"trades": [], "stats": {}}
        trades = list(book["trades"])
        # Why a signal did not become a trade. Without these a strategy the
        # engine refused on every bar and one that never signalled at all
        # look identical, and they are opposite findings.
        skipped = {}
        for day in book.get("day_history", []):
            for reason, count in (day.get("skipped") or {}).items():
                skipped[reason] = skipped.get(reason, 0) + count
        metrics = engine.window_metrics(trades)
        start_capital = float(state["config"].get("daily_capital") or 0.0) or None

    net = float(metrics["net"])
    return {
        "ok": True,
        "symbols": symbols,
        "sessions": len(state.get("replay", {}).get("sessions", [])),
        "trades": int(metrics["trades"]),
        "net": net,
        "profit_factor": metrics["profit_factor"],
        "no_losses": metrics["no_losses"],
        "win_rate": metrics["win_rate"],
        "max_drawdown": max_drawdown(trades),
        "return_pct": round(100.0 * net / start_capital, 3) if start_capital else None,
        "cost_multiple": cost_multiple,
        # The charges actually booked, so this part of the cost is measured
        # rather than reconstructed by adding an assumed figure back on.
        # `before_charges` is NOT gross: slippage is paid in the fill price,
        # not in charges, so it is still inside this number. The 0x row of
        # the sweep is the one with no slippage in it at all.
        "skipped": {k: v for k, v in sorted(skipped.items()) if v},
        "charges": round(sum(float(t.get("charges") or 0.0) for t in trades), 2),
        "before_charges": round(net + sum(float(t.get("charges") or 0.0)
                                          for t in trades), 2),
    }
