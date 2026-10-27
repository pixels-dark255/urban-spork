"""
Market profiles: what "a session" means outside the NSE.

The arena was built around one market with one shape - 09:15-15:30 IST,
Monday to Friday, minus NSE holidays - and that shape was spelled out in
constants scattered across the engine (14:45 entry cutoff, 15:15
square-off, "session" = calendar date). Trading US stocks, forex or crypto
from India breaks every one of those assumptions, so they are collected
here instead, once per market.

Three ideas carry the whole module:

**Open intervals.** Each market answers one question: over a span of real
time, when is it actually trading? NSE and US stocks give one interval per
trading day; crypto gives the whole span; forex gives one interval a week
(Sunday 17:00 to Friday 17:00 New York).

**The window.** The user picks the hours they want to watch, in their own
timezone. It is not the same thing as the market being open.

**The active session** is the intersection of the two, and it is what the
engine calls a "day": the longest contiguous stretch where the market is
open AND the user's window is active. Everything else - the opening range,
the entry cutoff, the square-off, the catch-up - is defined relative to
that, so none of it needs to know which market it is looking at.

Times are computed with zoneinfo, never with fixed offsets. The US session
is 19:00-01:30 IST in October and 20:00-02:30 IST in November, because the
US changes clocks and India does not; a hardcoded offset would silently
trade the wrong hours for four months of the year.

Nothing here places an order. See the regulatory note in README.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import threading
from zoneinfo import ZoneInfo

import market_calendar
import timeutil
from arena import costs as costs_mod

IST = timeutil.IST
NY = ZoneInfo("America/New_York")

BAR_MINUTES = 5
# A sliver of overlap is not a session. Three bars is the minimum that can
# produce an opening range, so anything shorter is skipped rather than
# half-traded.
MIN_SESSION_MINUTES = 15
MAX_SESSION_HOURS = 24


# ---------------------------------------------------------------------------
# Holiday data (the US list; NSE keeps its own loader in market_calendar)
# ---------------------------------------------------------------------------

_HOLIDAY_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_us_lock = threading.Lock()
_us_holidays: dict[str, str] = {}
_us_early: dict[str, str] = {}
_us_years: set[str] = set()
_us_meta: dict = {}


def _load_us_holidays() -> None:
    """Same contract as the NSE loader, including its hard-won rule: a
    weekend date in a holiday list means the list is a public-holiday
    calendar, not a trading calendar, and the whole file is suspect. Such
    an entry is rejected loudly rather than quietly honoured."""
    global _us_holidays, _us_early, _us_years, _us_meta
    path = os.path.join(_HOLIDAY_DIR, "us_holidays.json")
    try:
        with open(path) as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[warn] could not read {path}: {e}")
        payload = {}

    holidays, early, years = {}, {}, set()
    for bucket, target in (("holidays", holidays), ("early_closes", early)):
        for year, entries in (payload.get(bucket) or {}).items():
            if not entries:
                continue
            if bucket == "holidays":
                years.add(str(year))
            for entry in entries:
                date = (entry or {}).get("date")
                try:
                    parsed = dt.date.fromisoformat(date)
                except (TypeError, ValueError):
                    print(f"[warn] us_holidays: ignoring unparseable date {date!r}")
                    continue
                if parsed.weekday() >= 5:
                    print(f"[warn] us_holidays: ignoring weekend date {date} "
                          f"({entry.get('description', '?')}) - the NYSE does not list "
                          f"weekend dates; check the source list")
                    continue
                if str(parsed.year) != str(year):
                    print(f"[warn] us_holidays: ignoring {date}: filed under {year}")
                    continue
                target[date] = (entry.get("close") if bucket == "early_closes"
                                else entry.get("description", "Holiday"))
    with _us_lock:
        _us_holidays, _us_early, _us_years = holidays, early, years
        _us_meta = {"source": payload.get("source", ""),
                    "updated": payload.get("updated", "")}


_load_us_holidays()


def us_holiday_status() -> dict:
    with _us_lock:
        return {"years_loaded": sorted(_us_years), "holiday_count": len(_us_holidays),
                "early_close_count": len(_us_early),
                "source": _us_meta.get("source", ""), "updated": _us_meta.get("updated", "")}


def _us_is_trading_day(day: dt.date) -> bool:
    if day.weekday() >= 5:
        return False
    with _us_lock:
        return day.isoformat() not in _us_holidays


def _us_close_time(day: dt.date) -> dt.time:
    """16:00, or 13:00 on a published half day."""
    with _us_lock:
        early = _us_early.get(day.isoformat())
    if early:
        try:
            h, m = early.split(":")
            return dt.time(int(h), int(m))
        except (ValueError, AttributeError):
            pass
    return dt.time(16, 0)


# ---------------------------------------------------------------------------
# Cost models
# ---------------------------------------------------------------------------

class CostModel:
    """Per-leg fill adjustment and charges.

    `scale` converts a native quoted price into the book's currency (see
    `fx_for_session`). It matters here because a forex spread is quoted in
    pips of the native pair: half a pip of USD/JPY is 0.005 yen, which is
    not 0.005 dollars. Scaling the pip by the same factor as the price
    keeps the cost correct in whatever currency the book is kept in.
    """
    id = "base"
    defaults: dict = {}

    def merged(self, overrides: dict | None) -> dict:
        cfg = dict(self.defaults)
        for k, v in (overrides or {}).items():
            if k in cfg and isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
                cfg[k] = float(v)
        return cfg

    # The knob the slippage sweep turns. Each model names the one cost that
    # represents "how bad is my fill", so the sweep can scale it without
    # knowing which market it is sweeping.
    slippage_key = "slippage_pct"

    def fill_price(self, side: str, price: float, cfg: dict,
                   symbol: str = "", scale: float = 1.0) -> float:
        raise NotImplementedError

    def leg_charges(self, side: str, turnover: float, cfg: dict,
                    symbol: str = "") -> dict:
        raise NotImplementedError

    def round_trip_cost_estimate(self, price: float, qty: float, cfg: dict,
                                 symbol: str = "", scale: float = 1.0) -> float:
        buy = self.fill_price("BUY", price, cfg, symbol, scale) * qty
        sell = self.fill_price("SELL", price, cfg, symbol, scale) * qty
        return (buy - sell) + self.leg_charges("BUY", buy, cfg, symbol)["total"] \
            + self.leg_charges("SELL", sell, cfg, symbol)["total"]


class IndiaCostModel(CostModel):
    """The existing NSE intraday model, unchanged - see costs.py."""
    id = "nse"
    defaults = dict(costs_mod.DEFAULT_COSTS)

    def merged(self, overrides):
        return costs_mod.merged(overrides)

    def fill_price(self, side, price, cfg, symbol="", scale=1.0):
        return costs_mod.fill_price(side, price, cfg)

    def leg_charges(self, side, turnover, cfg, symbol=""):
        return costs_mod.leg_charges(side, turnover, cfg)

    def round_trip_cost_estimate(self, price, qty, cfg, symbol="", scale=1.0):
        return costs_mod.round_trip_cost_estimate(price, qty, cfg)


class USCostModel(CostModel):
    """US equities at a zero-commission retail broker.

    Commission is $0 by default but kept configurable, because "free" is a
    routing arrangement rather than a law. The SEC fee (sells only) and
    FINRA TAF are small but real; leaving them out would flatter every
    short-holding-period result.
    """
    id = "us"
    defaults = {
        "commission_per_order": 0.0,
        "sec_fee_sell_pct": 0.0000278,     # SEC Section 31, sells only
        "finra_taf_per_share": 0.000166,   # FINRA TAF, sells only, capped
        "finra_taf_cap": 8.30,
        "slippage_pct": 0.0002,            # 0.02% a side
    }

    def fill_price(self, side, price, cfg, symbol="", scale=1.0):
        slip = cfg["slippage_pct"]
        return price * (1 + slip) if side == "BUY" else price * (1 - slip)

    def leg_charges(self, side, turnover, cfg, symbol=""):
        commission = cfg["commission_per_order"]
        sec = turnover * cfg["sec_fee_sell_pct"] if side == "SELL" else 0.0
        total = commission + sec
        return {"commission": round(commission, 4), "sec_fee": round(sec, 6),
                "total": round(total, 6)}


class ForexCostModel(CostModel):
    """Retail spot forex: no commission, the cost is the spread.

    The spread is quoted in pips of the pair, half charged on each side, so
    a round trip pays the full quoted spread. A pip is 0.0001 for most
    pairs and 0.01 for yen-quoted ones; USD/INR is quoted to 4 decimals
    like a major.
    """
    id = "forex"
    defaults = {
        "spread_pips_default": 1.5,
        "spread_pips_eurusd": 1.0,
        "spread_pips_gbpusd": 1.5,
        "spread_pips_usdjpy": 1.0,
        "spread_pips_audusd": 1.2,
        "spread_pips_usdinr": 2.0,
    }
    slippage_key = None     # swept by scaling every spread instead

    @staticmethod
    def pair_of(symbol: str) -> str:
        return symbol.upper().replace("=X", "").replace("-", "").replace("/", "")

    @classmethod
    def pip_size(cls, symbol: str) -> float:
        # Yen is quoted to 2 decimals, so its pip is 100x a major's.
        return 0.01 if cls.pair_of(symbol).endswith("JPY") else 0.0001

    def spread_pips(self, symbol: str, cfg: dict) -> float:
        return float(cfg.get(f"spread_pips_{self.pair_of(symbol).lower()}",
                             cfg["spread_pips_default"]))

    def half_spread(self, symbol: str, cfg: dict, scale: float = 1.0) -> float:
        """Half the quoted spread, in the book's currency."""
        return 0.5 * self.spread_pips(symbol, cfg) * self.pip_size(symbol) * scale

    def fill_price(self, side, price, cfg, symbol="", scale=1.0):
        half = self.half_spread(symbol, cfg, scale)
        return price + half if side == "BUY" else price - half

    def leg_charges(self, side, turnover, cfg, symbol=""):
        # Spot forex at a retail broker charges no per-ticket commission;
        # the whole cost is already inside the fill price.
        return {"commission": 0.0, "total": 0.0}


