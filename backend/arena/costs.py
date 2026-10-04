"""
Transaction costs for NSE intraday (MIS) equity trades.

Why this matters more than any strategy tweak: on a ₹5,000-₹10,000 book a
typical intraday move is a few rupees per share, and a round trip costs a
few rupees in charges plus slippage. A simulator that ignores costs reports
strategies as profitable that would lose money for real. Every trade in the
arena is charged both legs.

Defaults follow a discount-broker schedule (Zerodha-style, as published in
2025-26). Rates change - edit DEFAULT_COSTS or pass overrides in the arena
config; everything reads from one dict.

  brokerage       0.03% of turnover per executed order, capped at ₹20
  STT             0.025% on the SELL side only (intraday equity)
  exchange txn    NSE ~0.00297% of turnover, both legs
  SEBI fee        ₹10 per crore of turnover, both legs
  stamp duty      0.003% on the BUY side only
  GST             18% on (brokerage + exchange txn + SEBI fee)
  slippage        0.05% adverse fill per leg (market orders on liquid stocks)
"""
from __future__ import annotations

DEFAULT_COSTS = {
    "brokerage_pct": 0.0003,
    "brokerage_cap": 20.0,
    "stt_sell_pct": 0.00025,
    "exchange_pct": 0.0000297,
    "sebi_per_crore": 10.0,
    "stamp_buy_pct": 0.00003,
    "gst_pct": 0.18,
    "slippage_pct": 0.0005,
}


def merged(overrides: dict | None) -> dict:
    cfg = dict(DEFAULT_COSTS)
    for k, v in (overrides or {}).items():
        if k in cfg and isinstance(v, (int, float)) and v >= 0:
            cfg[k] = float(v)
    return cfg


def fill_price(side: str, price: float, cfg: dict) -> float:
    """Adverse slippage: buys fill a little higher, sells a little lower."""
    slip = cfg["slippage_pct"]
    return price * (1 + slip) if side == "BUY" else price * (1 - slip)


def leg_charges(side: str, turnover: float, cfg: dict) -> dict:
    """Statutory + broker charges for one executed order (not slippage,
    which is already inside the fill price)."""
    brokerage = min(turnover * cfg["brokerage_pct"], cfg["brokerage_cap"])
    exchange = turnover * cfg["exchange_pct"]
    sebi = turnover * cfg["sebi_per_crore"] / 1e7
    stt = turnover * cfg["stt_sell_pct"] if side == "SELL" else 0.0
    stamp = turnover * cfg["stamp_buy_pct"] if side == "BUY" else 0.0
    gst = (brokerage + exchange + sebi) * cfg["gst_pct"]
    total = brokerage + exchange + sebi + stt + stamp + gst
    return {
        "brokerage": round(brokerage, 4),
        "stt": round(stt, 4),
        "exchange": round(exchange, 4),
        "sebi": round(sebi, 6),
        "stamp": round(stamp, 4),
        "gst": round(gst, 4),
        "total": round(total, 4),
    }


def round_trip_cost_estimate(price: float, qty: int, cfg: dict) -> float:
    """Charges + slippage for buying and selling `qty` at roughly `price`.
    Used by the engine to refuse trades whose target can't cover costs."""
    buy = fill_price("BUY", price, cfg) * qty
    sell = fill_price("SELL", price, cfg) * qty
    slippage = buy - sell
    return slippage + leg_charges("BUY", buy, cfg)["total"] + leg_charges("SELL", sell, cfg)["total"]
