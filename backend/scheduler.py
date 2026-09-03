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
from data_sources import fetch_multi_timeframe, fetch_latest_price, fetch_company_news, fetch_weather_signal, fetch_intraday_bars
from predictor import predict_price, nudge_weights
import intraday

import config
import market_data
import market_store
import paper_trading
import predictions as prediction_analytics
import universe
from ml import registry as ml_registry

IST = pytz.timezone("Asia/Kolkata")


def is_market_hours() -> bool:
    now = dt.datetime.now(IST)
    if now.weekday() >= 5:  # Sat/Sun
        return False
    open_t = now.replace(hour=9, minute=15, second=0, microsecond=0)
    close_t = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return open_t <= now <= close_t


def make_fresh_prediction(ip: str, item: dict):
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
        weights=item.get("signal_weights"),
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
        "raw_signals": result["signals"],
    }
    storage.append_prediction(ip, item["id"], prediction)


def tick():
    # Resolution runs every tick, every day - a target time passing on a
    # Saturday still deserves to be resolved using the last close price,
    # not left queued until Monday's market open. This is what actually
    # drives weight refinement, so gating it behind market hours meant
    # nothing learned at all across any weekend.
    now_iso = dt.datetime.utcnow().isoformat()
    storage.resolve_due_predictions(now_iso, fetch_latest_price, nudge_weights)

    # New predictions still only get made while the market's actually open -
    # no point predicting off a stale, closed-market quote.
    if is_market_hours():
        for ip, item in storage.all_items():
            make_fresh_prediction(ip, item)


def intraday_tick():
    """Runs every tick, market hours only - a paper trade decided off a
    closed-market quote wouldn't mean anything. Re-evaluates all 3
    timeframes for every tracked stock so they can be compared head to head
    under identical rules; the only difference between them is which
    interval's bars they're reacting to."""
    if not is_market_hours():
        return
    for ip, stock in storage.all_intraday_targets():
        symbol = stock["symbol"]
        for tf in intraday.TIMEFRAMES:
            try:
                bars = fetch_intraday_bars(symbol, tf)
                signal = intraday.compute_signal(bars)
                if signal is None:
                    continue
                portfolio = storage.get_intraday_portfolio(ip, symbol, tf) or intraday.default_portfolio()
                portfolio = intraday.step(portfolio, signal, tf)
                portfolio["last_price"] = signal["last_close"]
                portfolio["last_score"] = signal["score"]
                portfolio["last_updated"] = dt.datetime.utcnow().isoformat()
                storage.save_intraday_portfolio(ip, symbol, tf, portfolio)
            except Exception as e:
                print(f"[warn] intraday tick failed for {symbol} {tf}: {e}")


# ---------------------------------------------------------------------------
# Urban Spork platform jobs
# ---------------------------------------------------------------------------

def _tracked_symbols(limit: int = 40) -> list[tuple[str, str]]:
    """Everything worth collecting data for: open paper positions, recently
    predicted stocks, and the legacy watchlist. Collecting the entire 2,000+
    stock universe every five minutes would be pointless and would get the
    data source to rate-limit us within the hour."""
    seen, out = set(), []

    def add(symbol, exchange):
        key = (symbol.upper(), (exchange or "NSE").upper())
        if key not in seen:
            seen.add(key)
            out.append(key)

    try:
        for trade in market_store.open_trades_all():
            add(trade["symbol"], trade["exchange"])
    except Exception as e:
        print(f"[warn] collector could not read open trades: {e}")
    try:
        for pred in market_store.list_predictions(limit=80):
            add(pred["symbol"], pred["exchange"])
    except Exception as e:
        print(f"[warn] collector could not read predictions: {e}")
    try:
        for _ip, item in storage.all_items():
            symbol = item["symbol"]
            exchange = "BSE" if symbol.upper().endswith(".BO") else "NSE"
            add(symbol.replace(".NS", "").replace(".BO", ""), exchange)
    except Exception as e:
        print(f"[warn] collector could not read watchlist: {e}")

    return out[:limit]


def collector_tick():
    """Continuously grow the local historical database. Runs during market
    hours (that's when new bars exist) and NEVER deletes anything - the whole
    point is that the dataset survives the close, the weekend and restarts,
    so there is eventually enough history to train on."""
    if not is_market_hours():
        return
    for symbol, exchange in _tracked_symbols():
        try:
            market_data.collect_history(symbol, exchange, ["1m", "5m", "15m"])
        except Exception as e:
            print(f"[warn] collector failed for {symbol}: {e}")


def outcome_tick():
    """Grade due predictions and mark paper trades to market. Runs every
    tick, every day - a horizon that expired on Friday evening should not
    wait until Monday to be graded, or nothing learns over a weekend."""
    try:
        result = prediction_analytics.resolve_due()
        if result.get("resolved"):
            print(f"[info] resolved {result['resolved']} prediction(s)")
    except Exception as e:
        print(f"[warn] prediction resolution failed: {e}")
    try:
        paper_trading.mark_to_market()
    except Exception as e:
        print(f"[warn] paper mark-to-market failed: {e}")


def universe_tick():
    try:
        universe.refresh_universe()
    except Exception as e:
        print(f"[warn] universe refresh failed: {e}")


def training_tick():
    """Retrain models on a slow cadence - daily, after the close.

    Not after every trade, and not every tick: a model retrained on each new
    outcome chases the last hour of noise, which is the classic way to build
    something that backtests beautifully and loses money live."""
    try:
        reports = ml_registry.train_all_tracked(timeframes=["5m", "15m"], limit=10)
        usable = sum(1 for r in reports if r.get("usable"))
        print(f"[info] nightly training: {len(reports)} attempted, {usable} accepted as usable")
    except Exception as e:
        print(f"[warn] nightly training failed: {e}")


scheduler = BackgroundScheduler(timezone=str(IST))


def start_scheduler(interval_minutes: int = 5):
    scheduler.add_job(tick, "interval", minutes=interval_minutes, id="watchlist_tick", replace_existing=True)
    scheduler.add_job(intraday_tick, "interval", minutes=interval_minutes, id="intraday_tick", replace_existing=True)
    scheduler.add_job(collector_tick, "interval", minutes=config.COLLECTOR_MINUTES,
                      id="collector_tick", replace_existing=True)
    scheduler.add_job(outcome_tick, "interval", minutes=interval_minutes,
                      id="outcome_tick", replace_existing=True)
    scheduler.add_job(universe_tick, "interval", hours=max(1, int(config.UNIVERSE_REFRESH_HOURS)),
                      id="universe_tick", replace_existing=True)
    # 16:15 IST - after the 15:30 close, so the day's bars are complete.
    scheduler.add_job(training_tick, "cron", hour=16, minute=15,
                      id="training_tick", replace_existing=True)
    scheduler.start()
