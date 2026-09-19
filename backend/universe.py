"""
The stock universe: the complete NSE + BSE equity master, cached to disk.

Requirements this module exists to satisfy:
 - search the full NSE and BSE universe, not a hardcoded shortlist
 - work when the market is closed, and when NSE/BSE are unreachable
 - survive restarts without re-downloading
 - pick up newly listed stocks on a periodic refresh
 - match on symbol, company name, partial input and typos

How it works: the master list lives in SQLite (market_store.stock_master).
On startup, if the table is empty it is seeded from a bundled CSV so search
works instantly even with no network at all. A refresh from NSE/BSE runs in
the background and UPSERTs on top - so a failed refresh degrades to slightly
stale data rather than to an empty search box.
"""
from __future__ import annotations

import io
import csv
import os
import time
import difflib
import threading
import datetime as dt

import requests

import config
import market_store

NSE_LIST_URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
BSE_LIST_URL = (
    "https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w"
    "?Group=&Scripcode=&industry=&segment=Equity&status=Active"
)
BSE_LIST_URL_FALLBACK = (
    "https://api.bseindia.com/BseIndiaAPI/api/ListofScripCodes/w"
    "?Group=&Scripcode=&industry=&segment=Equity&status=Active"
)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.bseindia.com/",
}

SEED_PATH = os.path.join(config.BASE_DIR, "seed", "nse_seed.csv")
LAST_REFRESH_KEY = "universe_last_refresh"

_index_lock = threading.Lock()
_index: list[dict] = []
_index_stamp = 0.0
_INDEX_TTL = 300  # seconds - how often the in-memory search index re-reads SQLite


# ---------------------------------------------------------------------------
# Loading / refreshing
# ---------------------------------------------------------------------------

def load_seed() -> list[dict]:
    """The bundled fallback universe. Small but real, and always available -
    this is what makes search work on a machine with no internet at all."""
    if not os.path.exists(SEED_PATH):
        return []
    out = []
    with open(SEED_PATH, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if not row.get("symbol"):
                continue
            out.append({
                "symbol": row["symbol"].strip().upper(),
                "name": (row.get("name") or "").strip(),
                "exchange": "NSE",
                "sector": (row.get("sector") or "").strip() or None,
                "industry": (row.get("industry") or "").strip() or None,
                "isin": None,
                "bse_code": None,
            })
    return out


def fetch_nse_list() -> list[dict]:
    r = requests.get(NSE_LIST_URL, headers=_HEADERS, timeout=20)
    r.raise_for_status()
    reader = csv.DictReader(io.StringIO(r.text))
    out = []
    for row in reader:
        row = { (k or "").strip().upper(): (v or "").strip() for k, v in row.items() }
        symbol = row.get("SYMBOL")
        name = row.get("NAME OF COMPANY")
        if not symbol or not name:
            continue
        series = row.get("SERIES", "")
        if series and series not in ("EQ", "BE", "BZ", "SM", "ST"):
            continue  # skip debt/ETF-only series - not tradable equities
        out.append({
            "symbol": symbol.upper(),
            "name": name,
            "exchange": "NSE",
            "isin": row.get("ISIN NUMBER") or None,
            "sector": None,
            "industry": None,
            "bse_code": None,
        })
    return out


def fetch_bse_list() -> list[dict]:
    last_error = None
    for url in (BSE_LIST_URL, BSE_LIST_URL_FALLBACK):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=20)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            last_error = e
            continue
        rows = data if isinstance(data, list) else data.get("Table", []) or []
        out = []
        for row in rows:
            code = row.get("SCRIP_CD") or row.get("Scrip_Code") or row.get("scrip_cd")
            name = (row.get("SCRIP_NAME") or row.get("Scrip_Name")
                    or row.get("scrip_name") or row.get("Scripname"))
            if not code or not name:
                continue
            # BSE's Scrip_Id is the ticker-like short code (e.g. RELIANCE);
            # the numeric SCRIP_CD is what Yahoo needs (e.g. 500325.BO), so
            # keep both - one for humans, one for the data provider.
            ticker = (row.get("Scrip_Id") or row.get("SCRIP_ID") or "").strip()
            out.append({
                "symbol": str(code).strip(),
                "name": str(name).strip(),
                "exchange": "BSE",
                "isin": (row.get("ISIN_NUMBER") or row.get("ISIN") or "").strip() or None,
                "sector": (row.get("Sector_Name") or "").strip() or None,
                "industry": (row.get("Industry") or row.get("INDUSTRY") or "").strip() or None,
                "bse_code": ticker or None,
            })
        if out:
            return out
    if last_error:
        raise last_error
    return []


