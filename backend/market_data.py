"""
Market data facade: timeframes, resampling, persistence and fallbacks.

This is the only module the engines talk to for price data. It:
 - defines every timeframe the platform supports and how to obtain it,
 - resamples where the upstream provider doesn't serve a timeframe natively
   (Yahoo has no 10m or 2h bars, for example - but they're exact rollups of
   5m and 1h bars, so they're computed rather than faked),
 - persists every bar it fetches into the local database, so the historical
   dataset grows on its own just by using the app,
 - falls back to that stored history when the live provider is unreachable,
   which is what keeps the app usable outside market hours and during a
   Yahoo outage.

Honest note on 30s: no free provider serves sub-minute bars for Indian
equities. The 30s timeframe therefore runs on 1m bars and is flagged
`approximated: true` everywhere it surfaces, rather than pretending to a
resolution the data doesn't have.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

import market_store
import providers

IST = "Asia/Kolkata"


class Timeframe:
    __slots__ = ("label", "base_interval", "resample_minutes", "minutes", "period",
                 "approximated", "note")

    def __init__(self, label, base_interval, minutes, period,
                 resample_minutes=None, approximated=False, note=None):
        self.label = label
        self.base_interval = base_interval
        self.minutes = minutes
        self.period = period
        self.resample_minutes = resample_minutes
        self.approximated = approximated
        self.note = note

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "minutes": self.minutes,
            "base_interval": self.base_interval,
            "resampled_from": self.base_interval if self.resample_minutes else None,
            "approximated": self.approximated,
            "note": self.note,
        }


# period values are the widest windows Yahoo actually serves per interval
# (1m: 7 days, 2m-90m: 60 days, 1h: 730 days).
TIMEFRAMES: dict[str, Timeframe] = {
    "30s":  Timeframe("30s", "1m", 0.5, "5d", approximated=True,
                      note="No free source provides sub-minute Indian equity bars; "
                           "this runs on 1-minute bars."),
    "1m":   Timeframe("1m", "1m", 1, "5d"),
    "2m":   Timeframe("2m", "2m", 2, "1mo"),
    "5m":   Timeframe("5m", "5m", 5, "1mo"),
    "10m":  Timeframe("10m", "5m", 10, "1mo", resample_minutes=10),
    "15m":  Timeframe("15m", "15m", 15, "1mo"),
    "30m":  Timeframe("30m", "30m", 30, "2mo"),
    "1h":   Timeframe("1h", "1h", 60, "6mo"),
    "1.5h": Timeframe("1.5h", "1h", 90, "6mo", resample_minutes=90),
    "2h":   Timeframe("2h", "1h", 120, "6mo", resample_minutes=120),
}

INTRADAY_TIMEFRAMES = list(TIMEFRAMES.keys())

# How many bars ahead a recommendation is considered to play out over. Short
# timeframes need more bars for the move to mean anything; long ones fewer.
HOLD_BARS = {
    "30s": 20, "1m": 15, "2m": 12, "5m": 10, "10m": 8,
    "15m": 6, "30m": 5, "1h": 4, "1.5h": 3, "2h": 3,
}


def timeframe_catalog() -> list[dict]:
    out = []
    for tf in TIMEFRAMES.values():
        d = tf.as_dict()
        d["hold_bars"] = HOLD_BARS.get(tf.label)
        d["horizon_minutes"] = round(tf.minutes * HOLD_BARS.get(tf.label, 1), 2)
        out.append(d)
    return out


def resample_bars(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Roll finer bars up to `minutes`, aligned to each session's first bar
    (not to the wall clock) so a 10-minute bar starts at 09:15, the way an
    Indian market session actually opens - not at 09:10, which would splice
    two sessions' worth of price action into one candle."""
    if df is None or df.empty:
        return df
    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    rule = f"{minutes}min"
    chunks = []
    for _, day_df in df.groupby(df.index.date):
        if day_df.empty:
            continue
        chunks.append(day_df.resample(rule, origin="start").agg(agg).dropna(subset=["Close"]))
    if not chunks:
        return df.iloc[0:0]
    return pd.concat(chunks).sort_index()


def _frame_to_rows(df: pd.DataFrame) -> list[dict]:
    rows = []
    for idx, row in df.iterrows():
        ts = pd.Timestamp(idx)
        ts = ts.tz_convert("UTC") if ts.tzinfo is not None else ts.tz_localize("UTC")
        rows.append({
            "ts": int(ts.timestamp()),
            "open": float(row["Open"]), "high": float(row["High"]),
            "low": float(row["Low"]), "close": float(row["Close"]),
            "volume": float(row.get("Volume", 0) or 0),
        })
    return rows


