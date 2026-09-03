import os
import json
import threading
import datetime as dt
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import storage
from data_sources import (
    to_yf_symbol, fetch_multi_timeframe,
    fetch_latest_price, fetch_company_news, fetch_weather_signal,
    fetch_intraday_bars, fetch_daily_history,
)
from predictor import predict_price, gbm_path
from backtest import run_backtest_and_refine
from scheduler import start_scheduler, make_fresh_prediction
import intraday

# --- Urban Spork platform modules ---
import config
import market_store
import market_data
import market_overview
import intraday_engine
import paper_trading
import predictions as prediction_analytics
import providers
import risk
import universe
from ml import registry as ml_registry

app = FastAPI(title="Urban Spork - NSE/BSE analysis, intraday & prediction platform")

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
    # Order matters: the database and the stock universe must exist before
    # any scheduled job or request can touch them.
    market_store.init_db()
    universe.ensure_seeded()
    start_scheduler(interval_minutes=config.TICK_MINUTES)

    def _refresh_universe():
        """Pull the live NSE/BSE lists in the background. Startup must never
        block on a third-party site being slow or down - the bundled seed
        already makes search work."""
        try:
            result = universe.refresh_universe()
            print(f"[info] universe refresh: {result}")
        except Exception as e:
            print(f"[warn] universe refresh failed at startup: {e}")

    threading.Thread(target=_refresh_universe, name="universe-refresh", daemon=True).start()


# ---------- Stock search ----------

@app.get("/api/stocks/search")
def api_search_stocks(q: str, limit: int = 20, exchange: str | None = None):
    """Fuzzy search across the full NSE + BSE universe held locally, so it
    works with the market shut and with NSE/BSE unreachable."""
    results = universe.search(q, limit=limit, exchange=exchange)
    for r in results:
        r["yf_symbol"] = to_yf_symbol(r["symbol"], r["exchange"])
    return {"query": q, "results": results, "universe_size": market_store.stock_master_count()}


@app.get("/api/universe/status")
def api_universe_status():
    return universe.universe_status()


@app.post("/api/universe/refresh")
def api_universe_refresh(force: bool = False):
    return universe.refresh_universe(force=force)


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


@app.get("/api/stocks/{symbol}/chart")
def api_stock_chart(symbol: str, exchange: str = "NSE", horizon: str = "1d", weights: str | None = None, yf_symbol_override: str | None = None):
    """Historical daily OHLC for the candlestick, plus a forecast path from
    now to the horizon target - sampled at several points using the exact
    same GBM formula as the point prediction (not a separate model), so the
    shaded band on the chart is a faithful picture of what /analyze already
    computed, just spread across time instead of collapsed to one number.
    `weights` (optional) is a JSON string of per-signal weights, used when
    charting from the watchlist so the chart reflects that stock's refined
    weights instead of the generic defaults. `yf_symbol_override` lets a
    caller that already has the exact Yahoo symbol (e.g. the watchlist,
    which stores it directly) skip the symbol+exchange reconstruction."""
    if horizon not in HORIZON_PRESETS:
        raise HTTPException(400, f"horizon must be one of {list(HORIZON_PRESETS)}")
    horizon_minutes = HORIZON_PRESETS[horizon]

    yf_symbol = yf_symbol_override or to_yf_symbol(symbol, exchange)
    price = fetch_latest_price(yf_symbol)
    if price is None:
        raise HTTPException(502, f"Could not fetch live price for {yf_symbol} right now.")

    daily = fetch_daily_history(yf_symbol, period="1y")
    if daily is None or daily.empty:
        raise HTTPException(502, f"Could not fetch price history for {yf_symbol} right now.")
    daily = daily.tail(180)
    historical = [
        {
            "time": int(idx.timestamp()),
            "open": round(float(row["Open"]), 2),
            "high": round(float(row["High"]), 2),
            "low": round(float(row["Low"]), 2),
            "close": round(float(row["Close"]), 2),
        }
        for idx, row in daily.iterrows()
    ]

    tf_data = fetch_multi_timeframe(yf_symbol)
    news = fetch_company_news(symbol)
    weather = fetch_weather_signal()
    parsed_weights = None
    if weights:
        try:
            parsed_weights = json.loads(weights)
        except (json.JSONDecodeError, TypeError):
            parsed_weights = None

    result = predict_price(
        timeframe_data=tf_data,
        current_price=price,
        horizon_minutes=horizon_minutes,
        news_articles=news,
        weather_json=weather,
        weights=parsed_weights,
    )
    forecast = gbm_path(price, result["mu_annualized"], result["sigma_annualized"], horizon_minutes)

    return {
        "symbol": yf_symbol,
        "current_price": price,
        "historical": historical,
        "forecast": forecast,
        "predicted_price": result["predicted_price"],
        "confidence": result["confidence"],
    }


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
        today_bars = intraday._today_bars(bars) if bars is not None and not bars.empty else bars
        bar_points = [
            {
                "time": int(idx.timestamp()),
                "open": round(float(row["Open"]), 2),
                "high": round(float(row["High"]), 2),
                "low": round(float(row["Low"]), 2),
                "close": round(float(row["Close"]), 2),
            }
            for idx, row in today_bars.iterrows()
        ] if today_bars is not None and not today_bars.empty else []
        timeframes[tf] = {
            "summary": intraday.portfolio_summary(portfolio, live_price),
            "current_signal": signal,
            "trade_log": list(reversed(portfolio.get("trade_log", [])))[:50],
            "bars": bar_points,
        }
    if not timeframes:
        raise HTTPException(404, "not found")
    return {"symbol": symbol, "live_price": live_price, "timeframes": timeframes}




