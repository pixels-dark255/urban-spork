"""
Intraday paper-trading engine.

Simulated money only - no broker connected, no real orders. This exists to
find out, honestly, whether a simple rule-based intraday strategy has any
real edge before ever risking actual capital, and to compare timeframes
(5m/15m/30m) against each other on identical rules.

Why a different signal set than the daily predictor: the ensemble in
predictor.py is built around multi-day drift and a GBM projection - it's
answering "where roughly will this trade over the next N days," not "should
I buy in the next 5 minutes." Intraday decisions need signals that actually
mean something at that timescale: short-vs-long moving average crossover,
RSI, price vs the day's volume-weighted average price (VWAP), and opening-
range breakout. All four are standard, well-understood technical signals -
nothing exotic, nothing that claims to have discovered a hidden edge.

Mechanics, deliberately simple for this first version:
- One open position per (symbol, timeframe) at a time.
- Entering costs 20% of current cash (so one stock can't wipe the whole
  book, and a few positions can run concurrently across a stock list).
- A hard stop-loss and target are checked on every tick BEFORE the signal
  score, so a losing trade is capped regardless of what the score says next -
  this is what actually enforces "profits should outweigh losses" rather
  than hoping the score-based exit gets there in time.
"""
import datetime as dt
import pandas as pd

from indicators import sma, rsi as rsi_series

TIMEFRAMES = ["5m", "15m", "30m"]
STARTING_CAPITAL = 100_000.0
POSITION_SIZE_FRACTION = 0.20   # of current cash, per new trade
STOP_LOSS_PCT = -0.015           # -1.5%
TARGET_PCT = 0.025                # +2.5% (target > stop, by design - the
                                   # asymmetry is what should make profits
                                   # outweigh losses over many trades, not a
                                   # guarantee any single trade works out)


def default_portfolio() -> dict:
    return {
        "cash": STARTING_CAPITAL,
        "position": None,  # {"qty", "entry_price", "entry_at", "timeframe"}
        "trade_log": [],
        "created_at": dt.datetime.utcnow().isoformat(),
    }


def _today_bars(bars: pd.DataFrame) -> pd.DataFrame:
    """Filter to just today's session so VWAP/opening-range are computed
    over the actual trading day, not smeared across the multi-day history
    Yahoo returns."""
    if bars.empty:
        return bars
    today = pd.Timestamp.now("UTC").tz_localize(None).date()
    idx_dates = bars.index.tz_localize(None).date if bars.index.tz is not None else bars.index.date
    return bars[idx_dates == today]


def compute_signal(bars: pd.DataFrame) -> dict | None:
    """Returns the current score (-4..+4, higher = more bullish) and the raw
    values behind it, or None if there isn't enough data yet (e.g. right at
    market open)."""
    if bars.empty or len(bars) < 20 or "Close" not in bars.columns:
        return None

    close = bars["Close"].dropna()
    if len(close) < 20:
        return None

    last_close = float(close.iloc[-1])
    fast_ma = float(sma(close, 5).iloc[-1])
    slow_ma = float(sma(close, 20).iloc[-1])
    rsi_val = float(rsi_series(close, 14).iloc[-1])

    today = _today_bars(bars)
    if len(today) >= 3 and "Volume" in today.columns and today["Volume"].sum() > 0:
        vwap = float((today["Close"] * today["Volume"]).sum() / today["Volume"].sum())
        orb_high = float(today["High"].iloc[:3].max())
        orb_low = float(today["Low"].iloc[:3].min())
    else:
        vwap, orb_high, orb_low = last_close, last_close, last_close

    score = 0
    score += 1 if fast_ma > slow_ma else -1
    score += 1 if rsi_val < 30 else (-1 if rsi_val > 70 else 0)
    score += 1 if last_close > vwap else -1
    score += 1 if last_close > orb_high else (-1 if last_close < orb_low else 0)

    return {
        "last_close": round(last_close, 2),
        "fast_ma": round(fast_ma, 2),
        "slow_ma": round(slow_ma, 2),
        "rsi": round(rsi_val, 1),
        "vwap": round(vwap, 2),
        "orb_high": round(orb_high, 2),
        "orb_low": round(orb_low, 2),
        "score": score,
    }


def step(portfolio: dict, signal: dict, timeframe: str) -> dict:
    """Advance one portfolio by one tick given the latest signal. Mutates
    and returns the portfolio; appends a closed-trade record to trade_log
    if a sell happens this tick."""
    price = signal["last_close"]
    pos = portfolio.get("position")

    if pos:
        change_pct = (price - pos["entry_price"]) / pos["entry_price"]
        should_exit = (
            change_pct <= STOP_LOSS_PCT
            or change_pct >= TARGET_PCT
            or signal["score"] <= -2
        )
        if should_exit:
            proceeds = pos["qty"] * price
            pnl = proceeds - (pos["qty"] * pos["entry_price"])
            portfolio["cash"] += proceeds
            portfolio["trade_log"].append({
                "timeframe": timeframe,
                "entry_at": pos["entry_at"],
                "entry_price": pos["entry_price"],
                "exit_at": dt.datetime.utcnow().isoformat(),
                "exit_price": price,
                "qty": pos["qty"],
                "pnl": round(pnl, 2),
                "pnl_pct": round(change_pct * 100, 3),
                "exit_reason": "stop_loss" if change_pct <= STOP_LOSS_PCT
                    else "target" if change_pct >= TARGET_PCT else "signal_reversal",
            })
            portfolio["trade_log"] = portfolio["trade_log"][-200:]
            portfolio["position"] = None
    else:
        if signal["score"] >= 2:
            spend = portfolio["cash"] * POSITION_SIZE_FRACTION
            qty = int(spend // price)
            if qty > 0:
                portfolio["cash"] -= qty * price
                portfolio["position"] = {
                    "qty": qty,
                    "entry_price": price,
                    "entry_at": dt.datetime.utcnow().isoformat(),
                    "timeframe": timeframe,
                }

    return portfolio


def portfolio_summary(portfolio: dict, last_price: float | None) -> dict:
    pos = portfolio.get("position")
    open_value = pos["qty"] * last_price if (pos and last_price) else 0.0
    equity = portfolio["cash"] + open_value
    closed = portfolio.get("trade_log", [])
    wins = [t for t in closed if t["pnl"] > 0]
    total_pnl = sum(t["pnl"] for t in closed) + (
        (last_price - pos["entry_price"]) * pos["qty"] if (pos and last_price) else 0
    )
    return {
        "cash": round(portfolio["cash"], 2),
        "equity": round(equity, 2),
        "total_pnl": round(total_pnl, 2),
        "total_pnl_pct": round(total_pnl / STARTING_CAPITAL * 100, 2),
        "open_position": pos,
        "closed_trades": len(closed),
        "win_rate_pct": round(100 * len(wins) / len(closed), 1) if closed else None,
    }
