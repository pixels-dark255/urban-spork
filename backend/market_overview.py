"""
Market overview: what the wider Indian market is doing, and the context
signal that feeds every single-stock prediction.

Two jobs:
 1. Populate the Market tab - NIFTY / SENSEX / BANK NIFTY trends, India VIX,
    breadth (advancing vs declining), the day's biggest movers, and sector
    performance.
 2. Produce `market_context()` - one number in [-1, +1] with reasons, which
    the ensemble treats as a component. A long setup in a stock means less
    when the index is breaking down, and this is how the engine knows.

Everything is cached (default 10 minutes) and the last good snapshot is
persisted, so the Market tab still renders outside market hours and during a
data-source outage instead of showing an error.
"""
from __future__ import annotations

import time
import threading
import datetime as dt

import numpy as np

import market_data
import market_store
import universe

INDICES = {
    "NIFTY 50": "^NSEI",
    "SENSEX": "^BSESN",
    "BANK NIFTY": "^NSEBANK",
    "INDIA VIX": "^INDIAVIX",
}

# A representative large-cap sample for breadth and sector performance.
# Deliberately not the whole universe: 2,000 daily fetches per refresh would
# be slow, rate-limited and pointless - breadth converges long before that.
BREADTH_SAMPLE = [
    "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "HINDUNILVR", "ITC", "SBIN",
    "BHARTIARTL", "KOTAKBANK", "LT", "AXISBANK", "BAJFINANCE", "ASIANPAINT", "MARUTI",
    "HCLTECH", "SUNPHARMA", "TITAN", "ULTRACEMCO", "WIPRO", "NTPC", "POWERGRID",
    "TATAMOTORS", "M&M", "TATASTEEL", "JSWSTEEL", "COALINDIA", "ONGC", "ADANIENT",
    "ADANIPORTS", "CIPLA", "DRREDDY", "TECHM", "GRASIM", "NESTLEIND", "BRITANNIA",
]

CACHE_TTL_SECONDS = 600
SNAPSHOT_KEY = "market_overview_snapshot"

_lock = threading.Lock()
_cache: dict = {"data": None, "ts": 0.0}


def _index_snapshot(name: str, symbol: str) -> dict:
    daily = market_data.get_daily_bars(symbol, "NSE", period="6mo")
    if daily is None or daily.empty or len(daily) < 2:
        return {"name": name, "symbol": symbol, "available": False}
    close = daily["Close"].dropna()
    last = float(close.iloc[-1])
    prev = float(close.iloc[-2])
    change_pct = (last - prev) / prev * 100 if prev else 0.0

    ema20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
    ema50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1]) if len(close) >= 50 else ema20
    if last > ema20 > ema50:
        trend = "UPTREND"
    elif last < ema20 < ema50:
        trend = "DOWNTREND"
    else:
        trend = "SIDEWAYS"

    return {
        "name": name,
        "symbol": symbol,
        "available": True,
        "price": round(last, 2),
        "change_pct": round(change_pct, 2),
        "trend": trend,
        "ema20": round(ema20, 2),
        "ema50": round(ema50, 2),
        "change_5d_pct": round(float(close.pct_change(5).iloc[-1] * 100), 2) if len(close) > 5 else None,
        "change_20d_pct": round(float(close.pct_change(20).iloc[-1] * 100), 2) if len(close) > 20 else None,
    }


def _movers_and_sectors(sample: list[str]) -> dict:
    movers, sector_returns = [], {}
    for symbol in sample:
        try:
            daily = market_data.get_daily_bars(symbol, "NSE", period="1mo")
            if daily is None or len(daily) < 2:
                continue
            close = daily["Close"].dropna()
            last, prev = float(close.iloc[-1]), float(close.iloc[-2])
            if not prev:
                continue
            change = (last - prev) / prev * 100
            info = universe.resolve(symbol, "NSE") or {}
            movers.append({
                "symbol": symbol,
                "name": info.get("name") or symbol,
                "sector": info.get("sector"),
                "price": round(last, 2),
                "change_pct": round(change, 2),
            })
            sector = info.get("sector")
            if sector:
                sector_returns.setdefault(sector, []).append(change)
        except Exception as e:
            print(f"[warn] overview fetch failed for {symbol}: {e}")

    movers.sort(key=lambda m: m["change_pct"], reverse=True)
    advancing = sum(1 for m in movers if m["change_pct"] > 0)
    declining = sum(1 for m in movers if m["change_pct"] < 0)
    sectors = [
        {"sector": s, "avg_change_pct": round(float(np.mean(v)), 2), "stocks": len(v)}
        for s, v in sector_returns.items()
    ]
    sectors.sort(key=lambda s: s["avg_change_pct"], reverse=True)
    return {
        "gainers": movers[:8],
        "losers": list(reversed(movers[-8:])) if movers else [],
        "advancing": advancing,
        "declining": declining,
        "unchanged": len(movers) - advancing - declining,
        "breadth_ratio": round(advancing / max(1, advancing + declining), 3),
        "sectors": sectors,
        "sample_size": len(movers),
    }


