"""
Risk management engine.

Every recommendation the platform emits passes through here, and this module
has the authority to veto it. That ordering is deliberate: a signal that
can't be given a sane stop, a target worth the risk, and a position size the
account can survive is not a trade, however confident the model feels.

What it produces for a direction + entry price:
  stop loss      - ATR-based, widened in high volatility, and pulled to just
                   beyond the nearest structural level when one sits closer
                   than the ATR stop would (price respects levels, not
                   arithmetic).
  target         - the further of (a) the minimum acceptable R:R and (b) the
                   next structural level, capped so it isn't a fantasy
                   multiple of ATR that the timeframe has no time to reach.
  position size  - allowed risk in rupees / risk per share, then capped by
                   available capital.
  risk / reward  - in rupees, and their ratio.

Rejections are first-class results, not exceptions: `ok: False` plus a
`reason` the UI shows verbatim.
"""
from __future__ import annotations

import datetime as dt

import config
import market_store

DEFAULT_SETTINGS = {
    "capital": config.DEFAULT_CAPITAL,
    "risk_per_trade_pct": config.DEFAULT_RISK_PER_TRADE_PCT,
    "max_daily_loss_pct": config.DEFAULT_MAX_DAILY_LOSS_PCT,
    "min_risk_reward": config.DEFAULT_MIN_RISK_REWARD,
    "min_confidence": config.MIN_TRADE_CONFIDENCE,
    "max_open_positions": 5,
}

# ATR multiples per timeframe. Shorter timeframes need proportionally wider
# ATR multiples because their ATR is tiny and market noise would otherwise
# stop every trade out within seconds.
ATR_STOP_MULTIPLIER = {
    "30s": 2.5, "1m": 2.2, "2m": 2.0, "5m": 1.8, "10m": 1.7,
    "15m": 1.6, "30m": 1.5, "1h": 1.5, "1.5h": 1.4, "2h": 1.4,
}

# A stop tighter than this fraction of price is inside the spread+noise for
# Indian equities and will be hit at random.
MIN_STOP_PCT = 0.0025   # 0.25%
MAX_STOP_PCT = 0.05     # 5% - beyond this, intraday sizing stops making sense

# How much of the trip to target must lie beyond a level before that level
# counts as blocking the trade. Below this the level is effectively at the
# entry - price is already testing it - and breaking it is the setup itself.
BLOCKING_LEVEL_SHARE = 0.35


def get_settings(client_id: str = "default") -> dict:
    stored = market_store.get_settings(client_id) or {}
    settings = {**DEFAULT_SETTINGS, **stored}
    # Guard against a saved value that would make the engine dangerous.
    settings["capital"] = max(1000.0, float(settings["capital"]))
    settings["risk_per_trade_pct"] = min(10.0, max(0.05, float(settings["risk_per_trade_pct"])))
    settings["max_daily_loss_pct"] = min(50.0, max(0.1, float(settings["max_daily_loss_pct"])))
    settings["min_risk_reward"] = min(10.0, max(0.5, float(settings["min_risk_reward"])))
    settings["min_confidence"] = min(0.95, max(0.3, float(settings["min_confidence"])))
    settings["max_open_positions"] = int(max(1, min(50, settings.get("max_open_positions", 5))))
    return settings


def save_settings(client_id: str, patch: dict) -> dict:
    current = get_settings(client_id)
    allowed = set(DEFAULT_SETTINGS)
    for key, value in (patch or {}).items():
        if key in allowed and value is not None:
            current[key] = value
    market_store.save_settings(client_id, current)
    return get_settings(client_id)


def daily_loss_status(client_id: str = "default") -> dict:
    """How much of today's loss budget is already spent, from closed paper
    trades. Live recommendations are blocked once the budget is gone - the
    single most useful rule in intraday trading and the easiest to skip."""
    settings = get_settings(client_id)
    today = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).date().isoformat()
    trades = market_store.list_trades(client_id, status="CLOSED", limit=500)
    realised = sum(
        (t.get("pnl") or 0.0) for t in trades
        if (t.get("closed_at") or "").startswith(today)
    )
    budget = settings["capital"] * settings["max_daily_loss_pct"] / 100.0
    return {
        "date": today,
        "realised_pnl": round(realised, 2),
        "loss_budget": round(budget, 2),
        "budget_used_pct": round(min(100.0, max(0.0, -realised) / budget * 100), 2) if budget else 0.0,
        "breached": bool(realised < 0 and abs(realised) >= budget),
    }


