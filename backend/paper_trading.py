"""
Paper trading - simulated money, no broker, no orders.

This exists so the platform's recommendations can be judged on outcomes
rather than on how convincing they read. A recommendation becomes a paper
trade with a fixed entry, stop and target; the scheduler marks it to market
every tick and closes it the moment either level trades. What comes out the
other end - win rate, average win vs average loss, expectancy - is the only
honest answer to "does this thing work".

Deliberate modelling choices, so the numbers aren't quietly flattering:
 - a stop or target that is touched *within* a bar counts as hit, using the
   bar's high/low, not just its close. Assuming you'd have survived a spike
   that went through your stop is how backtests lie.
 - when both the stop and the target fall inside the same bar, the stop is
   assumed to have hit first. Without tick data you cannot know the order,
   and the pessimistic assumption is the only defensible one.
 - no slippage or brokerage is modelled yet; real fills would be slightly
   worse. Treat reported P&L as an upper bound.
"""
from __future__ import annotations

import datetime as dt

import market_data
import market_store
import risk


def open_from_plan(client_id: str, analysis: dict) -> dict:
    """Open a paper trade from an intraday_engine result."""
    plan = analysis.get("trade_plan")
    if not plan or not plan.get("ok"):
        return {"ok": False, "reason": analysis.get("reason") or "No tradeable plan in this analysis."}
    gate = risk.check_trade_allowed(client_id, analysis.get("confidence", 0.0))
    if not gate["allowed"]:
        return {"ok": False, "reason": gate["reason"]}
    if plan["position_size"] < 1:
        return {"ok": False, "reason": "Position size is zero."}

    # Link the trade to the prediction that produced it. The analysis only
    # carries an id when it wrote a new prediction row this run; inside the
    # per-bar cooldown it reuses the existing one, and that's the row this
    # trade should point at - otherwise the Predictions tab can't show which
    # calls were actually taken.
    prediction_id = analysis.get("prediction_id")
    if prediction_id is None:
        recent = market_store.list_predictions(
            client_id=client_id, symbol=analysis["symbol"],
            timeframe=analysis["timeframe"], limit=1,
        )
        if recent and recent[0]["recommendation"] == analysis.get("recommendation"):
            prediction_id = recent[0]["id"]

    trade_id = market_store.open_trade({
        "client_id": client_id,
        "prediction_id": prediction_id,
        "symbol": analysis["symbol"],
        "exchange": analysis["exchange"],
        "display_name": analysis.get("display_name"),
        "timeframe": analysis["timeframe"],
        "side": plan["side"],
        "quantity": plan["position_size"],
        "entry_price": plan["entry_price"],
        "stop_loss": plan["stop_loss"],
        "target_price": plan["target_price"],
        "confidence": analysis.get("confidence"),
        "market_regime": analysis.get("market_regime"),
        "last_price": plan["entry_price"],
    })
    return {"ok": True, "trade_id": trade_id, "trade": market_store.get_trade(trade_id)}


def _pnl(trade: dict, exit_price: float) -> tuple[float, float]:
    qty, entry = trade["quantity"], trade["entry_price"]
    direction = 1 if trade["side"] == "BUY" else -1
    pnl = (exit_price - entry) * qty * direction
    pnl_pct = (exit_price - entry) / entry * 100 * direction if entry else 0.0
    return round(pnl, 2), round(pnl_pct, 3)


def close_trade(trade_id: int, exit_price: float, reason: str = "manual") -> dict:
    trade = market_store.get_trade(trade_id)
    if not trade:
        return {"ok": False, "reason": "trade not found"}
    if trade["status"] != "OPEN":
        return {"ok": False, "reason": "trade already closed"}
    pnl, pnl_pct = _pnl(trade, exit_price)
    market_store.close_trade(trade_id, exit_price, reason, pnl, pnl_pct)
    return {"ok": True, "trade": market_store.get_trade(trade_id)}


def _resolve_against_bars(trade: dict) -> tuple[float, str] | None:
    """Walk the bars printed since entry and decide whether the stop or the
    target was reached, using each bar's full high/low range."""
    try:
        opened_at = dt.datetime.fromisoformat(trade["opened_at"])
    except (TypeError, ValueError):
        return None
    opened_ts = int(opened_at.replace(tzinfo=dt.timezone.utc).timestamp())

    bars = market_data.get_bars(trade["symbol"], trade["exchange"], trade["timeframe"])
    if bars is None or bars.empty:
        return None

    long_side = trade["side"] == "BUY"
    stop, target = trade.get("stop_loss"), trade.get("target_price")
    for idx, row in bars.iterrows():
        ts = idx.timestamp() if hasattr(idx, "timestamp") else None
        if ts is None or ts <= opened_ts:
            continue
        high, low = float(row["High"]), float(row["Low"])
        hit_stop = stop is not None and ((low <= stop) if long_side else (high >= stop))
        hit_target = target is not None and ((high >= target) if long_side else (low <= target))
        if hit_stop:
            return float(stop), "stop"       # pessimistic when both hit in one bar
        if hit_target:
            return float(target), "target"
    return None