def _rows_to_frame(rows: list[dict]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    df = pd.DataFrame(rows)
    idx = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert(IST)
    out = pd.DataFrame({
        "Open": df["open"].astype(float), "High": df["high"].astype(float),
        "Low": df["low"].astype(float), "Close": df["close"].astype(float),
        "Volume": df["volume"].astype(float),
    })
    out.index = pd.DatetimeIndex(idx)
    return out.sort_index()


def get_bars(symbol: str, exchange: str = "NSE", timeframe: str = "5m",
             persist: bool = True, allow_stored_fallback: bool = True) -> pd.DataFrame:
    """Bars for one symbol at one timeframe, live if possible and from the
    local historical store if not."""
    tf = TIMEFRAMES.get(timeframe)
    if tf is None:
        raise ValueError(f"unknown timeframe '{timeframe}'")

    live = pd.DataFrame()
    try:
        live = providers.get_provider().get_bars(symbol, exchange, tf.base_interval, tf.period)
    except Exception as e:
        print(f"[warn] provider bar fetch failed for {symbol} {timeframe}: {e}")

    if live is not None and not live.empty:
        if persist:
            try:
                market_store.save_bars(symbol, exchange, tf.base_interval, _frame_to_rows(live))
            except Exception as e:
                print(f"[warn] could not persist bars for {symbol}: {e}")
        base = live
    elif allow_stored_fallback:
        base = _rows_to_frame(market_store.load_bars(symbol, exchange, tf.base_interval, limit=3000))
    else:
        base = pd.DataFrame()

    if base is None or base.empty:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

    if tf.resample_minutes:
        return resample_bars(base, tf.resample_minutes)
    return base


def get_daily_bars(symbol: str, exchange: str = "NSE", period: str = "2y",
                   persist: bool = True) -> pd.DataFrame:
    live = pd.DataFrame()
    try:
        live = providers.get_provider().get_bars(symbol, exchange, "1d", period)
    except Exception as e:
        print(f"[warn] daily fetch failed for {symbol}: {e}")
    if live is not None and not live.empty:
        if persist:
            try:
                market_store.save_bars(symbol, exchange, "1d", _frame_to_rows(live))
            except Exception as e:
                print(f"[warn] could not persist daily bars for {symbol}: {e}")
        return live
    return _rows_to_frame(market_store.load_bars(symbol, exchange, "1d", limit=3000))


def get_quote(symbol: str, exchange: str = "NSE") -> float | None:
    """Live price, or the most recent stored close when the market is shut or
    the provider is unreachable. Returning a stale-but-real price beats
    returning nothing: every consumer of this also gets `as_of` from the bar
    data, so nothing silently treats a Friday close as a live tick."""
    try:
        price = providers.get_provider().get_quote(symbol, exchange)
        if price:
            return round(float(price), 2)
    except Exception as e:
        print(f"[warn] quote fetch failed for {symbol}: {e}")
    for tf in ("1m", "5m", "15m", "1d"):
        rows = market_store.load_bars(symbol, exchange, tf, limit=1)
        if rows:
            return round(float(rows[-1]["close"]), 2)
    return None


def last_bar_time(df: pd.DataFrame) -> str | None:
    if df is None or df.empty:
        return None
    ts = pd.Timestamp(df.index[-1])
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts
    return ts.tz_convert(IST).isoformat()


def bars_to_points(df: pd.DataFrame, limit: int = 200) -> list[dict]:
    """Chart-ready OHLC points (epoch seconds), newest `limit` bars."""
    if df is None or df.empty:
        return []
    tail = df.tail(limit)
    out = []
    for idx, row in tail.iterrows():
        ts = pd.Timestamp(idx)
        ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts
        out.append({
            "time": int(ts.timestamp()),
            "open": round(float(row["Open"]), 2),
            "high": round(float(row["High"]), 2),
            "low": round(float(row["Low"]), 2),
            "close": round(float(row["Close"]), 2),
            "volume": float(row.get("Volume", 0) or 0),
        })
    return out


def collect_history(symbol: str, exchange: str = "NSE",
                    timeframes: list[str] | None = None) -> dict:
    """Explicit data-collection pass: fetch and store bars for a symbol
    across timeframes. Called by the background collector and exposed as an
    API endpoint so history can be back-filled on demand."""
    timeframes = timeframes or ["1m", "5m", "15m", "30m", "1h"]
    written = {}
    for tf_label in timeframes:
        tf = TIMEFRAMES.get(tf_label)
        if tf is None:
            continue
        try:
            df = providers.get_provider().get_bars(symbol, exchange, tf.base_interval, tf.period)
            if df is not None and not df.empty:
                written[tf.base_interval] = market_store.save_bars(
                    symbol, exchange, tf.base_interval, _frame_to_rows(df)
                )
        except Exception as e:
            print(f"[warn] collect_history failed for {symbol} {tf_label}: {e}")
    try:
        daily = providers.get_provider().get_bars(symbol, exchange, "1d", "2y")
        if daily is not None and not daily.empty:
            written["1d"] = market_store.save_bars(symbol, exchange, "1d", _frame_to_rows(daily))
    except Exception as e:
        print(f"[warn] collect_history daily failed for {symbol}: {e}")
    return {"symbol": symbol, "exchange": exchange, "bars_written": written,
            "at": dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat()}