def _structural_stop(direction: int, entry: float, levels: dict | None) -> float | None:
    """A stop just beyond the level price would have to break for the idea to
    be wrong. 0.15% of padding keeps it clear of the exact level, where stop
    hunts cluster."""
    if not levels:
        return None
    pad = entry * 0.0015
    if direction > 0:
        support = levels.get("nearest_support")
        if support and support < entry:
            return float(support) - pad
    else:
        resistance = levels.get("nearest_resistance")
        if resistance and resistance > entry:
            return float(resistance) + pad
    return None


def build_trade_plan(direction: int, entry_price: float, atr_value: float,
                     timeframe: str, levels: dict | None = None,
                     regime_flags: list[str] | None = None,
                     client_id: str = "default", settings: dict | None = None) -> dict:
    """Turn a direction + price into a fully-specified, risk-checked trade."""
    settings = settings or get_settings(client_id)
    regime_flags = regime_flags or []

    if direction == 0:
        return {"ok": False, "reason": "No directional signal."}
    if not entry_price or entry_price <= 0:
        return {"ok": False, "reason": "No usable entry price."}

    atr_value = float(atr_value or 0.0)
    if atr_value <= 0:
        atr_value = entry_price * 0.005  # fall back to 0.5% when ATR is unusable

    multiplier = ATR_STOP_MULTIPLIER.get(timeframe, 1.6)
    if "HIGH_VOLATILITY" in regime_flags:
        multiplier *= 1.25   # wider stop, and the smaller size that implies
    stop_distance = atr_value * multiplier

    # Clamp to a band where the stop is neither noise-bait nor absurd.
    stop_distance = max(stop_distance, entry_price * MIN_STOP_PCT)
    stop_distance = min(stop_distance, entry_price * MAX_STOP_PCT)

    atr_stop = entry_price - stop_distance if direction > 0 else entry_price + stop_distance
    struct_stop = _structural_stop(direction, entry_price, levels)
    stop_loss = atr_stop
    stop_basis = "atr"
    if struct_stop is not None:
        # Use the structural stop only when it's tighter than the ATR stop but
        # still outside the noise floor - otherwise ATR wins.
        struct_distance = abs(entry_price - struct_stop)
        if entry_price * MIN_STOP_PCT <= struct_distance < stop_distance:
            stop_loss = struct_stop
            stop_basis = "support_resistance"

    risk_per_share = abs(entry_price - stop_loss)
    if risk_per_share <= 0:
        return {"ok": False, "reason": "Stop loss resolved to the entry price - no risk definable."}

    min_rr = float(settings["min_risk_reward"])
    rr_target = risk_per_share * min_rr
    target_price = entry_price + rr_target if direction > 0 else entry_price - rr_target

    # If a structural level sits beyond the R:R target and within reach,
    # extend to just short of it: that's where the move actually has a
    # reason to stop, and stopping short of the level is how you get filled.
    level_target = None
    blocking_level = None
    if levels:
        level = levels.get("nearest_resistance") if direction > 0 else levels.get("nearest_support")
        # Only a real swing pivot counts. The fallback level (the window's
        # extreme, used when price is at a fresh high or low) is not
        # structure, and treating it as one would veto every breakout.
        from_pivot = levels.get("resistance_is_pivot" if direction > 0 else "support_is_pivot", True)
        if level is not None and from_pivot:
            level = float(level)
            pad = entry_price * 0.001
            candidate = level - pad if direction > 0 else level + pad
            reward_to_level = (candidate - entry_price) if direction > 0 else (entry_price - candidate)
            if reward_to_level > 0:
                level_target = candidate
                if reward_to_level > rr_target:
                    # The level is further away than the minimum R:R needs, so
                    # aim for it - that's where the move has a reason to stop.
                    target_price = candidate
                elif reward_to_level >= BLOCKING_LEVEL_SHARE * rr_target:
                    # A meaningful share of the move has to happen beyond this
                    # level, so the trade only pays by breaking through it.
                    # That is a materially worse setup than the arithmetic
                    # suggests, and is rejected below rather than quietly
                    # priced as if the level weren't there.
                    blocking_level = level
                # A level nearer than that is not an obstacle: price is
                # already trading at it, and clearing it IS the entry.

    reward_per_share = abs(target_price - entry_price)
    risk_reward = reward_per_share / risk_per_share if risk_per_share else 0.0

    capital = float(settings["capital"])
    allowed_risk = capital * float(settings["risk_per_trade_pct"]) / 100.0
    quantity = int(allowed_risk // risk_per_share)
    max_affordable = int(capital // entry_price)
    capped_by_capital = quantity > max_affordable
    quantity = min(quantity, max_affordable)

    plan = {
        "ok": True,
        "direction": direction,
        "side": "BUY" if direction > 0 else "SELL",
        "entry_price": round(entry_price, 2),
        "stop_loss": round(stop_loss, 2),
        "target_price": round(target_price, 2),
        "stop_basis": stop_basis,
        "stop_distance": round(risk_per_share, 2),
        "stop_pct": round(risk_per_share / entry_price * 100, 3),
        "target_distance": round(reward_per_share, 2),
        "target_pct": round(reward_per_share / entry_price * 100, 3),
        "position_size": quantity,
        "risk_amount": round(quantity * risk_per_share, 2),
        "reward_amount": round(quantity * reward_per_share, 2),
        "risk_reward": round(risk_reward, 2),
        "risk_reward_label": f"1:{round(risk_reward, 2)}",
        "capital_deployed": round(quantity * entry_price, 2),
        "atr_used": round(atr_value, 3),
        "atr_multiplier": round(multiplier, 2),
        "allowed_risk": round(allowed_risk, 2),
        "capped_by_capital": capped_by_capital,
        "level_target": round(level_target, 2) if level_target else None,
    }

    if blocking_level is not None:
        plan["ok"] = False
        plan["blocking_level"] = round(blocking_level, 2)
        plan["reason"] = (
            f"{'Resistance' if direction > 0 else 'Support'} at {blocking_level:,.2f} sits between the "
            f"entry and the minimum {min_rr:.2f}R target - the trade only pays if it breaks through it."
        )
        return plan
    if risk_reward < min_rr - 1e-9:
        plan["ok"] = False
        plan["reason"] = (
            f"Risk/reward {risk_reward:.2f} is below your minimum of {min_rr:.2f} - "
            f"the nearest level is too close for this trade to pay for its own risk."
        )
        return plan
    if quantity < 1:
        plan["ok"] = False
        plan["reason"] = (
            f"Position size rounds to zero: risking {allowed_risk:,.0f} at "
            f"{risk_per_share:,.2f} per share buys less than one share."
        )
        return plan
    return plan


def check_trade_allowed(client_id: str, confidence: float, settings: dict | None = None) -> dict:
    """Account-level gates that apply regardless of how good one signal looks."""
    settings = settings or get_settings(client_id)
    if confidence < settings["min_confidence"]:
        return {
            "allowed": False,
            "reason": (f"Confidence {confidence:.0%} is below your {settings['min_confidence']:.0%} "
                       f"threshold - the engine would rather sit out than guess."),
        }
    loss = daily_loss_status(client_id)
    if loss["breached"]:
        return {
            "allowed": False,
            "reason": (f"Daily loss limit hit ({loss['realised_pnl']:,.0f} of a "
                       f"{loss['loss_budget']:,.0f} budget). No further trades today."),
        }
    open_trades = market_store.list_trades(client_id, status="OPEN", limit=100)
    if len(open_trades) >= settings["max_open_positions"]:
        return {
            "allowed": False,
            "reason": f"Already at your limit of {settings['max_open_positions']} open paper positions.",
        }
    return {"allowed": True, "reason": None}