class CryptoCostModel(CostModel):
    """Spot crypto at an exchange's taker rate, plus slippage."""
    id = "crypto"
    defaults = {
        "taker_fee_pct": 0.001,     # 0.10% a side
        "slippage_pct": 0.0005,     # 0.05% a side
    }

    def fill_price(self, side, price, cfg, symbol="", scale=1.0):
        slip = cfg["slippage_pct"]
        return price * (1 + slip) if side == "BUY" else price * (1 - slip)

    def leg_charges(self, side, turnover, cfg, symbol=""):
        fee = turnover * cfg["taker_fee_pct"]
        return {"taker_fee": round(fee, 6), "total": round(fee, 6)}


COST_MODELS = {m.id: m for m in [IndiaCostModel(), USCostModel(),
                                 ForexCostModel(), CryptoCostModel()]}


# ---------------------------------------------------------------------------
# Market profiles
# ---------------------------------------------------------------------------

class Market:
    id = "base"
    name = "Base"
    tz = IST
    # What to call the market's clock on screen. str(tz) would print
    # "UTC+05:30" for the fixed-offset IST the rest of the app uses.
    tz_label = "India"
    quote_currency = "INR"      # the currency the BOOK is kept in
    currency_symbol = "₹"
    has_volume = True
    fractional = False
    qty_step = 1.0
    default_symbols: list[str] = []
    presets: dict[str, list[str]] = {}
    # Starting capital for a NEW arena on this market, always in rupees.
    # Per market because what counts as a workable stake is not the same
    # everywhere: see UsMarket.
    default_capital_inr = 10_000.0
    cost_model_id = "nse"
    # Minutes before the session end: no new entries, then square everything off.
    entry_cutoff_before_end = 30
    square_off_before_end = 15
    allow_leverage = False
    symbol_hint = ""

    @property
    def costs(self) -> CostModel:
        return COST_MODELS[self.cost_model_id]

    def default_window(self) -> dict:
        raise NotImplementedError

    def open_intervals(self, start: dt.datetime, end: dt.datetime) -> list[tuple]:
        """Maximal (open, close) UTC-aware intervals overlapping [start, end]."""
        raise NotImplementedError

    def normalise_symbol(self, s: str) -> str | None:
        s = (s or "").strip().upper()
        return s or None

    def symbol_currency(self, symbol: str) -> str:
        return self.quote_currency

    def is_open(self, ts: dt.datetime) -> bool:
        ts = timeutil.ensure_aware(ts)
        pad = dt.timedelta(days=2)
        return any(a <= ts <= b for a, b in self.open_intervals(ts - pad, ts + pad))

    def status(self, now: dt.datetime | None = None) -> dict:
        now = timeutil.ensure_aware(now or timeutil.utc_now())
        is_open = self.is_open(now)
        nxt = None
        for a, _b in self.open_intervals(now, now + dt.timedelta(days=10)):
            if a > now:
                nxt = a
                break
        return {
            "id": self.id, "name": self.name, "is_open": is_open,
            "timezone": str(self.tz), "timezone_label": self.tz_label,
            "currency": self.quote_currency,
            "currency_symbol": self.currency_symbol,
            "now_local": now.astimezone(self.tz).isoformat(),
            "next_open": timeutil.to_iso(nxt) if nxt else None,
            "next_open_ist": nxt.astimezone(IST).isoformat() if nxt else None,
            "has_volume": self.has_volume, "fractional": self.fractional,
            "allow_leverage": self.allow_leverage,
        }


