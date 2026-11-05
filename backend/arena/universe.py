"""
Choosing which stocks to trade, instead of being handed a list.

A fixed list of six or twelve large-caps is a bet that those twelve will be
the ones that move. Most days they are not. This picks the day's candidates
from a broad NSE universe on three things an intraday strategy actually
needs, in this order:

  **Liquidity.** A strategy that cannot get out is not a strategy. Ranked on
  median daily turnover (close x volume), with a hard floor - a stock that
  trades a few lakh rupees a day will not absorb a position and get out of
  it twice in one session at anything like the modelled price.

  **Movement.** An intraday strategy needs range. Ranked on ATR as a
  percentage of price, but through a *preference band*, not a maximum:
  something that moves 0.4% a day cannot pay for its own costs, and
  something moving 9% is usually news, a circuit or an illiquid spike
  rather than something a rule can trade.

  **Affordability.** Measured, not assumed. Position size is capped per
  position and then rounded DOWN to whole shares, so a share priced near
  the whole allowance wastes most of it: on a ₹5,000 allowance a ₹3,100
  share is one share and ₹1,900 idle. A stock is only a candidate if the
  allowance buys at least `MIN_SHARES` of it.

Everything is computed from DAILY bars up to and including the previous
session, never the current one, so the choice could have been made before
the market opened. Picking today's movers using today's data is the oldest
way to make a backtest look brilliant and a live account lose money.

The universe below is a broad list of liquid NSE names rather than an index
membership: an index is a marketing product, and its constituents change on
someone else's schedule. A symbol that no longer trades simply returns no
data and drops out of the screen, so a stale entry costs nothing but a slot
in one batched download.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

import data_sources
import timeutil

# Liquid NSE equities across sectors. Not an index: see the module docstring.
NSE_UNIVERSE = [
    # Banks and financials
    "HDFCBANK.NS", "ICICIBANK.NS", "SBIN.NS", "AXISBANK.NS", "KOTAKBANK.NS",
    "INDUSINDBK.NS", "BANKBARODA.NS", "PNB.NS", "CANBK.NS", "FEDERALBNK.NS",
    "IDFCFIRSTB.NS", "AUBANK.NS", "BAJFINANCE.NS", "BAJAJFINSV.NS", "SBILIFE.NS",
    "HDFCLIFE.NS", "ICICIGI.NS", "ICICIPRULI.NS", "SHRIRAMFIN.NS", "CHOLAFIN.NS",
    "MUTHOOTFIN.NS", "LICHSGFIN.NS", "PFC.NS", "RECLTD.NS", "IRFC.NS",
    # IT
    "TCS.NS", "INFY.NS", "HCLTECH.NS", "WIPRO.NS", "TECHM.NS",
    "LTIM.NS", "PERSISTENT.NS", "COFORGE.NS", "MPHASIS.NS", "OFSS.NS",
    # Energy, oil and gas
    "RELIANCE.NS", "ONGC.NS", "IOC.NS", "BPCL.NS", "HINDPETRO.NS",
    "GAIL.NS", "PETRONET.NS", "OIL.NS", "ATGL.NS", "IGL.NS",
    # Power and utilities
    "NTPC.NS", "POWERGRID.NS", "TATAPOWER.NS", "ADANIPOWER.NS", "JSWENERGY.NS",
    "NHPC.NS", "SJVN.NS", "TORNTPOWER.NS",
    # Metals and mining
    "TATASTEEL.NS", "JSWSTEEL.NS", "HINDALCO.NS", "VEDL.NS", "SAIL.NS",
    "NMDC.NS", "COALINDIA.NS", "JINDALSTEL.NS", "NATIONALUM.NS", "HINDZINC.NS",
    # Autos
    "MARUTI.NS", "TATAMOTORS.NS", "HEROMOTOCO.NS", "EICHERMOT.NS", "ASHOKLEY.NS",
    "TVSMOTOR.NS", "BHARATFORG.NS", "MOTHERSON.NS", "BALKRISIND.NS", "EXIDEIND.NS",
    # FMCG and consumer
    "ITC.NS", "HINDUNILVR.NS", "NESTLEIND.NS", "BRITANNIA.NS", "DABUR.NS",
    "MARICO.NS", "GODREJCP.NS", "COLPAL.NS", "TATACONSUM.NS", "VBL.NS",
    "UNITDSPR.NS", "JUBLFOOD.NS",
    # Pharma and healthcare
    "SUNPHARMA.NS", "CIPLA.NS", "DRREDDY.NS", "DIVISLAB.NS", "LUPIN.NS",
    "AUROPHARMA.NS", "TORNTPHARM.NS", "ALKEM.NS", "ZYDUSLIFE.NS", "BIOCON.NS",
    "APOLLOHOSP.NS", "LAURUSLABS.NS", "GLENMARK.NS",
    # Industrials, infra and cement
    "LT.NS", "SIEMENS.NS", "ABB.NS", "BHEL.NS", "BEL.NS",
    "HAL.NS", "CUMMINSIND.NS", "THERMAX.NS", "ULTRACEMCO.NS", "SHREECEM.NS",
    "AMBUJACEM.NS", "ACC.NS", "DALBHARAT.NS", "JKCEMENT.NS", "GRASIM.NS",
    "ADANIPORTS.NS", "ADANIENT.NS", "GMRINFRA.NS", "IRCTC.NS", "CONCOR.NS",
    # Telecom and media
    "BHARTIARTL.NS", "IDEA.NS", "INDUSTOWER.NS", "SUNTV.NS", "ZEEL.NS",
    # Chemicals and fertilisers
    "UPL.NS", "PIIND.NS", "SRF.NS", "DEEPAKNTR.NS", "AARTIIND.NS",
    "TATACHEM.NS", "CHAMBLFERT.NS", "COROMANDEL.NS", "NAVINFLUOR.NS",
    # Retail, realty and the rest
    "TITAN.NS", "TRENT.NS", "DMART.NS", "PAGEIND.NS", "ASIANPAINT.NS",
    "BERGEPAINT.NS", "PIDILITIND.NS", "HAVELLS.NS", "VOLTAS.NS", "CROMPTON.NS",
    "DLF.NS", "GODREJPROP.NS", "OBEROIRLTY.NS", "PRESTIGE.NS", "PHOENIXLTD.NS",
    "INDHOTEL.NS", "ZOMATO.NS", "PAYTM.NS", "NYKAA.NS", "POLICYBZR.NS",
    "IRB.NS", "RVNL.NS", "BSE.NS", "CDSL.NS", "ANGELONE.NS",
    "MANAPPURAM.NS", "POLYCAB.NS", "ASTRAL.NS", "SUPREMEIND.NS", "APLAPOLLO.NS",
]

UNIVERSES = {"nse": NSE_UNIVERSE}

# --- screen parameters -----------------------------------------------------
LOOKBACK_DAYS = 30          # daily bars used to rank
MIN_HISTORY = 15            # a stock with less than this is not judged
MIN_TURNOVER_CRORE = 25.0   # median daily turnover floor, in ₹ crore
ATR_SWEET_SPOT = (0.012, 0.045)   # 1.2%-4.5% daily range is tradeable
ATR_HARD_RANGE = (0.006, 0.09)    # outside this, not a candidate at all
# The share of the per-position allowance that must survive whole-share
# rounding. Expressed as utilisation rather than as "at least N shares",
# because the two disagree exactly where it matters: against a ₹2,000
# allowance a ₹1,750 share is one share and 87% of the money used, which
# is fine, while a share-count rule would reject it for not buying four.
MIN_UTILISATION = 0.75
MAX_SYMBOLS = 25            # the engine's own cap on a symbol list


def _atr_pct(df: pd.DataFrame) -> float | None:
    high, low, close = df["High"], df["Low"], df["Close"]
    prev = close.shift(1)
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14, min_periods=7).mean().iloc[-1]
    last = float(close.iloc[-1])
    if not np.isfinite(atr) or not np.isfinite(last) or last <= 0:
        return None
    return float(atr) / last


def _band_score(value: float, low: float, high: float) -> float:
    """1.0 inside the preferred band, tapering to 0 at the hard edges.

    A band rather than "more is better": an intraday strategy needs range,
    but the stocks with the most range on any given day are usually the
    ones with news, a circuit or no liquidity, and those are exactly the
    ones a rule-based entry handles worst.
    """
    if low <= value <= high:
        return 1.0
    if value < low:
        floor = ATR_HARD_RANGE[0]
        return max(0.0, (value - floor) / (low - floor)) if low > floor else 0.0
    ceiling = ATR_HARD_RANGE[1]
    return max(0.0, (ceiling - value) / (ceiling - low)) if ceiling > high else 0.0


def measure(frames: dict[str, pd.DataFrame],
            allowance: float | None) -> list[dict]:
    """Score every symbol we got usable daily bars for."""
    rows = []
    for symbol, df in frames.items():
        if df is None or len(df) < MIN_HISTORY:
            continue
        df = df.dropna(subset=["Close", "High", "Low"])
        if len(df) < MIN_HISTORY:
            continue
        close = df["Close"].astype(float)
        volume = df["Volume"].astype(float).fillna(0)
        price = float(close.iloc[-1])
        if price <= 0:
            continue
        turnover_crore = float((close * volume).median()) / 1e7
        atr_pct = _atr_pct(df)
        if atr_pct is None:
            continue
        reasons = []
        if turnover_crore < MIN_TURNOVER_CRORE:
            reasons.append(f"turnover ₹{turnover_crore:.0f}cr below floor")
        if not (ATR_HARD_RANGE[0] <= atr_pct <= ATR_HARD_RANGE[1]):
            reasons.append(f"daily range {atr_pct:.1%} outside tradeable band")
        # Whole-share rounding: see the module docstring.
        used = utilisation(allowance, price) if allowance else 1.0
        if allowance and used < MIN_UTILISATION:
            reasons.append(f"₹{price:,.0f} leaves {1 - used:.0%} of the "
                           f"₹{allowance:,.0f} allowance unused")
        rows.append({
            "symbol": symbol, "price": round(price, 2),
            "turnover_crore": round(turnover_crore, 1),
            "atr_pct": round(atr_pct, 4),
            "utilisation": round(used, 3),
            "excluded": reasons,
        })
    if not rows:
        return rows

    # Liquidity is ranked rather than used raw: turnover spans three orders
    # of magnitude, and a raw product would let one mega-cap outweigh every
    # other consideration.
    eligible = [r for r in rows if not r["excluded"]]
    if eligible:
        order = sorted(eligible, key=lambda r: r["turnover_crore"])
        n = len(order)
        for i, row in enumerate(order):
            row["liquidity_rank"] = (i + 1) / n
    for row in rows:
        row.setdefault("liquidity_rank", 0.0)
        row["movement_score"] = round(_band_score(row["atr_pct"], *ATR_SWEET_SPOT), 3)
        # Liquidity is the larger weight: an untradeable stock that moves a
        # lot is worse than a tradeable one that moves less.
        row["score"] = round(0.6 * row["liquidity_rank"] + 0.4 * row["movement_score"], 4)
    return rows


# One download per process, not per arena: a runner advancing five arenas
# on the same market would otherwise pull the same 160 daily series five
# times. The process is short-lived, so this never goes stale.
_daily_cache: dict[tuple, dict] = {}
FETCH_TIMEOUT = 45


def fetch_daily(symbols: list[str], period: str = "3mo") -> dict[str, pd.DataFrame]:
    """One batched download for the whole universe.

    ~160 separate requests would take minutes and get the runner's
    datacenter IP throttled; yfinance takes a space-separated list and
    returns a column MultiIndex, which is one request. Bounded by a
    timeout, because an unreachable Yahoo must cost this job seconds and
    not its whole budget.
    """
    frames: dict[str, pd.DataFrame] = {}
    if not symbols:
        return frames
    key = (tuple(symbols), period)
    if key in _daily_cache:
        return _daily_cache[key]
    # flatten=False: the column MultiIndex's second level is the ticker,
    # and without it a batched download is indistinguishable from one
    # symbol's bars repeated - which is exactly how this silently screened
    # nothing the first time it ran for real.
    raw = data_sources._yf_download(" ".join(symbols), period, "1d",
                                    timeout=FETCH_TIMEOUT, flatten=False)
    if raw is None or raw.empty:
        _daily_cache[key] = frames
        return frames
    if isinstance(raw.columns, pd.MultiIndex):
        # _yf_download flattens a MultiIndex for the single-ticker case; a
        # multi-ticker frame arrives here with it intact.
        for symbol in symbols:
            try:
                part = raw.xs(symbol, axis=1, level=1)
            except (KeyError, ValueError):
                continue
            if not part.dropna(how="all").empty:
                frames[symbol] = part.dropna(how="all")
    elif len(symbols) == 1:
        frames[symbols[0]] = raw
    _daily_cache[key] = frames
    return frames


def screen(market_id: str, count: int, allowance: float | None = None,
           frames: dict[str, pd.DataFrame] | None = None) -> dict:
    """Pick the day's candidates. Returns the picks and the full workings."""
    universe = UNIVERSES.get(market_id, [])
    if not universe:
        return {"symbols": [], "rows": [], "universe": 0,
                "note": f"no screening universe for {market_id}"}
    if frames is None:
        frames = fetch_daily(universe)
    rows = measure(frames, allowance)
    eligible = sorted((r for r in rows if not r["excluded"]),
                      key=lambda r: -r["score"])
    count = max(1, min(int(count), MAX_SYMBOLS))
    picks = [r["symbol"] for r in eligible[:count]]
    return {
        "symbols": picks,
        "rows": eligible[:count],
        "rejected": len(rows) - len(eligible),
        "universe": len(universe),
        "measured": len(rows),
        "at": timeutil.iso_now(),
        "allowance": allowance,
        "note": ("Ranked on 30 days of DAILY bars up to the previous session - "
                 "never today's - so the same list could have been chosen before "
                 "the open."),
    }


def is_stale(picked: dict | None, session_date: str) -> bool:
    """Picks are made once per session and reused, so every strategy in the
    arena trades the same stocks and the day stays comparable."""
    if not picked or not picked.get("symbols"):
        return True
    return picked.get("session") != session_date


def utilisation(allowance: float, price: float) -> float:
    """What fraction of one position's allowance a whole number of shares
    actually uses. 1.0 would be a perfect fit; 0.6 means two fifths of the
    money sits in cash because the next share does not fit."""
    if allowance <= 0 or price <= 0:
        return 0.0
    qty = int(allowance // price)
    return (qty * price) / allowance if qty else 0.0


def allowance_per_position(capital_inr: float, slots: int) -> float:
    """The book divided evenly between its concurrent positions."""
    return float(capital_inr) / max(1, int(slots))


def previous_session_cutoff(now: dt.datetime | None = None) -> dt.date:
    """The last date whose daily bar is complete - never today's."""
    ist = (now or timeutil.utc_now()).astimezone(timeutil.IST)
    return ist.date() - dt.timedelta(days=1)
