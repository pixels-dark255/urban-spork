"""
All external data fetching lives here:
 - NSE / BSE full stock list (for search)
 - Multi-timeframe historical price data (yfinance)
 - Company news (NewsAPI.org)
 - Weather / seasonal proxy (Open-Meteo, keyless)

Everything is defensive: if a live source fails or is blocked, we fall back
to a small bundled sample list so the app never hard-crashes.
"""
import os
import io
import time
import datetime as dt
import requests
import pandas as pd
import yfinance as yf

NEWS_API_KEY = os.getenv("NEWS_API_KEY", "")

NSE_LIST_URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
BSE_LIST_URL = "https://api.bseindia.com/BseIndiaAPI/api/ListofScripCodes/w?Group=&Scripcode=&industry=&segment=Equity&status=Active"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

# In-memory cache so we don't hammer NSE/BSE on every keystroke of search
_STOCK_LIST_CACHE = {"data": None, "ts": 0}
_CACHE_TTL_SECONDS = 6 * 3600  # refresh twice a day

_FALLBACK_STOCKS = [
    {"symbol": "RELIANCE", "name": "Reliance Industries Ltd", "exchange": "NSE"},
    {"symbol": "TCS", "name": "Tata Consultancy Services Ltd", "exchange": "NSE"},
    {"symbol": "HDFCBANK", "name": "HDFC Bank Ltd", "exchange": "NSE"},
    {"symbol": "INFY", "name": "Infosys Ltd", "exchange": "NSE"},
    {"symbol": "ICICIBANK", "name": "ICICI Bank Ltd", "exchange": "NSE"},
    {"symbol": "SBIN", "name": "State Bank of India", "exchange": "NSE"},
    {"symbol": "TATAMOTORS", "name": "Tata Motors Ltd", "exchange": "NSE"},
    {"symbol": "ITC", "name": "ITC Ltd", "exchange": "NSE"},
    {"symbol": "HINDUNILVR", "name": "Hindustan Unilever Ltd", "exchange": "NSE"},
    {"symbol": "BAJFINANCE", "name": "Bajaj Finance Ltd", "exchange": "NSE"},
]


def fetch_nse_list() -> list[dict]:
    r = requests.get(NSE_LIST_URL, headers=_HEADERS, timeout=15)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    df.columns = [c.strip() for c in df.columns]
    out = []
    for _, row in df.iterrows():
        out.append({
            "symbol": str(row["SYMBOL"]).strip(),
            "name": str(row["NAME OF COMPANY"]).strip(),
            "exchange": "NSE",
        })
    return out


def fetch_bse_list() -> list[dict]:
    r = requests.get(BSE_LIST_URL, headers=_HEADERS, timeout=15)
    r.raise_for_status()
    data = r.json()
    out = []
    for row in data:
        code = row.get("SCRIP_CD") or row.get("scrip_cd")
        name = row.get("SCRIP_NAME") or row.get("scrip_name") or row.get("Scripname")
        if code and name:
            out.append({"symbol": str(code).strip(), "name": str(name).strip(), "exchange": "BSE"})
    return out


def get_stock_universe(force_refresh: bool = False) -> list[dict]:
    """Cached, combined NSE + BSE stock universe. Falls back to a small
    bundled list if both live sources fail (e.g. no internet in this session,
    or NSE/BSE temporarily blocking the request)."""
    now = time.time()
    if not force_refresh and _STOCK_LIST_CACHE["data"] and (now - _STOCK_LIST_CACHE["ts"] < _CACHE_TTL_SECONDS):
        return _STOCK_LIST_CACHE["data"]

    combined = []
    try:
        combined.extend(fetch_nse_list())
    except Exception as e:
        print(f"[warn] NSE list fetch failed: {e}")
    try:
        combined.extend(fetch_bse_list())
    except Exception as e:
        print(f"[warn] BSE list fetch failed: {e}")

    if not combined:
        combined = _FALLBACK_STOCKS

    _STOCK_LIST_CACHE["data"] = combined
    _STOCK_LIST_CACHE["ts"] = now
    return combined


def search_stocks(query: str, limit: int = 20) -> list[dict]:
    query = query.strip().lower()
    if not query:
        return []
    universe = get_stock_universe()
    scored = []
    for item in universe:
        sym = item["symbol"].lower()
        name = item["name"].lower()
        if query in sym or query in name:
            # rank exact/prefix symbol matches first
            score = 0 if sym == query else (1 if sym.startswith(query) else (2 if name.startswith(query) else 3))
            scored.append((score, item))
    scored.sort(key=lambda x: x[0])
    return [x[1] for x in scored[:limit]]