class NseMarket(Market):
    id = "nse"
    name = "India (NSE)"
    tz = IST
    quote_currency = "INR"
    currency_symbol = "₹"
    cost_model_id = "nse"
    # The NSE arena keeps the behaviour it has always had: last entry 14:45
    # (45 minutes before the 15:30 close), square-off 15:15.
    entry_cutoff_before_end = 45
    square_off_before_end = 15
    symbol_hint = "NSE symbols, e.g. RELIANCE (.NS is added for you)"
    # ₹10,000 against a 50% per-position cap left about ₹5,000 for one
    # position, and whole-share rounding then threw away a median 22% of
    # the intended size - a ₹3,100 share of TCS out of a ₹5,000 allowance
    # is one share and ₹1,900 idle. At ₹50,000 that waste drops to 3%.
    default_capital_inr = 50_000.0
    default_symbols = [
        "RELIANCE.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS", "TCS.NS",
        "SBIN.NS", "AXISBANK.NS", "ITC.NS", "BHARTIARTL.NS", "TATAMOTORS.NS",
        "TATASTEEL.NS", "HINDUNILVR.NS",
    ]
    presets = {
        "liquid_nifty": [
            "RELIANCE.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS", "TCS.NS",
            "SBIN.NS", "AXISBANK.NS", "ITC.NS", "BHARTIARTL.NS", "TATAMOTORS.NS",
            "TATASTEEL.NS", "HINDUNILVR.NS", "LT.NS", "HCLTECH.NS", "SUNPHARMA.NS"],
        "banking": [
            "HDFCBANK.NS", "ICICIBANK.NS", "SBIN.NS", "AXISBANK.NS",
            "KOTAKBANK.NS", "INDUSINDBK.NS"],
        "it": ["TCS.NS", "INFY.NS", "HCLTECH.NS", "WIPRO.NS", "TECHM.NS"],
        # Lower-priced names, where whole-share rounding barely bites: a
        # ₹250 share fills its allowance almost exactly, a ₹3,600 one
        # cannot. Useful if you run a smaller book.
        "low_priced": [
            "ITC.NS", "TATASTEEL.NS", "WIPRO.NS", "NTPC.NS", "POWERGRID.NS",
            "COALINDIA.NS", "ONGC.NS", "SBIN.NS"],
    }

    def default_window(self):
        return {"start": "09:15", "end": "15:30", "tz": "Asia/Kolkata", "days": [0, 1, 2, 3, 4]}

    def normalise_symbol(self, s):
        s = (s or "").strip().upper()
        if not s:
            return None
        return s if s.endswith((".NS", ".BO")) else s + ".NS"

    def open_intervals(self, start, end):
        out = []
        day = start.astimezone(self.tz).date() - dt.timedelta(days=1)
        last = end.astimezone(self.tz).date() + dt.timedelta(days=1)
        while day <= last:
            if market_calendar.is_trading_day(day):
                a = dt.datetime.combine(day, market_calendar.MARKET_OPEN, tzinfo=self.tz)
                b = dt.datetime.combine(day, market_calendar.MARKET_CLOSE, tzinfo=self.tz)
                if b >= start and a <= end:
                    out.append((a, b))
            day += dt.timedelta(days=1)
        return out


