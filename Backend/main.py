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
)
from predictor import predict_price
from backtest import run_backtest_and_refine
from scheduler import start_scheduler, make_fresh_prediction

app = FastAPI(title="NSE/BSE Stock Analyzer & Predictor")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_client_ip(request: Request) -> str:
    """Render (and most cloud hosts) sit behind a proxy, so the real client
    IP is in X-Forwarded-For, not request.client.host."""
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

        out.append({
            "id": item["id"],
            "symbol": item["symbol"],
            "display_name": item.get("display_name"),
            "horizon_minutes": item["horizon_minutes"],
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


@app.get("/api/watchlist/{item_id}/detail")
def api_watchlist_detail(item_id: int, request: Request):
    """Full picture for the watchlist tap-through view: the 90-day backtest
    (predicted vs actual for each day, and how accuracy trended from the
    earliest backtested period to the most recent), the refined signal
    weights currently in use, and every live prediction made since."""
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
        "backtest_summary": item.get("backtest_summary"),
        "backtest_history": item.get("backtest_history", []),
        "live_predictions": item.get("predictions", []),
    }


@app.get("/api/health")
def health():
    return {"status": "ok", "time": dt.datetime.utcnow().isoformat()}


# ---------- Serve the PWA frontend ----------
frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.isdir(frontend_dir):
    app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")
