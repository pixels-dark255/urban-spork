"""
Simple JSON-file-based watchlist storage, keyed by client IP address.

This deliberately avoids a real database, per request - fine for a personal,
low-traffic app. Two caveats worth knowing:

1. Render's free tier wipes local disk on every restart - including the
   automatic spin-down/spin-up after ~15 min of inactivity, not just on
   redeploys. This file will NOT survive a long idle period any better than
   SQLite did. If that keeps happening, the only real fix on a free plan is
   external storage (a free Postgres from supabase.com or neon.tech) - the
   code here is written so swapping to that later is a small change, not a
   rewrite.
2. IP-based identification is a rough proxy for "you" - it changes if your
   phone switches from wifi to mobile data, and can't tell two people on the
   same wifi/NAT apart. Fine for solo personal use.
"""
import os
import json
import threading
import datetime as dt

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)
STORE_PATH = os.path.join(DATA_DIR, "watchlists.json")

_lock = threading.Lock()


def _load() -> dict:
    if not os.path.exists(STORE_PATH):
        return {}
    try:
        with open(STORE_PATH, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict):
    tmp_path = STORE_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, STORE_PATH)  # atomic on POSIX, avoids half-written files


def _with_store(mutator):
    """Load once, let mutator(data) read/modify it, save once. mutator's
    return value is passed back to the caller."""
    with _lock:
        data = _load()
        result = mutator(data)
        _save(data)
        return result


def get_watchlist(ip: str) -> list[dict]:
    with _lock:
        data = _load()
        return data.get(ip, [])


def get_item(ip: str, item_id: int) -> dict | None:
    for item in get_watchlist(ip):
        if item["id"] == item_id:
            return item
    return None


def add_item(ip: str, symbol: str, display_name: str, horizon_minutes: int) -> dict:
    def mutate(data):
        items = data.setdefault(ip, [])
        next_id = (max((i["id"] for i in items), default=0)) + 1
        new_item = {
            "id": next_id,
            "symbol": symbol,
            "display_name": display_name,
            "horizon_minutes": horizon_minutes,
            "created_at": dt.datetime.utcnow().isoformat(),
            "predictions": [],
            "signal_weights": {"trend": 1.0, "momentum": 1.0, "news": 1.0, "seasonality": 1.0, "weather": 1.0},
            "backtest_history": [],
            "backtest_summary": None,
        }
        items.append(new_item)
        return new_item
    return _with_store(mutate)


def set_backtest_result(ip: str, item_id: int, backtest_history: list, refined_weights: dict, summary: dict | None):
    """Stores the one-time 90-day backtest result and its refined starting
    weights for this stock (see backtest.py)."""
    def mutate(data):
        for item in data.get(ip, []):
            if item["id"] == item_id:
                item["backtest_history"] = backtest_history
                item["signal_weights"] = refined_weights
                item["backtest_summary"] = summary
                break
    _with_store(mutate)


def remove_item(ip: str, item_id: int) -> bool:
    def mutate(data):
        items = data.get(ip, [])
        before = len(items)
        data[ip] = [i for i in items if i["id"] != item_id]
        return len(data[ip]) != before
    return _with_store(mutate)


def all_items() -> list[tuple]:
    """Flat list of (ip, item) across every IP - used by the background scheduler."""
    with _lock:
        data = _load()
        return [(ip, item) for ip, items in data.items() for item in items]


def append_prediction(ip: str, item_id: int, prediction: dict):
    def mutate(data):
        for item in data.get(ip, []):
            if item["id"] == item_id:
                item.setdefault("predictions", []).append(prediction)
                break
    _with_store(mutate)


def resolve_due_predictions(now_iso: str, resolver_fn, weight_updater_fn=None):
    """resolver_fn(symbol) -> actual_price or None.
    weight_updater_fn(weights, raw_signals, actual_direction) -> updated weights
    (pass predictor.nudge_weights) - keeps a stock's weights refining for as
    long as it stays on the watchlist, not just during the initial backtest."""
    def mutate(data):
        for ip, items in data.items():
            for item in items:
                for pred in item.get("predictions", []):
                    if not pred.get("resolved") and pred["target_at"] <= now_iso:
                        actual = resolver_fn(item["symbol"])
                        if actual is None:
                            continue
                        pred["actual_price"] = actual
                        pred["resolved"] = True
                        if pred.get("predicted_price"):
                            pred["error_pct"] = round(
                                (actual - pred["predicted_price"]) / pred["predicted_price"] * 100, 3
                            )
                        if weight_updater_fn and pred.get("raw_signals") and pred.get("price_at_prediction"):
                            base = pred["price_at_prediction"]
                            direction = 1 if actual > base else (-1 if actual < base else 0)
                            item["signal_weights"] = weight_updater_fn(
                                item.get("signal_weights", {}), pred["raw_signals"], direction
                            )
    _with_store(mutate)