class UsMarket(Market):
    id = "us"
    name = "US stocks"
    tz = NY
    tz_label = "New York"
    quote_currency = "USD"
    currency_symbol = "$"
    cost_model_id = "us"
    symbol_hint = "US tickers, e.g. AAPL, SPY"
    # ₹10,000 is about $113, and US equities trade in whole shares - one
    # share of most of the symbols below costs more than that, so a default
    # of ₹10,000 produced an arena that skipped four entries in five and
    # showed an empty leaderboard. ₹50,000 is roughly $565, enough to size
    # a position in a mega-cap and still respect the risk limits.
    default_capital_inr = 50_000.0
    default_symbols = ["AAPL", "MSFT", "NVDA", "AMZN", "SPY", "QQQ"]
    presets = {
        "mega_cap": ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META"],
        "index_etfs": ["SPY", "QQQ", "DIA", "IWM"],
    }

    def default_window(self):
        # Expressed in New York time on purpose: the market's own session
        # is the default, and writing it as "19:00 IST" would be wrong for
        # the four months of the year when the US is not on daylight time.
        return {"start": "09:30", "end": "16:00", "tz": "America/New_York",
                "days": [0, 1, 2, 3, 4]}

    def open_intervals(self, start, end):
        out = []
        day = start.astimezone(self.tz).date() - dt.timedelta(days=1)
        last = end.astimezone(self.tz).date() + dt.timedelta(days=1)
        while day <= last:
            if _us_is_trading_day(day):
                a = dt.datetime.combine(day, dt.time(9, 30), tzinfo=self.tz)
                b = dt.datetime.combine(day, _us_close_time(day), tzinfo=self.tz)
                if b >= start and a <= end:
                    out.append((a, b))
            day += dt.timedelta(days=1)
        return out


