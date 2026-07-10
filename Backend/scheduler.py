"""
Background job: on each tick during market hours -
  1. Resolve any past predictions whose target time has passed (record the
     actual price, compute error %) - across every IP's watchlist.
  2. Make a fresh prediction for every watchlist item, for every IP.
Runs via APScheduler inside the same process as the FastAPI app.
"""
import datetime as dt
import pytz
from apscheduler.schedulers.background import BackgroundScheduler

import storage
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


def _make_fresh_prediction(ip: str, item: dict):
    price = fetch_latest_price(item["symbol"])
    if price is None:
        return
    tf_data = fetch_multi_timeframe(item["symbol"])
    company_name = item.get("display_name") or item["symbol"]
    news = fetch_company_news(company_name)
    weather = fetch_weather_signal()

    result = predict_price(
        timeframe_data=tf_data,
        current_price=price,
        horizon_minutes=item["horizon_minutes"],
        news_articles=news,
        weather_json=weather,
    )

    now = dt.datetime.utcnow()
    prediction = {
        "made_at": now.isoformat(),
        "target_at": (now + dt.timedelta(minutes=item["horizon_minutes"])).isoformat(),
        "price_at_prediction": price,
        "predicted_price": result["predicted_price"],
        "predicted_low": result["band_68"][0],
        "predicted_high": result["band_68"][1],
        "confidence": result["confidence"],
        "actual_price": None,
        "resolved": False,
        "error_pct": None,
    }
    storage.append_prediction(ip, item["id"], prediction)


def tick():
    if not is_market_hours():
        return

    now_iso = dt.datetime.utcnow().isoformat()
    storage.resolve_due_predictions(now_iso, fetch_latest_price)

    for ip, item in storage.all_items():
        _make_fresh_prediction(ip, item)


scheduler = BackgroundScheduler(timezone=str(IST))


def start_scheduler(interval_minutes: int = 5):
    scheduler.add_job(tick, "interval", minutes=interval_minutes, id="watchlist_tick", replace_existing=True)
    scheduler.start()