def refresh_universe(force: bool = False) -> dict:
    """Pull NSE + BSE and UPSERT into the master table. Safe to call often -
    it no-ops unless the configured refresh interval has elapsed."""
    last = market_store.kv_get(LAST_REFRESH_KEY)
    if not force and last:
        try:
            age_h = (time.time() - float(last.get("ts", 0))) / 3600.0
            if age_h < config.UNIVERSE_REFRESH_HOURS:
                return {"refreshed": False, "reason": "recent", "age_hours": round(age_h, 2)}
        except (TypeError, ValueError):
            pass

    written = {"nse": 0, "bse": 0}
    errors = {}
    for label, fetcher in (("nse", fetch_nse_list), ("bse", fetch_bse_list)):
        try:
            records = fetcher()
            written[label] = market_store.replace_stock_master(records)
        except Exception as e:
            errors[label] = str(e)
            print(f"[warn] {label.upper()} universe refresh failed: {e}")

    if any(written.values()):
        market_store.kv_set(LAST_REFRESH_KEY, {"ts": time.time(), "written": written})
    _invalidate_index()
    return {"refreshed": bool(any(written.values())), "written": written, "errors": errors,
            "total": market_store.stock_master_count()}


def ensure_seeded() -> int:
    """Guarantee the master table is never empty. Called at startup."""
    count = market_store.stock_master_count()
    if count == 0:
        seed = load_seed()
        if seed:
            market_store.replace_stock_master(seed)
            print(f"[info] universe seeded with {len(seed)} bundled stocks")
        count = market_store.stock_master_count()
    return count


def universe_status() -> dict:
    last = market_store.kv_get(LAST_REFRESH_KEY) or {}
    ts = last.get("ts")
    return {
        "total_stocks": market_store.stock_master_count(),
        "by_exchange": _counts_by_exchange(),
        "last_refresh": (dt.datetime.fromtimestamp(ts).isoformat() if ts else None),
        "refresh_interval_hours": config.UNIVERSE_REFRESH_HOURS,
        "seed_available": os.path.exists(SEED_PATH),
    }


def _counts_by_exchange() -> dict:
    counts = {}
    for row in _get_index():
        counts[row["exchange"]] = counts.get(row["exchange"], 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def _invalidate_index():
    global _index_stamp
    with _index_lock:
        _index_stamp = 0.0


def _get_index() -> list[dict]:
    global _index, _index_stamp
    with _index_lock:
        if _index and (time.time() - _index_stamp) < _INDEX_TTL:
            return _index
        rows = market_store.load_stock_master()
        for r in rows:
            r["_sym"] = (r["symbol"] or "").lower()
            r["_name"] = (r["name"] or "").lower()
            r["_bse"] = (r.get("bse_code") or "").lower()
        _index = rows
        _index_stamp = time.time()
        return _index


def _score(query: str, row: dict) -> float:
    """Lower is better. Exact symbol beats symbol prefix beats name prefix
    beats substring beats fuzzy. Fuzzy is what turns 'relaince' into
    Reliance; the ladder above it is what keeps 'TCS' from ranking a
    company whose name merely contains 't', 'c' and 's' above the actual
    ticker TCS."""
    sym, name, bse = row["_sym"], row["_name"], row["_bse"]
    if sym == query or bse == query:
        return 0.0
    if sym.startswith(query) or (bse and bse.startswith(query)):
        return 1.0 + len(sym) / 1000.0
    if name.startswith(query):
        return 2.0 + len(name) / 1000.0
    # word-start match: "tata mot" -> Tata Motors
    words = name.split()
    if any(w.startswith(query) for w in words):
        return 3.0 + len(name) / 1000.0
    if query in sym:
        return 4.0
    if query in name:
        return 5.0 + name.index(query) / 100.0
    # Fuzzy: only worth computing for short-ish candidates, and only
    # accepted above a similarity floor, otherwise every stock "matches".
    ratio = max(
        difflib.SequenceMatcher(None, query, sym).ratio(),
        difflib.SequenceMatcher(None, query, name[:40]).ratio(),
    )
    if ratio >= 0.62:
        return 6.0 + (1.0 - ratio)
    return float("inf")


def search(query: str, limit: int = 20, exchange: str | None = None) -> list[dict]:
    q = (query or "").strip().lower()
    if not q:
        return []
    rows = _get_index()
    scored = []
    for row in rows:
        if exchange and row["exchange"] != exchange.upper():
            continue
        s = _score(q, row)
        if s == float("inf"):
            continue
        scored.append((s, row))
    scored.sort(key=lambda x: (x[0], x[1]["_name"]))

    out = []
    for score, row in scored[:limit]:
        out.append({
            "symbol": row["symbol"],
            "name": row["name"],
            "exchange": row["exchange"],
            "isin": row.get("isin"),
            "sector": row.get("sector"),
            "industry": row.get("industry"),
            "bse_code": row.get("bse_code"),
            "match_score": round(score, 3),
        })
    return out


def resolve(symbol: str, exchange: str = "NSE") -> dict | None:
    """Look up one stock's master record (for display names, sector, ISIN)."""
    row = market_store.get_stock(symbol.upper(), exchange.upper())
    if row:
        return row
    for r in _get_index():
        if r["_sym"] == symbol.lower() and r["exchange"] == exchange.upper():
            return r
    return None


def display_name(symbol: str, exchange: str = "NSE") -> str:
    row = resolve(symbol, exchange)
    return (row or {}).get("name") or symbol