class ForexMarket(Market):
    id = "forex"
    name = "Forex"
    tz = NY
    tz_label = "New York"
    quote_currency = "USD"      # the book is kept in USD; see symbol_currency
    currency_symbol = "$"
    has_volume = False          # Yahoo reports 0 volume on FX pairs
    fractional = True
    qty_step = 0.01
    cost_model_id = "forex"
    allow_leverage = True
    symbol_hint = "Yahoo FX pairs, e.g. EURUSD=X"
    default_symbols = ["EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X", "USDINR=X"]
    presets = {
        "majors": ["EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X"],
        "inr_pairs": ["USDINR=X", "EURINR=X", "GBPINR=X"],
    }

    def default_window(self):
        return {"start": "19:00", "end": "23:30", "tz": "Asia/Kolkata", "days": [0, 1, 2, 3, 4]}

    def normalise_symbol(self, s):
        s = (s or "").strip().upper().replace("/", "")
        if not s:
            return None
        return s if s.endswith("=X") else s + "=X"

    def symbol_currency(self, symbol):
        """The currency the pair is QUOTED in - the second half of the pair.
        EURUSD is dollars per euro, so it is quoted in USD; USDJPY is yen
        per dollar, so it is quoted in JPY."""
        pair = ForexCostModel.pair_of(symbol)
        return pair[3:6] if len(pair) >= 6 else self.quote_currency

    def open_intervals(self, start, end):
        """One interval a week: Sunday 17:00 to Friday 17:00, New York time.

        Anchored to New York rather than UTC because that is how the retail
        FX week is actually defined, and the boundary therefore moves with
        US daylight saving along with everything else.
        """
        out = []
        cursor = (start.astimezone(self.tz).date()
                  - dt.timedelta(days=start.astimezone(self.tz).weekday() + 9))
        last = end.astimezone(self.tz).date() + dt.timedelta(days=9)
        while cursor <= last:
            if cursor.weekday() == 6:       # Sunday
                a = dt.datetime.combine(cursor, dt.time(17, 0), tzinfo=self.tz)
                b = dt.datetime.combine(cursor + dt.timedelta(days=5), dt.time(17, 0),
                                        tzinfo=self.tz)
                if b >= start and a <= end:
                    out.append((a, b))
            cursor += dt.timedelta(days=1)
        return out


