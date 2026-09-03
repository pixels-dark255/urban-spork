"""
Yahoo Finance provider (via yfinance). Keyless, the platform's default.

Yahoo actively blocks plain python-requests traffic from datacenter IPs, so
this reuses the curl_cffi browser-impersonating session already set up in
data_sources.py rather than opening a second, weaker one.
"""
from __future__ import annotations

import pandas as pd

from providers.base import MarketDataProvider, normalise_frame, empty_frame


def _yf_symbol(symbol: str, exchange: str) -> str:
    s = symbol.upper()
    # Indices (^NSEI, ^BSESN, ^NSEBANK) are already Yahoo-native and must not
    # be given an exchange suffix, or they resolve to nothing.
    if s.startswith("^") or s.endswith((".NS", ".BO")):
        return s
    return f"{s}{'.NS' if exchange.upper() == 'NSE' else '.BO'}"


class YahooProvider(MarketDataProvider):
    name = "yahoo"

    def is_available(self) -> bool:
        return True  # keyless

    def capabilities(self) -> dict:
        return {
            "quotes": True,
            "historical": True,
            "intraday": True,      # limited lookback per interval, see market_data.py
            "streaming": False,
            "orders": False,
        }

    def get_bars(self, symbol: str, exchange: str, interval: str, period: str) -> pd.DataFrame:
        from data_sources import _yf_download  # local import keeps the session single-instanced
        raw = _yf_download(_yf_symbol(symbol, exchange), period, interval)
        return normalise_frame(raw)

    def get_quote(self, symbol: str, exchange: str) -> float | None:
        from data_sources import fetch_latest_price
        return fetch_latest_price(_yf_symbol(symbol, exchange))
