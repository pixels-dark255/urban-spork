import os
import datetime as dt
from fastapi import FastAPI, HTTPException, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session

from db import init_db, get_db, WatchlistItem, PredictionRecord
from data_sources import (
    search_stocks, to_yf_symbol, fetch_multi_timeframe,
    fetch_latest_price, fetch_company_news, fetch_weather_signal,
)
from predictor import predict_price
from scheduler import start_scheduler

app = FastAPI(title="NSE/BSE Stock Analyzer & Predictor")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup():
    init_db()
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
        raise HTTPException(404, f"Could not fetch live price for {yf_symbol}. Check the symbol.")

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


# ---------- Watchlist ----------

class WatchlistAddRequest(BaseModel):
    symbol: str
    exchange: str = "NSE"
    display_name: str | None = None
    horizon: str = "1d"


@app.get("/api/watchlist")
def api_get_watchlist(db: Session = Depends(get_db)):
    items = db.query(WatchlistItem).all()
    out = []
    for item in items:
        latest = (
            db.query(PredictionRecord)
            .filter(PredictionRecord.watchlist_id == item.id)
            .order_by(PredictionRecord.made_at.desc())
            .first()
        )
        resolved = (
            db.query(PredictionRecord)
            .filter(PredictionRecord.watchlist_id == item.id, PredictionRecord.resolved == 1)
            .all()
        )
        avg_abs_error = None
        if resolved:
            errs = [abs(p.error_pct) for p in resolved if p.error_pct is not None]
            if errs:
                avg_abs_error = round(sum(errs) / len(errs), 3)

        out.append({
            "id": item.id,
            "symbol": item.symbol,
            "display_name": item.display_name,
            "horizon_minutes": item.horizon_minutes,
            "latest_prediction": {
                "made_at": latest.made_at.isoformat() if latest else None,
                "target_at": latest.target_at.isoformat() if latest else None,
                "price_at_prediction": latest.price_at_prediction if latest else None,
                "predicted_price": latest.predicted_price if latest else None,
                "confidence": latest.confidence if latest else None,
            } if latest else None,
            "track_record": {
                "resolved_count": len(resolved),
                "avg_abs_error_pct": avg_abs_error,
            },
        })
    return {"watchlist": out}


@app.post("/api/watchlist")
def api_add_watchlist(req: WatchlistAddRequest, db: Session = Depends(get_db)):
    if req.horizon not in HORIZON_PRESETS:
        raise HTTPException(400, f"horizon must be one of {list(HORIZON_PRESETS)}")
    yf_symbol = to_yf_symbol(req.symbol, req.exchange)
    item = WatchlistItem(
        symbol=yf_symbol,
        display_name=req.display_name or req.symbol,
        horizon_minutes=HORIZON_PRESETS[req.horizon],
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return {"id": item.id, "symbol": item.symbol}


@app.delete("/api/watchlist/{item_id}")
def api_remove_watchlist(item_id: int, db: Session = Depends(get_db)):
    item = db.query(WatchlistItem).filter(WatchlistItem.id == item_id).first()
    if not item:
        raise HTTPException(404, "not found")
    db.delete(item)
    db.commit()
    return {"deleted": item_id}


@app.get("/api/watchlist/{item_id}/history")
def api_watchlist_history(item_id: int, db: Session = Depends(get_db)):
    records = (
        db.query(PredictionRecord)
        .filter(PredictionRecord.watchlist_id == item_id)
        .order_by(PredictionRecord.made_at.asc())
        .all()
    )
    return {
        "history": [
            {
                "made_at": r.made_at.isoformat(),
                "target_at": r.target_at.isoformat(),
                "price_at_prediction": r.price_at_prediction,
                "predicted_price": r.predicted_price,
                "actual_price": r.actual_price,
                "resolved": bool(r.resolved),
                "error_pct": r.error_pct,
            }
            for r in records
        ]
    }


@app.get("/api/health")
def health():
    return {"status": "ok", "time": dt.datetime.utcnow().isoformat()}


# ---------- Serve the PWA frontend ----------
frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.isdir(frontend_dir):
    app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")