def _sentiment(indices: list[dict], breadth: dict) -> dict:
    """One sentiment read from index direction + breadth + volatility."""
    reasons = []
    scores = []
    for idx in indices:
        if not idx.get("available") or idx["name"] == "INDIA VIX":
            continue
        change = idx["change_pct"]
        trend_score = {"UPTREND": 0.5, "SIDEWAYS": 0.0, "DOWNTREND": -0.5}[idx["trend"]]
        scores.append(float(np.clip(change / 1.5, -1, 1)) * 0.6 + trend_score * 0.4)
        reasons.append(f"{idx['name']} {change:+.2f}% and in a {idx['trend'].lower()}")

    index_score = float(np.mean(scores)) if scores else 0.0

    breadth_score = 0.0
    if breadth.get("sample_size"):
        breadth_score = (breadth["breadth_ratio"] - 0.5) * 2.0
        reasons.append(
            f"Breadth {breadth['advancing']} advancing vs {breadth['declining']} declining"
        )

    vix = next((i for i in indices if i["name"] == "INDIA VIX" and i.get("available")), None)
    volatility_note = None
    if vix:
        if vix["price"] >= 20:
            volatility_note = f"India VIX at {vix['price']:.1f} - a nervous, whippy market"
        elif vix["price"] <= 12:
            volatility_note = f"India VIX at {vix['price']:.1f} - a calm market"
        if volatility_note:
            reasons.append(volatility_note)

    score = float(np.clip(0.65 * index_score + 0.35 * breadth_score, -1, 1))
    if score >= 0.4:
        label = "BULLISH"
    elif score >= 0.12:
        label = "MILDLY_BULLISH"
    elif score <= -0.4:
        label = "BEARISH"
    elif score <= -0.12:
        label = "MILDLY_BEARISH"
    else:
        label = "NEUTRAL"

    return {
        "label": label,
        "score": round(score, 4),
        "reasons": reasons,
        "index_score": round(index_score, 4),
        "breadth_score": round(breadth_score, 4),
        "volatility_note": volatility_note,
    }


def build_overview(force: bool = False) -> dict:
    with _lock:
        if not force and _cache["data"] and (time.time() - _cache["ts"]) < CACHE_TTL_SECONDS:
            return _cache["data"]

    indices = [_index_snapshot(name, sym) for name, sym in INDICES.items()]
    # If not a single index came back, the provider is unreachable. Walking
    # the whole breadth sample anyway would mean dozens more doomed requests
    # and a screen that hangs for a minute before admitting it has nothing.
    if any(i.get("available") for i in indices):
        breadth = _movers_and_sectors(BREADTH_SAMPLE)
    else:
        breadth = {"gainers": [], "losers": [], "advancing": 0, "declining": 0,
                   "unchanged": 0, "breadth_ratio": 0.0, "sectors": [], "sample_size": 0}
    sentiment = _sentiment(indices, breadth)

    overview = {
        "generated_at": dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat(),
        "indices": indices,
        "sentiment": sentiment,
        "breadth": {
            "advancing": breadth["advancing"],
            "declining": breadth["declining"],
            "unchanged": breadth["unchanged"],
            "ratio": breadth["breadth_ratio"],
            "sample_size": breadth["sample_size"],
        },
        "gainers": breadth["gainers"],
        "losers": breadth["losers"],
        "sectors": breadth["sectors"],
        "live": any(i.get("available") for i in indices),
    }

    if overview["live"]:
        with _lock:
            _cache["data"] = overview
            _cache["ts"] = time.time()
        try:
            market_store.kv_set(SNAPSHOT_KEY, overview)
        except Exception as e:
            print(f"[warn] could not persist market overview: {e}")
        return overview

    # Nothing live: serve the last good snapshot rather than an empty screen,
    # clearly labelled as stale so nobody trades off a three-day-old index.
    stored = market_store.kv_get(SNAPSHOT_KEY)
    if stored:
        stored = dict(stored)
        stored["stale"] = True
        stored["stale_note"] = "Live market data is unavailable - showing the last stored snapshot."
        return stored
    overview["stale"] = True
    overview["stale_note"] = "No market data available yet."
    return overview


def market_context() -> dict:
    """The ensemble's market-context component input."""
    try:
        overview = build_overview()
    except Exception as e:
        return {"available": False, "reason": str(e)}
    if not overview.get("live") and not overview.get("indices"):
        return {"available": False, "reason": "no market data"}
    sentiment = overview.get("sentiment") or {}
    stale = bool(overview.get("stale"))
    return {
        "available": bool(sentiment) and not stale,
        "score": sentiment.get("score", 0.0),
        # Market context is real but coarse - it never deserves the
        # confidence a stock's own price action gets.
        "confidence": 0.35 if not stale else 0.1,
        "reasons": (sentiment.get("reasons") or [])[:3],
        "detail": {
            "label": sentiment.get("label"),
            "breadth_ratio": (overview.get("breadth") or {}).get("ratio"),
            "stale": stale,
        },
    }