class CryptoMarket(Market):
    id = "crypto"
    name = "Crypto"
    tz = timeutil.UTC if hasattr(timeutil, "UTC") else dt.timezone.utc
    tz_label = "UTC"
    quote_currency = "USD"
    currency_symbol = "$"
    fractional = True
    qty_step = 1e-6
    cost_model_id = "crypto"
    allow_leverage = True
    symbol_hint = "Yahoo crypto symbols, e.g. BTC-USD"
    default_symbols = ["BTC-USD", "ETH-USD", "SOL-USD"]
    presets = {"majors": ["BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD"]}

    def default_window(self):
        return {"start": "19:00", "end": "23:30", "tz": "Asia/Kolkata",
                "days": [0, 1, 2, 3, 4, 5, 6]}

    def normalise_symbol(self, s):
        s = (s or "").strip().upper()
        if not s:
            return None
        return s if "-" in s else s + "-USD"

    def open_intervals(self, start, end):
        return [(start - dt.timedelta(days=2), end + dt.timedelta(days=2))]


MARKETS: dict[str, Market] = {m.id: m for m in
                              [NseMarket(), UsMarket(), ForexMarket(), CryptoMarket()]}
DEFAULT_MARKET = "nse"


def get(market_id: str | None) -> Market:
    return MARKETS.get((market_id or "").strip().lower(), MARKETS[DEFAULT_MARKET])


def is_market_id(market_id: str | None) -> bool:
    return (market_id or "").strip().lower() in MARKETS


def catalog(now: dt.datetime | None = None) -> list[dict]:
    return [m.status(now) for m in MARKETS.values()]


# ---------------------------------------------------------------------------
# Windows and active sessions
# ---------------------------------------------------------------------------

DEFAULT_WINDOW_DAYS = [0, 1, 2, 3, 4]


def normalise_window(raw: dict | None, market: Market, base: dict | None = None) -> dict:
    win = dict(base or market.default_window())
    raw = raw or {}
    for key in ("start", "end"):
        val = raw.get(key)
        if isinstance(val, str) and _parse_hhmm(val) is not None:
            win[key] = _parse_hhmm(val).strftime("%H:%M")
    tz = raw.get("tz")
    if isinstance(tz, str):
        try:
            ZoneInfo(tz)
            win["tz"] = tz
        except Exception:
            pass
    days = raw.get("days")
    if isinstance(days, list):
        cleaned = sorted({int(d) for d in days
                          if isinstance(d, (int, float)) and not isinstance(d, bool)
                          and 0 <= int(d) <= 6})
        if cleaned:
            win["days"] = cleaned
    win.setdefault("days", list(DEFAULT_WINDOW_DAYS))
    return {"start": win["start"], "end": win["end"], "tz": win["tz"], "days": win["days"]}


def _parse_hhmm(text: str) -> dt.time | None:
    try:
        h, m = str(text).strip().split(":")
        h, m = int(h), int(m)
    except (ValueError, AttributeError):
        return None
    if 0 <= h <= 23 and 0 <= m <= 59:
        return dt.time(h, m)
    return None


