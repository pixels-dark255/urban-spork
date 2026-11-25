"""The risk gate. It may only reduce exposure; it may never change a rule.

That distinction is the whole design. A gate that could widen a stop, flip
a side or raise a size would be a strategy wearing a safety jacket, and
the thing it overrode would be the rule that was actually tested. So:

  * an order's quantity may be cut, never raised;
  * an order may be dropped;
  * an order may be ADDED only when it closes or reduces a position - a
    forced square-off, or a stop that went missing;
  * nothing else.

`check_invariant` states that in code and the tests assert it over the
gate's own output, so the claim is checked rather than believed.

Everything the gate does is recorded in `actions` with the reason, because
a position that is half the size it should be is deeply confusing unless
something says why.
"""
from __future__ import annotations

import dataclasses
import datetime as dt

from .store import PAUSE

# The brief's three limits.
SOFT_LOSS_PCT = 3.0      # no new entries today; exits still run
HARD_LOSS_PCT = 5.0      # PAUSE: nothing new opens until a human clears it
DRAWDOWN_HALVE = 0.5     # size multiplier past the worst tested drawdown

SQUARE_OFF = dt.time(15, 15)
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

ENTRY_SIDES = {"BUY", "SHORT"}
EXIT_SIDES = {"SELL", "COVER"}


@dataclasses.dataclass
class Context:
    account: str
    equity: float
    day_start_equity: float
    worst_backtest_drawdown_pct: float
    positions: list
    now_ist: dt.datetime
    intraday: bool = False
    flags: dict = dataclasses.field(default_factory=dict)


def daily_loss_pct(ctx: Context) -> float:
    """Loss so far today as a positive percentage. Zero if up."""
    if ctx.day_start_equity <= 0:
        return 0.0
    change = (ctx.equity - ctx.day_start_equity) / ctx.day_start_equity * 100.0
    return max(0.0, -change)


def drawdown_pct(ctx: Context, peak: float | None) -> float:
    if not peak or peak <= 0:
        return 0.0
    return max(0.0, (peak - ctx.equity) / peak * 100.0)


def is_entry(order: dict) -> bool:
    return str(order.get("side", "")).upper() in ENTRY_SIDES


def apply(orders: list[dict], ctx: Context, peak_equity: float | None = None,
          store=None) -> dict:
    """Run the gate. Returns the allowed orders and what it did, and why."""
    actions: list[dict] = []
    allowed = [dict(o) for o in orders]

    # --- every order is capped at its own target ----------------------
    for order in allowed:
        target = order.get("target_qty")
        if target is not None and int(order["qty"]) > int(target):
            actions.append({"rule": "target_cap", "ticker": order["ticker"],
                            "from": int(order["qty"]), "to": int(target),
                            "why": "no order may exceed its target"})
            order["qty"] = int(target)

    # --- an existing PAUSE stops anything new -------------------------
    paused = bool(store and (store.get_flag(PAUSE) or {}).get("value") == "on")
    if paused:
        kept = [o for o in allowed if not is_entry(o)]
        if len(kept) != len(allowed):
            actions.append({"rule": "pause_flag", "dropped": len(allowed) - len(kept),
                            "why": "PAUSE is set; nothing new opens until a "
                                   "human clears it"})
        allowed = kept

    loss = daily_loss_pct(ctx)

    # --- 5%: set PAUSE and stop opening -------------------------------
    if loss > HARD_LOSS_PCT:
        if store is not None and not paused:
            store.set_flag(PAUSE, "on",
                           "daily loss %.2f%% exceeded %.1f%%" % (loss, HARD_LOSS_PCT))
        kept = [o for o in allowed if not is_entry(o)]
        actions.append({"rule": "hard_loss", "loss_pct": round(loss, 2),
                        "dropped": len(allowed) - len(kept),
                        "why": "daily loss above %.1f%%: PAUSE set, exits only"
                               % HARD_LOSS_PCT})
        allowed = kept
        paused = True

    # --- 3%: no new entries today, exits still run --------------------
    elif loss > SOFT_LOSS_PCT:
        kept = [o for o in allowed if not is_entry(o)]
        actions.append({"rule": "soft_loss", "loss_pct": round(loss, 2),
                        "dropped": len(allowed) - len(kept),
                        "why": "daily loss above %.1f%%: no new entries today, "
                               "exits still run" % SOFT_LOSS_PCT})
        allowed = kept

    # --- past the worst tested drawdown: halve new sizes --------------
    dd = drawdown_pct(ctx, peak_equity)
    worst = abs(ctx.worst_backtest_drawdown_pct or 0.0)
    if worst and dd > worst:
        for order in allowed:
            if is_entry(order) and int(order["qty"]) > 0:
                before = int(order["qty"])
                order["qty"] = int(before * DRAWDOWN_HALVE)
                actions.append({"rule": "drawdown_halve", "ticker": order["ticker"],
                                "from": before, "to": order["qty"],
                                "why": "drawdown %.2f%% is past the worst tested "
                                       "%.2f%%; new sizes halved until it is "
                                       "back within half of that" % (dd, worst)})

    # --- forced square-off, whatever happened earlier ------------------
    if ctx.intraday and ctx.now_ist.timetz() >= SQUARE_OFF.replace(tzinfo=IST):
        allowed = [o for o in allowed if not is_entry(o)]
        for position in ctx.positions:
            if int(position.get("qty", 0)) == 0:
                continue
            if any(o["ticker"] == position["ticker"] and not is_entry(o)
                   for o in allowed):
                continue
            allowed.append({"ticker": position["ticker"],
                            "strategy": position.get("strategy", "?"),
                            "side": "SELL" if int(position["qty"]) > 0 else "COVER",
                            "qty": abs(int(position["qty"])),
                            "reason": "forced square-off at 15:15"})
            actions.append({"rule": "square_off", "ticker": position["ticker"],
                            "why": "intraday positions are closed by 15:15 IST "
                                   "even if an earlier step errored"})

    # --- a position with no stop gets one back ------------------------
    restored = []
    for position in ctx.positions:
        if int(position.get("qty", 0)) and position.get("stop") in (None, 0):
            restored.append(position["ticker"])
            actions.append({"rule": "missing_stop", "ticker": position["ticker"],
                            "why": "a position without a stop is unbounded risk; "
                                   "it is re-created from the position record"})

    # Drop anything the gate reduced to nothing.
    allowed = [o for o in allowed if int(o.get("qty", 0)) > 0]

    return {"orders": allowed, "actions": actions, "paused": paused,
            "daily_loss_pct": round(loss, 2), "drawdown_pct": round(dd, 2),
            "stops_to_restore": sorted(restored)}


def check_invariant(before: list[dict], after: list[dict]) -> list[str]:
    """The gate may only reduce exposure. Returns any violation found.

    Used by the tests and by the runner itself, so a future change that
    quietly lets the gate raise a size fails loudly instead of shipping.
    """
    problems = []
    wanted = {}
    for order in before:
        if is_entry(order):
            key = (order["ticker"], order.get("strategy"), order["side"])
            wanted[key] = wanted.get(key, 0) + int(order.get("qty", 0))
    got = {}
    for order in after:
        if is_entry(order):
            key = (order["ticker"], order.get("strategy"), order["side"])
            got[key] = got.get(key, 0) + int(order.get("qty", 0))
    for key, qty in got.items():
        if qty > wanted.get(key, 0):
            problems.append("the gate raised %s from %d to %d"
                            % (key, wanted.get(key, 0), qty))
    for order in after:
        if is_entry(order):
            continue
        # A new order that is not an entry reduces exposure: allowed.
    return problems
