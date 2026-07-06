"""
Background job: for every watchlist item, on each tick during market hours -
  1. Resolve any past predictions whose target time has passed (record actual
     price, compute error %).
  2. Make a fresh prediction for the configured horizon.
Runs via APScheduler inside the same process as the FastAPI app (fine for a
single free-tier instance; note free tiers may sleep after inactivity, which
pauses this loop until a request wakes it again).
"""
import datetime as dt
import pytz
from apscheduler.schedulers.background import BackgroundScheduler

from db import SessionLocal, WatchlistItem, PredictionRecord
from data_sources import fetch_multi_timeframe, fetch_latest_price, fetch_company_news, fetch_weather_signal
from predictor import predict_price

IST = pytz.timezone("Asia/Kolkata")


def is_market_hours() -> bool:
    now = dt.datetime.now(IST)
    if now.weekday() >= 5:  # Sat/Sun
        return False
    open_t = now.replace(hour=9, minute=15, second=0, microsecond=0)
    close_t = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return open_t <= now <= close_t


def _resolve_due_predictions(db):
    now = dt.datetime.utcnow()
    due = db.query(PredictionRecord).filter(
        PredictionRecord.resolved == 0,
        PredictionRecord.target_at <= now,
    ).all()
    for pred in due:
        actual = fetch_latest_price(pred.symbol)
        if actual is None:
            continue
        pred.actual_price = actual
        pred.error_pct = round(
            (actual - pred.predicted_price) / pred.predicted_price * 100, 3
        ) if pred.predicted_price else None
        pred.resolved = 1
    db.commit()


def _make_fresh_prediction(db, item: WatchlistItem):
    price = fetch_latest_price(item.symbol)
    if price is None:
        return
    tf_data = fetch_multi_timeframe(item.symbol)
    company_name = item.display_name or item.symbol
    news = fetch_company_news(company_name)
    weather = fetch_weather_signal()

    result = predict_price(
        timeframe_data=tf_data,
        current_price=price,
        horizon_minutes=item.horizon_minutes,
        news_articles=news,
        weather_json=weather,
    )

    record = PredictionRecord(
        watchlist_id=item.id,
        symbol=item.symbol,
        made_at=dt.datetime.utcnow(),
        target_at=dt.datetime.utcnow() + dt.timedelta(minutes=item.horizon_minutes),
        price_at_prediction=price,
        predicted_price=result["predicted_price"],
        predicted_low=result["band_68"][0],
        predicted_high=result["band_68"][1],
        confidence=result["confidence"],
    )
    db.add(record)
    db.commit()


def tick():
    if not is_market_hours():
        return
    db = SessionLocal()
    try:
        _resolve_due_predictions(db)
        items = db.query(WatchlistItem).all()
        for item in items:
            _make_fresh_prediction(db, item)
    finally:
        db.close()


scheduler = BackgroundScheduler(timezone=str(IST))


def start_scheduler(interval_minutes: int = 5):
    scheduler.add_job(tick, "interval", minutes=interval_minutes, id="watchlist_tick", replace_existing=True)
    scheduler.start()