def window_span(window: dict, date: dt.date) -> tuple[dt.datetime, dt.datetime]:
    """The window's real-time span for the window-local date `date`.

    A window whose end is at or before its start crosses midnight, and
    belongs to the date it STARTS on - so "22:00-02:00 on the 5th" runs
    from the 5th into the 6th and settles once, as the 5th.
    """
    tz = ZoneInfo(window["tz"])
    start_t = _parse_hhmm(window["start"]) or dt.time(0, 0)
    end_t = _parse_hhmm(window["end"]) or dt.time(23, 59)
    start = dt.datetime.combine(date, start_t, tzinfo=tz)
    end = dt.datetime.combine(date, end_t, tzinfo=tz)
    if end <= start:
        end = dt.datetime.combine(date + dt.timedelta(days=1), end_t, tzinfo=tz)
    return start, end


def active_session(market: Market, window: dict, date: dt.date) -> tuple | None:
    """When this market is tradable inside the user's window on `date`.

    Returns (start, end) as aware datetimes, or None if the two never
    overlap - a weekend for a stock market, a day the window excludes, a
    holiday, or a forex window that falls entirely in the weekend gap.

    Where the overlap comes in pieces (a window straddling the Friday
    17:00 forex close, say), the longest contiguous piece is the session.
    Two disconnected stretches on one date would otherwise share a single
    opening range and square-off, which describes neither of them.
    """
    if date.weekday() not in window.get("days", DEFAULT_WINDOW_DAYS):
        return None
    w_start, w_end = window_span(window, date)
    if (w_end - w_start) > dt.timedelta(hours=MAX_SESSION_HOURS):
        w_end = w_start + dt.timedelta(hours=MAX_SESSION_HOURS)

    best = None
    for a, b in market.open_intervals(w_start, w_end):
        lo, hi = max(a, w_start), min(b, w_end)
        if hi - lo < dt.timedelta(minutes=MIN_SESSION_MINUTES):
            continue
        if best is None or (hi - lo) > (best[1] - best[0]):
            best = (lo, hi)
    return best


def session_date_for(market: Market, window: dict, ts: dt.datetime) -> str | None:
    """Which active session (by its start date) a moment belongs to, if any."""
    ts = timeutil.ensure_aware(ts)
    local = ts.astimezone(ZoneInfo(window["tz"])).date()
    for date in (local, local - dt.timedelta(days=1)):
        sess = active_session(market, window, date)
        if sess and sess[0] <= ts <= sess[1]:
            return date.isoformat()
    return None


def sessions_in_range(market: Market, window: dict, first: dt.date, last: dt.date,
                      limit: int = 400) -> list[str]:
    """Every session date from `first` to `last` inclusive that has one."""
    out, cursor = [], first
    while cursor <= last and len(out) < limit:
        if active_session(market, window, cursor):
            out.append(cursor.isoformat())
        cursor += dt.timedelta(days=1)
    return out


def session_bounds(market: Market, window: dict, date_iso: str) -> tuple | None:
    try:
        return active_session(market, window, dt.date.fromisoformat(date_iso))
    except (TypeError, ValueError):
        return None


