import os
import datetime as dt
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import storage
from data_sources import (
    search_stocks, to_yf_symbol, fetch_multi_timeframe,
    fetch_latest_price, fetch_company_news, fetch_weather_signal,
    fetch_intraday_bars,
)
from predictor import predict_price
from backtest import run_backtest_and_refine
from scheduler import start_scheduler, make_fresh_prediction
import intraday

app = FastAPI(title="NSE/BSE Stock Analyzer & Predictor")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_client_ip(request: Request) -> str:
    """Identifies 'you' for storage purposes. Prefers a stable client ID the
    frontend generates once and sends on every request (X-Client-Id) -
    public IP address is NOT reliable on Indian mobile networks, which
    reassign it constantly (sleep/wake, wifi<->mobile data switches, carrier
    NAT rotation), causing the watchlist to appear to 'vanish' on every such
    change even though the old data is untouched, just filed under an IP
    you're no longer using. IP is kept only as a fallback for any caller
    that doesn't send the header."""
    client_id = request.headers.get("x-client-id")
    if client_id:
        return client_id
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@app.on_event("startup")
def on_startup():
    start_scheduler(interval_minutes=int(os.getenv("TICK_MINUTES", "5")))


# ---------- Stock search ----------

@app.get("/api/stocks/search")
def api_search_stocks(q: str, limit: int = 20):
    results = search_stocks(q, limit=limit)
    for r in results:
        r["yf_symbol"] = to_yf_symbol(r["symbol"], r["exchange"])
    return {"query": q, "results": results}


# ---------- Full analysis ----------

HORIZON_PRESETS = {
    "15m": 15, "1h": 60, "4h": 240, "1d": 1440, "3d": 4320,
    "1wk": 10080, "1mo": 43200, "3mo": 129600,
}


@app.get("/api/stocks/{symbol}/analyze")
def api_analyze_stock(symbol: str, exchange: str = "NSE", horizon: str = "1d"):
    if horizon not in HORIZON_PRESETS:
        raise HTTPException(400, f"horizon must be one of {list(HORIZON_PRESETS)}")

    yf_symbol = to_yf_symbol(symbol, exchange)
    price = fetch_latest_price(yf_symbol)
    if price is None:
        raise HTTPException(
            502,
            f"Could not fetch live price for {yf_symbol} from Yahoo Finance right now. "
            f"This is usually a temporary data-source issue (rate limiting), not a bad symbol. "
            f"Check server logs for details and try again in a minute.",
        )

    tf_data = fetch_multi_timeframe(yf_symbol)
    news = fetch_company_news(symbol)
    weather = fetch_weather_signal()

    result = predict_price(
        timeframe_data=tf_data,
        current_price=price,
        horizon_minutes=HORIZON_PRESETS[horizon],
        news_articles=news,
        weather_json=weather,
    )
    result["symbol"] = symbol
    result["yf_symbol"] = yf_symbol
    result["horizon_label"] = horizon
    result["news"] = news[:8]
    return result


# ---------- Watchlist (JSON file, keyed by client IP - see storage.py) ----------

class WatchlistAddRequest(BaseModel):
    symbol: str
    exchange: str = "NSE"
    display_name: str | None = None
    horizon: str = "1d"


@app.get("/api/watchlist")
def api_get_watchlist(request: Request):
    ip = get_client_ip(request)
    items = storage.get_watchlist(ip)
    out = []
    for item in items:
        preds = item.get("predictions", [])
        latest = preds[-1] if preds else None
        resolved = [p for p in preds if p.get("resolved")]
        avg_abs_error = None
        if resolved:
            errs = [abs(p["error_pct"]) for p in resolved if p.get("error_pct") is not None]
            if errs:
                avg_abs_error = round(sum(errs) / len(errs), 3)

        # Use the price from the last scheduler tick (stored, instant) rather
        # than a fresh Yahoo fetch per item here - fetching live for N items
        # on every list load/poll was slow enough to time out the request
        # entirely (this is what "could not load watchlist" was). The
        # single-stock analysis screen still fetches genuinely live.
        live_price = latest["price_at_prediction"] if latest else None

        out.append({
            "id": item["id"],
            "symbol": item["symbol"],
            "display_name": item.get("display_name"),
            "horizon_minutes": item["horizon_minutes"],
            "live_price": live_price,
            "latest_prediction": {
                "made_at": latest["made_at"],
                "target_at": latest["target_at"],
                "price_at_prediction": latest["price_at_prediction"],
                "predicted_price": latest["predicted_price"],
                "confidence": latest["confidence"],
            } if latest else None,
            "track_record": {
                "resolved_count": len(resolved),
                "avg_abs_error_pct": avg_abs_error,
            },
            "backtest_summary": item.get("backtest_summary"),
        })
    return {"watchlist": out, "your_ip": ip}


