"""
Intraday recommendation engine.

Takes a stock and a timeframe and returns a complete, risk-checked trade
idea - or an explicit refusal. The pipeline, in order:

    bars -> indicators -> regime -> components -> ensemble -> risk -> answer

Every recommendation carries entry, stop loss, target, position size, risk,
reward, R:R and confidence, plus the reasoning behind it. Nothing here is a
black box: the response includes each component's vote, its weight, and the
plain-English reasons it gave.

Refusals outnumber trades by design. The engine returns NO_TRADE when
confidence is under the user's threshold, when the risk engine can't build a
trade worth taking, when the daily loss limit is spent, or when there simply
isn't enough data. "No trade" is the correct answer to most market moments
and is treated here as a first-class result with its own reason, not as an
error.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

import config
import ensemble
import indicators
import market_data
import market_overview
import market_store
import regime as regime_mod
import risk
import universe
from ml import registry as ml_registry

ACTIONABLE = {"BUY", "STRONG_BUY", "SELL", "STRONG_SELL"}

MIN_BARS = 40   # below this nothing can be computed honestly


def _session_bars(bars: pd.DataFrame) -> pd.DataFrame:
    """Just the latest session, for VWAP and the opening range."""
    if bars is None or bars.empty:
        return bars
    try:
        dates = pd.Index([pd.Timestamp(i).date() for i in bars.index])
    except (TypeError, ValueError):
        return bars
    return bars[dates == dates[-1]]


def _cooldown_ok(client_id: str, symbol: str, timeframe: str, cooldown_minutes: float) -> bool:
    """Don't write a new prediction row every time the screen refreshes -
    one per timeframe bar is what's meaningful, and anything more inflates
    the history without adding information."""
    recent = market_store.list_predictions(client_id=client_id, symbol=symbol,
                                           timeframe=timeframe, limit=1)
    if not recent:
        return True
    try:
        made_at = dt.datetime.fromisoformat(recent[0]["made_at"])
    except (ValueError, TypeError):
        return True
    age_minutes = (dt.datetime.utcnow() - made_at).total_seconds() / 60.0
    return age_minutes >= cooldown_minutes


def analyze(symbol: str, exchange: str = "NSE", timeframe: str = "5m",
            client_id: str = "default", record: bool = True,
            market_context: dict | None = None) -> dict:
    tf = market_data.TIMEFRAMES.get(timeframe)
    if tf is None:
        return {"ok": False, "error": f"unknown timeframe '{timeframe}'",
                "supported": list(market_data.TIMEFRAMES)}

    symbol = symbol.upper()
    exchange = exchange.upper()
    settings = risk.get_settings(client_id)
    hold_bars = market_data.HOLD_BARS.get(timeframe, 10)
    horizon_minutes = tf.minutes * hold_bars
    name = universe.display_name(symbol, exchange)

    base = {
        "symbol": symbol,
        "exchange": exchange,
        "display_name": name,
        "timeframe": timeframe,
        "timeframe_detail": tf.as_dict(),
        "horizon_minutes": round(horizon_minutes, 2),
        "hold_bars": hold_bars,
        "generated_at": dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat(),
    }

    bars = market_data.get_bars(symbol, exchange, timeframe)
    if bars is None or len(bars) < MIN_BARS:
        have = 0 if bars is None else len(bars)
        return {
            **base, "ok": True, "recommendation": "NO_TRADE", "confidence": 0.0,
            "reason": (f"Only {have} {timeframe} bars available (need {MIN_BARS}). "
                       f"Either the data source is unreachable or this stock is too "
                       f"illiquid on this timeframe to analyse."),
            "data_available": False,
            "bars_analysed": have,
            "entry_price": None,
            "explanation": [],
            "chart": [],
        }

    snapshot = indicators.indicator_snapshot(bars, session_df=_session_bars(bars))
    if snapshot is None:
        return {**base, "ok": True, "recommendation": "NO_TRADE", "confidence": 0.0,
                "reason": "Indicators could not be computed from the available bars.",
                "data_available": False, "bars_analysed": int(len(bars)),
                "entry_price": None, "explanation": [], "chart": []}

    regime_info = regime_mod.detect(bars, snapshot)

    if market_context is None:
        try:
            market_context = market_overview.market_context()
        except Exception as e:
            market_context = {"available": False, "reason": str(e)}

    components = {
        "technical": ensemble.technical_component(snapshot, regime_info),
        "ml": ml_registry.predict(symbol, exchange, timeframe, bars),
        "statistical": ensemble.statistical_component(snapshot, bars),
        "volume": ensemble.volume_component(snapshot),
        "market_context": ensemble.market_context_component(market_context),
    }
    if components["ml"].get("available"):
        ml_comp = components["ml"]
        ml_comp.setdefault("reasons", [])
        ml_comp["reasons"].append(
            f"{ml_comp['model']} model: {ml_comp['p_up']:.0%} up / {ml_comp['p_down']:.0%} down "
            f"(validated at {ml_comp['val_directional_accuracy']:.0%} directional accuracy "
            f"out of sample)"
        )
        ml_comp["confidence"] = float(min(0.8, ml_comp.get("conviction", 0.5)))

    fused = ensemble.fuse(components, regime_info, timeframe, approximated=tf.approximated)
    recommendation = ensemble.classify(fused["score"], fused["confidence"],
                                       settings["min_confidence"])

    entry_price = market_data.get_quote(symbol, exchange) or snapshot["price"]

    result = {
        **base,
        "ok": True,
        "recommendation": recommendation,
        "confidence": fused["confidence"],
        "score": fused["score"],
        "agreement": fused["agreement"],
        "direction": fused["direction"],
        "entry_price": entry_price,
        "last_bar_at": market_data.last_bar_time(bars),
        "market_regime": regime_info["primary"],
        "regime": {**regime_info, "description": regime_mod.describe(regime_info)},
        "indicators": snapshot,
        "components": {k: _public_component(v) for k, v in components.items()},
        "weights": fused["weights"],
        "penalties": fused["penalties"],
        "performance_detail": fused["performance_detail"],
        "settings_used": {
            "capital": settings["capital"],
            "risk_per_trade_pct": settings["risk_per_trade_pct"],
            "min_risk_reward": settings["min_risk_reward"],
            "min_confidence": settings["min_confidence"],
        },
        "data_available": True,
        "bars_analysed": int(len(bars)),
        "chart": market_data.bars_to_points(bars, limit=180),
    }

    # --- risk layer: it can veto, and its veto is final -------------------
    trade_plan = None
    if recommendation in ACTIONABLE:
        gate = risk.check_trade_allowed(client_id, fused["confidence"], settings)
        if not gate["allowed"]:
            result["recommendation"] = "NO_TRADE"
            result["reason"] = gate["reason"]
            result["downgraded_from"] = recommendation
        else:
            trade_plan = risk.build_trade_plan(
                direction=fused["direction"],
                entry_price=entry_price,
                atr_value=snapshot["atr"],
                timeframe=timeframe,
                levels=snapshot.get("levels"),
                regime_flags=regime_info.get("flags"),
                client_id=client_id,
                settings=settings,
            )
            if not trade_plan.get("ok"):
                result["recommendation"] = "NO_TRADE"
                result["reason"] = trade_plan.get("reason")
                result["downgraded_from"] = recommendation
                result["rejected_plan"] = trade_plan
                trade_plan = None
    elif recommendation == "NO_TRADE":
        result["reason"] = (
            f"Confidence {fused['confidence']:.0%} is below the {settings['min_confidence']:.0%} "
            f"threshold - the signals disagree too much to justify a trade."
        )
    else:  # HOLD
        result["reason"] = ("Signals are close to balanced. Nothing here is worth "
                            "paying the spread for.")

    result["trade_plan"] = trade_plan
    result["explanation"] = build_explanation(result, components, regime_info, trade_plan)

    if record:
        cooldown = max(1.0, tf.minutes)
        should_record = result["recommendation"] in ACTIONABLE and _cooldown_ok(
            client_id, symbol, timeframe, cooldown
        )
        if should_record:
            try:
                result["prediction_id"] = _record(result, client_id, components, trade_plan)
            except Exception as e:
                print(f"[warn] could not record prediction for {symbol}: {e}")

    return result


def _public_component(comp: dict) -> dict:
    """Trim a component to what the UI needs, keeping the reasoning."""
    return {
        "available": bool(comp.get("available")),
        "score": round(float(comp.get("score", 0.0)), 4) if comp.get("available") else None,
        "confidence": round(float(comp.get("confidence", 0.0)), 4) if comp.get("available") else None,
        "reasons": comp.get("reasons") or [],
        "reason": comp.get("reason"),
        "detail": comp.get("detail"),
        "model": comp.get("model"),
        "val_directional_accuracy": comp.get("val_directional_accuracy"),
        "top_features": comp.get("top_features"),
    }


def build_explanation(result: dict, components: dict, regime_info: dict,
                      trade_plan: dict | None) -> list[str]:
    """The 'show your work' section - why this answer, in order of weight."""
    lines = [regime_mod.describe(regime_info)]

    ranked = sorted(
        ((name, comp) for name, comp in components.items() if comp.get("available")),
        key=lambda kv: -abs(kv[1].get("score", 0.0)),
    )
    for name, comp in ranked:
        reasons = comp.get("reasons") or []
        if not reasons:
            continue
        direction = "bullish" if comp["score"] > 0 else ("bearish" if comp["score"] < 0 else "neutral")
        weight = (result.get("weights") or {}).get(name, {}).get("weight")
        weight_txt = f", {weight:.0%} of the vote" if weight else ""
        # "net" matters: the evidence list below deliberately includes the
        # points that argued the other way, which is the whole reason this
        # is explainable rather than a single number.
        lines.append(f"{name.replace('_', ' ').title()} is net {direction}{weight_txt}. Evidence: " +
                     "; ".join(reasons[:3]) + ".")

    unavailable = [n for n, c in components.items() if not c.get("available")]
    if unavailable:
        detail = "; ".join(
            f"{n.replace('_', ' ')} ({components[n].get('reason', 'unavailable')})"
            for n in unavailable
        )
        lines.append(f"Components with no data this run: {detail}.")

    if result.get("penalties"):
        lines.append("Confidence was reduced for: " + ", ".join(result["penalties"]) + ".")

    if trade_plan:
        lines.append(
            f"Risk plan: stop at {trade_plan['stop_loss']:,.2f} "
            f"({trade_plan['stop_pct']:.2f}% away, set from {trade_plan['stop_basis'].replace('_', ' ')}), "
            f"target {trade_plan['target_price']:,.2f}, "
            f"{trade_plan['position_size']} shares risking "
            f"{trade_plan['risk_amount']:,.0f} to make {trade_plan['reward_amount']:,.0f} "
            f"at {trade_plan['risk_reward_label']}."
        )
    elif result.get("reason"):
        lines.append(result["reason"])

    return lines


def _record(result: dict, client_id: str, components: dict, trade_plan: dict | None) -> int:
    import json
    now = dt.datetime.utcnow()
    target_at = now + dt.timedelta(minutes=result["horizon_minutes"])
    return market_store.save_prediction({
        "client_id": client_id,
        "symbol": result["symbol"],
        "exchange": result["exchange"],
        "display_name": result["display_name"],
        "timeframe": result["timeframe"],
        "kind": "intraday",
        "made_at": now.isoformat(),
        "horizon_minutes": result["horizon_minutes"],
        "target_at": target_at.isoformat(),
        "recommendation": result["recommendation"],
        "direction": result["direction"],
        "entry_price": result["entry_price"],
        "stop_loss": (trade_plan or {}).get("stop_loss"),
        "target_price": (trade_plan or {}).get("target_price"),
        "position_size": (trade_plan or {}).get("position_size"),
        "risk_amount": (trade_plan or {}).get("risk_amount"),
        "reward_amount": (trade_plan or {}).get("reward_amount"),
        "risk_reward": (trade_plan or {}).get("risk_reward"),
        "confidence": result["confidence"],
        "market_regime": result["market_regime"],
        "indicators_json": json.dumps(result["indicators"], default=str),
        "components_json": json.dumps(
            {k: {"score": v.get("score"), "available": v.get("available")}
             for k, v in components.items()}, default=str),
    })


def scan(symbols: list[tuple[str, str]], timeframe: str = "5m",
         client_id: str = "default", record: bool = False) -> list[dict]:
    """Run the engine across several stocks - used by the watchlist screen.
    Market context is fetched once and shared rather than per stock."""
    try:
        context = market_overview.market_context()
    except Exception:
        context = {"available": False}
    out = []
    for symbol, exchange in symbols:
        try:
            result = analyze(symbol, exchange, timeframe, client_id=client_id,
                             record=record, market_context=context)
            out.append({
                "symbol": result["symbol"],
                "exchange": result["exchange"],
                "display_name": result.get("display_name"),
                "recommendation": result.get("recommendation"),
                "confidence": result.get("confidence"),
                "score": result.get("score"),
                "entry_price": result.get("entry_price"),
                "market_regime": result.get("market_regime"),
                "trade_plan": result.get("trade_plan"),
                "reason": result.get("reason"),
                "indicators": {
                    k: (result.get("indicators") or {}).get(k)
                    for k in ("price", "rsi", "adx", "atr_pct", "vwap", "ema9", "ema21")
                } if result.get("indicators") else None,
            })
        except Exception as e:
            out.append({"symbol": symbol, "exchange": exchange, "error": str(e)})
    return out