def mark_to_market(client_id: str | None = None) -> dict:
    """Update open trades, closing any whose stop or target has traded.
    Called every scheduler tick and whenever the Paper tab is opened."""
    trades = market_store.open_trades_all()
    if client_id:
        trades = [t for t in trades if t["client_id"] == client_id]
    closed, updated = [], 0
    for trade in trades:
        try:
            hit = _resolve_against_bars(trade)
            if hit:
                exit_price, reason = hit
                result = close_trade(trade["id"], exit_price, reason)
                if result.get("ok"):
                    closed.append(result["trade"])
                continue
            price = market_data.get_quote(trade["symbol"], trade["exchange"])
            if price:
                market_store.update_trade_price(trade["id"], price)
                updated += 1
        except Exception as e:
            print(f"[warn] mark-to-market failed for trade {trade['id']}: {e}")
    return {"updated": updated, "closed": len(closed), "closed_trades": closed}


def open_positions(client_id: str) -> list[dict]:
    out = []
    for trade in market_store.list_trades(client_id, status="OPEN"):
        last = trade.get("last_price") or trade["entry_price"]
        pnl, pnl_pct = _pnl(trade, last)
        trade["unrealised_pnl"] = pnl
        trade["unrealised_pnl_pct"] = pnl_pct
        out.append(trade)
    return out


def summary(client_id: str) -> dict:
    """The scorecard. Every number here is computed from closed trades only -
    counting open trades at their current mark is how a losing book gets to
    report a win rate."""
    closed = market_store.list_trades(client_id, status="CLOSED", limit=1000)
    opens = open_positions(client_id)
    settings = risk.get_settings(client_id)

    wins = [t for t in closed if (t.get("pnl") or 0) > 0]
    losses = [t for t in closed if (t.get("pnl") or 0) < 0]
    realised = sum((t.get("pnl") or 0) for t in closed)
    unrealised = sum(t["unrealised_pnl"] for t in opens)

    avg_win = (sum(t["pnl"] for t in wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(t["pnl"] for t in losses) / len(losses)) if losses else 0.0
    win_rate = (len(wins) / len(closed)) if closed else None
    # Expectancy per trade: the number that actually decides whether a
    # strategy makes money. A 40% win rate with 3:1 winners beats a 70% win
    # rate with 1:3 winners, and only this shows that.
    expectancy = ((win_rate * avg_win) + ((1 - win_rate) * avg_loss)) if win_rate is not None else None

    by_timeframe = {}
    for trade in closed:
        bucket = by_timeframe.setdefault(trade["timeframe"], {"trades": 0, "wins": 0, "pnl": 0.0})
        bucket["trades"] += 1
        bucket["wins"] += 1 if (trade.get("pnl") or 0) > 0 else 0
        bucket["pnl"] += trade.get("pnl") or 0
    for bucket in by_timeframe.values():
        bucket["win_rate_pct"] = round(100 * bucket["wins"] / bucket["trades"], 1)
        bucket["pnl"] = round(bucket["pnl"], 2)

    exits = {}
    for trade in closed:
        exits[trade.get("exit_reason") or "unknown"] = exits.get(trade.get("exit_reason") or "unknown", 0) + 1

    return {
        "starting_capital": settings["capital"],
        "closed_trades": len(closed),
        "open_trades": len(opens),
        "realised_pnl": round(realised, 2),
        "unrealised_pnl": round(unrealised, 2),
        "total_pnl": round(realised + unrealised, 2),
        "return_pct": round((realised + unrealised) / settings["capital"] * 100, 3),
        "win_rate_pct": round(win_rate * 100, 1) if win_rate is not None else None,
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "profit_factor": (round(sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in losses)), 2)
                          if losses and sum(t["pnl"] for t in losses) else None),
        "expectancy_per_trade": round(expectancy, 2) if expectancy is not None else None,
        "by_timeframe": by_timeframe,
        "exit_reasons": exits,
        "daily_loss_status": risk.daily_loss_status(client_id),
        "note": ("Simulated money only. No slippage or brokerage is modelled, "
                 "so real-world results would be somewhat worse."),
    }