@app.post("/api/watchlist")
def api_add_watchlist(req: WatchlistAddRequest, request: Request):
    if req.horizon not in HORIZON_PRESETS:
        raise HTTPException(400, f"horizon must be one of {list(HORIZON_PRESETS)}")
    ip = get_client_ip(request)
    yf_symbol = to_yf_symbol(req.symbol, req.exchange)
    item = storage.add_item(
        ip=ip,
        symbol=yf_symbol,
        display_name=req.display_name or req.symbol,
        horizon_minutes=HORIZON_PRESETS[req.horizon],
    )

    # One-time 90-trading-day backtest + weight refinement (see backtest.py).
    # ~90 lightweight predictions off one data fetch - typically a few
    # seconds, so we just do it inline rather than a background job.
    try:
        bt = run_backtest_and_refine(yf_symbol)
        storage.set_backtest_result(
            ip, item["id"], bt["backtest_history"], bt["refined_weights"], bt["summary"]
        )
        item["signal_weights"] = bt["refined_weights"]
    except Exception as e:
        print(f"[warn] backtest failed for {yf_symbol}: {e}")

    # Make an immediate live prediction (using the just-refined weights) so
    # the watchlist shows something right away instead of waiting for the
    # next 5-minute scheduler tick.
    try:
        make_fresh_prediction(ip, item)
    except Exception as e:
        print(f"[warn] initial prediction failed for {yf_symbol}: {e}")

    return {"id": item["id"], "symbol": item["symbol"]}


@app.delete("/api/watchlist/{item_id}")
def api_remove_watchlist(item_id: int, request: Request):
    ip = get_client_ip(request)
    removed = storage.remove_item(ip, item_id)
    if not removed:
        raise HTTPException(404, "not found")
    return {"deleted": item_id}


@app.get("/api/watchlist/{item_id}/history")
def api_watchlist_history(item_id: int, request: Request):
    ip = get_client_ip(request)
    item = storage.get_item(ip, item_id)
    if not item:
        raise HTTPException(404, "not found")
    return {"history": item.get("predictions", [])}


def _directional_accuracy_trend(predictions: list) -> dict | None:
    """What SHOULD improve as weights refine is the rate of getting the
    up/down direction right - not the raw % error, which stays noisy
    because day-to-day price moves are dominated by real market randomness
    no amount of weight-tuning can remove. This splits resolved live
    predictions into an early half and a recent half and compares hit rate."""
    resolved = [p for p in predictions if p.get("resolved") and p.get("correct_direction") is not None]
    if len(resolved) < 4:
        return None
    half = len(resolved) // 2
    early, recent = resolved[:half], resolved[half:]
    def hit_rate(preds):
        return round(100 * sum(1 for p in preds if p["correct_direction"]) / len(preds), 1)
    return {
        "resolved_count": len(resolved),
        "overall_pct": hit_rate(resolved),
        "early_period_pct": hit_rate(early),
        "recent_period_pct": hit_rate(recent),
        "improved": hit_rate(recent) >= hit_rate(early),
    }


@app.get("/api/watchlist/{item_id}/analysis")
def api_watchlist_analysis(item_id: int, request: Request):
    """The 'click a watchlist stock -> see its analysis' screen: a fresh,
    live prediction computed right now using this stock's own refined
    weights (not the generic defaults /analyze uses) - so what you see here
    actually reflects everything this stock's backtest and live track
    record have taught it so far."""
    ip = get_client_ip(request)
    item = storage.get_item(ip, item_id)
    if not item:
        raise HTTPException(404, "not found")

    yf_symbol = item["symbol"]
    price = fetch_latest_price(yf_symbol)
    if price is None:
        raise HTTPException(
            502,
            f"Could not fetch live price for {yf_symbol} from Yahoo Finance right now. "
            f"Usually temporary rate-limiting - try again in a minute.",
        )
    tf_data = fetch_multi_timeframe(yf_symbol)
    news = fetch_company_news(item.get("display_name") or yf_symbol)
    weather = fetch_weather_signal()

    result = predict_price(
        timeframe_data=tf_data,
        current_price=price,
        horizon_minutes=item["horizon_minutes"],
        news_articles=news,
        weather_json=weather,
        weights=item.get("signal_weights"),
    )
    result["symbol"] = yf_symbol
    result["display_name"] = item.get("display_name")
    result["item_id"] = item["id"]
    result["news"] = news[:8]
    result["backtest_summary"] = item.get("backtest_summary")
    result["directional_accuracy"] = _directional_accuracy_trend(item.get("predictions", []))
    return result