def session_info(market: Market, window: dict, date: dt.date | None = None,
                 now: dt.datetime | None = None) -> dict:
    """What the UI prints about the window: today's session in IST and in
    the market's own local time, plus when the next one starts."""
    now = timeutil.ensure_aware(now or timeutil.utc_now())
    date = date or now.astimezone(ZoneInfo(window["tz"])).date()
    sess = active_session(market, window, date)
    upcoming, upcoming_date = None, None
    cursor = date
    for _ in range(14):
        candidate = active_session(market, window, cursor)
        if candidate and candidate[1] > now:
            upcoming, upcoming_date = candidate, cursor
            break
        cursor += dt.timedelta(days=1)

    def pair(span):
        if not span:
            return None
        a, b = span
        return {
            "start": timeutil.to_iso(a), "end": timeutil.to_iso(b),
            "start_ist": a.astimezone(IST).strftime("%H:%M"),
            "end_ist": b.astimezone(IST).strftime("%H:%M"),
            "start_local": a.astimezone(market.tz).strftime("%H:%M"),
            "end_local": b.astimezone(market.tz).strftime("%H:%M"),
            "minutes": int((b - a).total_seconds() // 60),
        }

    return {
        "window": dict(window),
        "today": pair(sess),
        "next": pair(upcoming),
        "next_date": upcoming_date.isoformat() if upcoming_date else None,
        "market_timezone": str(market.tz),
        "market_timezone_label": market.tz_label,
        # True when the market's own clock is the user's, so printing both
        # would just be the same two times twice.
        "same_clock_as_ist": market.tz_label == "India",
        "active_now": bool(sess and sess[0] <= now <= sess[1]),
    }


def minute_marks(market: Market, span: tuple) -> dict:
    """Entry cutoff and square-off as minutes from the session start.

    Expressed relative to the END of the session and then converted, so a
    short window gets a proportionate cutoff rather than NSE's 14:45 applied
    to a market that closes at 02:00 IST.
    """
    start, end = span
    total = max(1, int((end - start).total_seconds() // 60))
    square_off = max(0, total - market.square_off_before_end)
    cutoff = max(0, total - market.entry_cutoff_before_end)
    if cutoff >= square_off:
        # A window shorter than the cutoff would otherwise forbid every
        # entry. Give it the first half to enter and the rest to get out.
        cutoff = max(0, min(square_off - 1, total // 2))
    return {"total_minutes": total, "entry_cutoff": cutoff, "square_off": square_off}


# ---------------------------------------------------------------------------
# Currency
# ---------------------------------------------------------------------------

FX_SYMBOLS = {"USDINR": "USDINR=X", "USDJPY": "USDJPY=X"}


def fx_for_session(market: Market, symbols: list[str], quotes: dict) -> dict:
    """Rates for one session, recorded in the day's record and never re-read.

    `quotes` maps a Yahoo FX symbol to its last price. Two numbers come out:

      inr_per_unit   what one unit of the book's currency is worth in INR,
                     used to state capital and P&L in rupees.
      symbol_scale   per symbol, what multiplies its quoted price to put it
                     in the book's currency. It is 1.0 for everything
                     except a pair quoted in something other than the book
                     currency - USD/JPY prices are yen, so they are divided
                     by USD/JPY to become dollars.

    Scaling prices up front means the engine, the strategies and the costs
    all work in one currency and none of them has to know about FX.
    """
    usdinr = _positive(quotes.get(FX_SYMBOLS["USDINR"]))
    usdjpy = _positive(quotes.get(FX_SYMBOLS["USDJPY"]))
    inr_per_unit = 1.0 if market.quote_currency == "INR" else usdinr

    scale = {}
    for sym in symbols:
        cur = market.symbol_currency(sym)
        if cur == market.quote_currency:
            scale[sym] = 1.0
        elif cur == "JPY" and market.quote_currency == "USD":
            scale[sym] = (1.0 / usdjpy) if usdjpy else None
        elif cur == "INR" and market.quote_currency == "USD":
            scale[sym] = (1.0 / usdinr) if usdinr else None
        else:
            scale[sym] = None       # unknown cross: left out rather than guessed
    return {
        "inr_per_unit": inr_per_unit,
        "symbol_scale": scale,
        "quotes": {k: v for k, v in quotes.items() if _positive(v)},
        "currency": market.quote_currency,
        "at": timeutil.iso_now(),
        # True when a rate was wanted and missing, so the UI can say the
        # rupee figures are an assumption rather than a conversion.
        "incomplete": inr_per_unit is None or any(v is None for v in scale.values()),
    }


def _positive(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 and f == f else None


def to_inr(amount: float | None, fx: dict | None) -> float | None:
    """Convert a book-currency amount to rupees at the session's rate."""
    if amount is None or not fx:
        return None
    rate = fx.get("inr_per_unit")
    if not rate:
        return None
    return round(float(amount) * float(rate), 2)