def to_yf_symbol(symbol: str, exchange: str) -> str:
    suffix = ".NS" if exchange.upper() == "NSE" else ".BO"
    if symbol.upper().endswith((".NS", ".BO")):
        return symbol.upper()
    return f"{symbol.upper()}{suffix}"


# Timeframes requested by the user, mapped to yfinance (period, interval) pairs.
# yfinance limits intraday granularity by lookback (e.g. 1m data only for last 7d),
# so shorter windows use finer intervals.
TIMEFRAMES = {
    "5y":  {"period": "5y",  "interval": "1wk"},
    "1y":  {"period": "1y",  "interval": "1d"},
    "6mo": {"period": "6mo", "interval": "1d"},
    "3mo": {"period": "3mo", "interval": "1d"},
    "2mo": {"period": "2mo", "interval": "1d"},
    "1mo": {"period": "1mo", "interval": "1d"},
    "4wk": {"period": "1mo", "interval": "1d"},
    "3wk": {"period": "1mo", "interval": "1d"},
    "2wk": {"period": "1mo", "interval": "1d"},
    "1wk": {"period": "5d",  "interval": "1h"},
    "4d":  {"period": "5d",  "interval": "1h"},
    "3d":  {"period": "5d",  "interval": "1h"},
    "2d":  {"period": "5d",  "interval": "1h"},
    "1d":  {"period": "2d",  "interval": "5m"},
    "recent_hours": {"period": "1d", "interval": "5m"},
}


def fetch_multi_timeframe(yf_symbol: str) -> dict[str, pd.DataFrame]:
    """Pull all requested timeframes for a symbol. Returns dict of DataFrames
    (empty DataFrame if that particular fetch fails)."""
    ticker = yf.Ticker(yf_symbol)
    out = {}
    # Group identical (period, interval) requests to minimise network calls
    seen = {}
    for label, cfg in TIMEFRAMES.items():
        key = (cfg["period"], cfg["interval"])
        if key not in seen:
            try:
                hist = ticker.history(period=cfg["period"], interval=cfg["interval"])
            except Exception as e:
                print(f"[warn] history fetch failed for {yf_symbol} {key}: {e}")
                hist = pd.DataFrame()
            seen[key] = hist
        out[label] = seen[key]
    return out


def fetch_latest_price(yf_symbol: str) -> float | None:
    try:
        ticker = yf.Ticker(yf_symbol)
        fast = ticker.fast_info
        price = fast.get("lastPrice") or fast.get("last_price")
        if price:
            return float(price)
        hist = ticker.history(period="1d", interval="1m")
        if not hist.empty:
            return float(hist["Close"].iloc[-1])
    except Exception as e:
        print(f"[warn] latest price fetch failed for {yf_symbol}: {e}")
    return None


def fetch_company_news(company_name: str, max_articles: int = 15) -> list[dict]:
    """Uses NewsAPI.org (https://newsapi.org) - free key required, set NEWS_API_KEY.
    Falls back to Google News RSS (keyless) if no key is configured."""
    if NEWS_API_KEY:
        try:
            r = requests.get(
                "https://newsapi.org/v2/everything",
                params={
                    "q": company_name,
                    "language": "en",
                    "sortBy": "publishedAt",
                    "pageSize": max_articles,
                    "apiKey": NEWS_API_KEY,
                },
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            articles = []
            for a in data.get("articles", []):
                articles.append({
                    "title": a.get("title", ""),
                    "description": a.get("description", "") or "",
                    "source": (a.get("source") or {}).get("name", ""),
                    "published_at": a.get("publishedAt", ""),
                    "url": a.get("url", ""),
                })
            return articles
        except Exception as e:
            print(f"[warn] NewsAPI fetch failed, falling back to RSS: {e}")

    # Keyless fallback: Google News RSS
    try:
        import xml.etree.ElementTree as ET
        q = requests.utils.quote(f"{company_name} stock")
        url = f"https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"
        r = requests.get(url, headers=_HEADERS, timeout=10)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        articles = []
        for item in root.findall(".//item")[:max_articles]:
            articles.append({
                "title": item.findtext("title") or "",
                "description": "",
                "source": (item.findtext("source") or ""),
                "published_at": item.findtext("pubDate") or "",
                "url": item.findtext("link") or "",
            })
        return articles
    except Exception as e:
        print(f"[warn] Google News RSS fallback failed: {e}")
        return []


def fetch_weather_signal(lat: float = 28.6139, lon: float = 77.2090) -> dict:
    """Open-Meteo (keyless). Defaults to Delhi as a general India-wide proxy.
    This is a coarse, experimental signal - weather has no proven general
    relationship to stock prices outside a few sectors (agri, power, travel)."""
    try:
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,precipitation,weather_code",
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
                "forecast_days": 3,
                "timezone": "Asia/Kolkata",
            },
            timeout=10,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[warn] weather fetch failed: {e}")
        return {}