@app.get("/api/watchlist/{item_id}/detail")
def api_watchlist_detail(item_id: int, request: Request):
    """The '90-day backtest' screen: predicted vs actual for each backtested
    day, how accuracy and directional hit-rate trended from the earliest
    period to the most recent, the refined signal weights and how they've
    moved over time, and every live prediction made since."""
    ip = get_client_ip(request)
    item = storage.get_item(ip, item_id)
    if not item:
        raise HTTPException(404, "not found")
    return {
        "id": item["id"],
        "symbol": item["symbol"],
        "display_name": item.get("display_name"),
        "horizon_minutes": item["horizon_minutes"],
        "signal_weights": item.get("signal_weights"),
        "weights_history": item.get("weights_history", []),
        "backtest_summary": item.get("backtest_summary"),
        "backtest_history": item.get("backtest_history", []),
        "live_predictions": item.get("predictions", []),
        "directional_accuracy": _directional_accuracy_trend(item.get("predictions", [])),
    }


@app.get("/api/health")
def health():
    return {"status": "ok", "time": dt.datetime.utcnow().isoformat()}


# ---------- Intraday paper trading (simulated money only, no broker) ----------

class IntradayAddRequest(BaseModel):
    symbol: str
    exchange: str = "NSE"
    display_name: str | None = None


@app.get("/api/intraday/stocks")
def api_get_intraday_stocks(request: Request):
    ip = get_client_ip(request)
    stocks = storage.get_intraday_stocks(ip)
    out = []
    for s in stocks:
        timeframes = {}
        live_price = None
        for tf in intraday.TIMEFRAMES:
            portfolio = storage.get_intraday_portfolio(ip, s["symbol"], tf)
            if portfolio:
                # Cached from the last scheduler tick - avoids up to 4 live
                # Yahoo calls per stock on every list load, which was slow
                # enough to time the request out (same root cause as the
                # watchlist "could not load" bug). Detail screen still
                # fetches genuinely live for the one stock being viewed.
                cached_price = portfolio.get("last_price")
                if cached_price:
                    live_price = cached_price
                summary = intraday.portfolio_summary(portfolio, cached_price)
                summary["score"] = portfolio.get("last_score")
                timeframes[tf] = summary
        out.append({
            "symbol": s["symbol"],
            "display_name": s.get("display_name"),
            "added_at": s["added_at"],
            "live_price": live_price,
            "timeframes": timeframes,
        })
    return {"stocks": out, "starting_capital_per_timeframe": intraday.STARTING_CAPITAL}


@app.post("/api/intraday/stocks")
def api_add_intraday_stock(req: IntradayAddRequest, request: Request):
    ip = get_client_ip(request)
    yf_symbol = to_yf_symbol(req.symbol, req.exchange)
    storage.add_intraday_stock(ip, yf_symbol, req.display_name or req.symbol)
    return {"symbol": yf_symbol, "starting_capital_per_timeframe": intraday.STARTING_CAPITAL}


@app.delete("/api/intraday/stocks/{symbol}")
def api_remove_intraday_stock(symbol: str, request: Request):
    ip = get_client_ip(request)
    ok = storage.remove_intraday_stock(ip, symbol)
    if not ok:
        raise HTTPException(404, "not found")
    return {"removed": True}


@app.get("/api/intraday/stocks/{symbol}/detail")
def api_intraday_detail(symbol: str, request: Request):
    ip = get_client_ip(request)
    live_price = fetch_latest_price(symbol)
    timeframes = {}
    for tf in intraday.TIMEFRAMES:
        portfolio = storage.get_intraday_portfolio(ip, symbol, tf)
        if not portfolio:
            continue
        bars = fetch_intraday_bars(symbol, tf)
        signal = intraday.compute_signal(bars)
        timeframes[tf] = {
            "summary": intraday.portfolio_summary(portfolio, live_price),
            "current_signal": signal,
            "trade_log": list(reversed(portfolio.get("trade_log", [])))[:50],
        }
    if not timeframes:
        raise HTTPException(404, "not found")
    return {"symbol": symbol, "live_price": live_price, "timeframes": timeframes}


# ---------- Serve the PWA frontend ----------
frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.isdir(frontend_dir):
    app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")