# ===========================================================================
# Urban Spork platform API
#
# Everything below is the newer engine set: full-universe search (above),
# the intraday recommendation engine, risk settings, paper trading,
# prediction history, market overview, the ML registry and the data
# collector. The endpoints above this line are the original analysis and
# watchlist screens, kept working unchanged.
# ===========================================================================

# ---------- Timeframes & platform status ----------

@app.get("/api/timeframes")
def api_timeframes():
    return {
        "timeframes": market_data.timeframe_catalog(),
        "note": ("30s runs on 1-minute bars: no free provider serves sub-minute Indian "
                 "equity data, and the platform flags approximations rather than "
                 "pretending to a resolution it does not have."),
    }


@app.get("/api/platform/status")
def api_platform_status():
    return {
        "providers": providers.provider_status(),
        "active_provider": providers.get_provider().name,
        "universe": universe.universe_status(),
        "historical_data": market_store.bar_stats(),
        "ml_backends": ml_registry.available_backends(),
        "models_trained": len(ml_registry.model_status()),
        "min_trade_confidence": config.MIN_TRADE_CONFIDENCE,
    }


# ---------- Intraday recommendation engine ----------

@app.get("/api/intraday/analyze")
def api_intraday_analyze(symbol: str, exchange: str = "NSE", timeframe: str = "5m",
                         record: bool = True, request: Request = None):
    client_id = get_client_ip(request) if request else "default"
    result = intraday_engine.analyze(symbol, exchange, timeframe,
                                     client_id=client_id, record=record)
    if not result.get("ok", True):
        raise HTTPException(400, result.get("error", "analysis failed"))
    return result


class IntradayScanRequest(BaseModel):
    symbols: list[str]
    exchange: str = "NSE"
    timeframe: str = "5m"


@app.post("/api/intraday/scan")
def api_intraday_scan(req: IntradayScanRequest, request: Request):
    client_id = get_client_ip(request)
    if len(req.symbols) > 25:
        raise HTTPException(400, "scan is limited to 25 symbols per request")
    pairs = [(s, req.exchange) for s in req.symbols]
    return {"timeframe": req.timeframe,
            "results": intraday_engine.scan(pairs, req.timeframe, client_id=client_id)}


# ---------- Risk settings ----------

class RiskSettingsRequest(BaseModel):
    capital: float | None = None
    risk_per_trade_pct: float | None = None
    max_daily_loss_pct: float | None = None
    min_risk_reward: float | None = None
    min_confidence: float | None = None
    max_open_positions: int | None = None


@app.get("/api/settings")
def api_get_settings(request: Request):
    client_id = get_client_ip(request)
    return {"settings": risk.get_settings(client_id),
            "defaults": risk.DEFAULT_SETTINGS,
            "daily_loss_status": risk.daily_loss_status(client_id)}


@app.post("/api/settings")
def api_save_settings(req: RiskSettingsRequest, request: Request):
    client_id = get_client_ip(request)
    return {"settings": risk.save_settings(client_id, req.model_dump(exclude_none=True))}


# ---------- Paper trading ----------

