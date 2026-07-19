"""
Replacement template for Backend/data_sources.py

NOTE:
This is a template implementing the cached get_stock_universe() logic.
If your existing file contains additional functions, merge this implementation
into your project instead of overwriting unrelated code.
"""

import json
import os
import time

CACHE_FILE = "stock_universe_cache.json"
CACHE_MAX_AGE = 60 * 60 * 24

_FALLBACK_STOCKS = []

def fetch_nse_list():
    raise NotImplementedError

def fetch_bse_list():
    raise NotImplementedError

def _load_cache():
    try:
        if not os.path.exists(CACHE_FILE):
            return None
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def _save_cache(stocks):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump({"timestamp": time.time(), "stocks": stocks}, f)

def _cache_valid(cache):
    return cache and time.time() - cache["timestamp"] < CACHE_MAX_AGE

def get_stock_universe():
    cache = _load_cache()
    if _cache_valid(cache):
        return cache["stocks"]

    combined = []
    try:
        combined.extend(fetch_nse_list())
    except Exception:
        pass

    try:
        combined.extend(fetch_bse_list())
    except Exception:
        pass

    seen = set()
    unique = []
    for stock in combined:
        sym = stock.get("symbol","").upper()
        if sym not in seen:
            seen.add(sym)
            unique.append(stock)

    if unique:
        _save_cache(unique)
        return unique

    if cache:
        return cache["stocks"]

    return _FALLBACK_STOCKS