class PaperOpenRequest(BaseModel):
    symbol: str
    exchange: str = "NSE"
    timeframe: str = "5m"


class PaperCloseRequest(BaseModel):
    price: float | None = None


@app.get("/api/paper/positions")
def api_paper_positions(request: Request):
    client_id = get_client_ip(request)
    return {
        "open": paper_trading.open_positions(client_id),
        "closed": market_store.list_trades(client_id, status="CLOSED", limit=100),
        "summary": paper_trading.summary(client_id),
    }


@app.post("/api/paper/open")
def api_paper_open(req: PaperOpenRequest, request: Request):
    """Take the engine's current recommendation for this stock/timeframe and
    open it as a paper trade. Deliberately re-runs the analysis rather than
    trusting numbers posted by the client - the trade is opened at the price
    and plan the engine stands behind right now."""
    client_id = get_client_ip(request)
    analysis = intraday_engine.analyze(req.symbol, req.exchange, req.timeframe,
                                       client_id=client_id, record=True)
    result = paper_trading.open_from_plan(client_id, analysis)
    if not result.get("ok"):
        raise HTTPException(400, result.get("reason", "could not open paper trade"))
    return {**result, "recommendation": analysis.get("recommendation"),
            "confidence": analysis.get("confidence")}


@app.post("/api/paper/close/{trade_id}")
def api_paper_close(trade_id: int, req: PaperCloseRequest, request: Request):
    client_id = get_client_ip(request)
    trade = market_store.get_trade(trade_id)
    if not trade or trade["client_id"] != client_id:
        raise HTTPException(404, "trade not found")
    price = req.price or market_data.get_quote(trade["symbol"], trade["exchange"])
    if not price:
        raise HTTPException(502, "no price available to close this trade at")
    result = paper_trading.close_trade(trade_id, float(price), "manual")
    if not result.get("ok"):
        raise HTTPException(400, result.get("reason"))
    return result


@app.post("/api/paper/mark")
def api_paper_mark(request: Request):
    client_id = get_client_ip(request)
    return paper_trading.mark_to_market(client_id)


# ---------- Prediction history & accuracy ----------

@app.get("/api/predictions")
def api_predictions(request: Request, symbol: str | None = None,
                    timeframe: str | None = None, limit: int = 200):
    client_id = get_client_ip(request)
    return {"predictions": prediction_analytics.history(client_id, symbol, timeframe, limit)}


@app.get("/api/predictions/accuracy")
def api_prediction_accuracy(request: Request, scope: str = "mine"):
    client_id = None if scope == "all" else get_client_ip(request)
    return prediction_analytics.accuracy(client_id)


@app.post("/api/predictions/resolve")
def api_resolve_predictions():
    """Grade every prediction whose horizon has passed. The scheduler does
    this automatically; this endpoint is for forcing it on demand."""
    return prediction_analytics.resolve_due()


# ---------- Market overview ----------

@app.get("/api/market/overview")
def api_market_overview(force: bool = False):
    return market_overview.build_overview(force=force)


# ---------- ML models ----------

@app.get("/api/models")
def api_models(symbol: str | None = None):
    return {"backends": ml_registry.available_backends(),
            "models": ml_registry.model_status(symbol)}


class TrainRequest(BaseModel):
    symbol: str
    exchange: str = "NSE"
    timeframe: str = "5m"


@app.post("/api/models/train")
def api_train_model(req: TrainRequest):
    """Train and validate a model for one stock/timeframe. Returns the
    validation report whether or not the model was accepted - a model that
    fails to beat its baseline is stored as unusable and ignored by the
    ensemble, which is reported here rather than hidden."""
    return ml_registry.train_symbol(req.symbol, req.exchange, req.timeframe)


# ---------- Historical data collection ----------

@app.get("/api/data/status")
def api_data_status():
    return market_store.bar_stats()


class CollectRequest(BaseModel):
    symbol: str
    exchange: str = "NSE"
    timeframes: list[str] | None = None


@app.post("/api/data/collect")
def api_collect_data(req: CollectRequest):
    """Back-fill and store bars for a symbol. The scheduler also does this
    continuously for everything you track, so the local history grows on its
    own - this is for pulling a new stock's history in immediately."""
    return market_data.collect_history(req.symbol, req.exchange, req.timeframes)
# ---------- Serve the PWA frontend ----------
frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.isdir(frontend_dir):
    app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")
