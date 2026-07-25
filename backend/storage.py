"""
Watchlist storage, keyed by client IP address.

Two backends, auto-selected:
- If DATABASE_URL is set (a free Postgres from supabase.com or neon.tech),
  everything is stored there and survives Render restarts/redeploys/idle
  wake-ups permanently. This is the recommended setup - see README.md.
- If DATABASE_URL is NOT set, falls back to a local JSON file. Fine for
  local testing, but on Render's free tier this file is wiped on every
  restart (including the automatic spin-down/spin-up after ~15 min idle),
  not just redeploys - if you're seeing your watchlist vanish, this is why,
  and the fix is to set DATABASE_URL.

Everything above this line in both backends is exactly the same JSON shape
(a dict of ip -> list of watchlist item dicts), so every function below
behaves identically regardless of which backend is active - nothing else
in the app needs to know or care which one is in use.

IP-based identification is still a rough proxy for "you" - it changes if
your phone switches from wifi to mobile data, and can't tell two people on
the same wifi/NAT apart. Fine for solo personal use.
"""
import os
import json
import threading
import datetime as dt

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)
STORE_PATH = os.path.join(DATA_DIR, "watchlists.json")

_lock = threading.Lock()
_pg_pool = None

if DATABASE_URL:
    try:
        import psycopg2
        import psycopg2.extras
        from psycopg2 import pool as _pg_pool_module

        _pg_pool = _pg_pool_module.SimpleConnectionPool(1, 5, DATABASE_URL)
        _conn = _pg_pool.getconn()
        try:
            with _conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS watchlist_store (
                        ip TEXT PRIMARY KEY,
                        items JSONB NOT NULL DEFAULT '[]'::jsonb
                    )
                """)
            _conn.commit()
        finally:
            _pg_pool.putconn(_conn)
        print("[info] storage.py: using Postgres backend (DATABASE_URL set) - watchlist will persist permanently.")
    except Exception as e:
        print(f"[warn] Postgres init failed ({e}) - falling back to local JSON file. "
              f"Watchlist will NOT survive Render restarts until this is fixed.")
        _pg_pool = None
else:
    print("[info] storage.py: using local JSON file backend (no DATABASE_URL set) - "
          "watchlist will NOT survive Render restarts/redeploys. Set DATABASE_URL to a "
          "free Postgres (see README.md) to fix this permanently.")


def _pg_load() -> dict:
    conn = _pg_pool.getconn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT ip, items FROM watchlist_store")
            rows = cur.fetchall()
        return {row["ip"]: row["items"] for row in rows}
    finally:
        _pg_pool.putconn(conn)


def _pg_save(data: dict):
    conn = _pg_pool.getconn()
    try:
        with conn.cursor() as cur:
            for ip, items in data.items():
                cur.execute(
                    """
                    INSERT INTO watchlist_store (ip, items) VALUES (%s, %s)
                    ON CONFLICT (ip) DO UPDATE SET items = EXCLUDED.items
                    """,
                    (ip, json.dumps(items)),
                )
        conn.commit()
    finally:
        _pg_pool.putconn(conn)


def _file_load() -> dict:
    if not os.path.exists(STORE_PATH):
        return {}
    try:
        with open(STORE_PATH, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _file_save(data: dict):
    tmp_path = STORE_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, STORE_PATH)  # atomic on POSIX, avoids half-written files


def _load() -> dict:
    return _pg_load() if _pg_pool else _file_load()


def _save(data: dict):
    if _pg_pool:
        _pg_save(data)
    else:
        _file_save(data)


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
            "weights_history": [],
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
                # Seed weights_history from the backtest's own day-by-day
                # weight snapshots, so the trend line has 90 days of history
                # immediately instead of starting empty.
                item["weights_history"] = [
                    {"at": day["date"], "weights": day["weights_used"], "trigger": "backtest"}
                    for day in backtest_history
                ]
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
    long as it stays on the watchlist, not just during the initial backtest.

    Also records whether the predicted DIRECTION (up/down) was correct, and
    appends every weight change to weights_history. Raw % error on a single
    day is dominated by market noise and will never shrink monotonically -
    directional accuracy and the weight trend are the honest signals of
    whether refinement is actually working. See README / detail view."""
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
                        if pred.get("price_at_prediction"):
                            base = pred["price_at_prediction"]
                            actual_dir = 1 if actual > base else (-1 if actual < base else 0)
                            pred_dir = 1 if pred.get("predicted_price", base) > base else (-1 if pred.get("predicted_price", base) < base else 0)
                            pred["correct_direction"] = (actual_dir == pred_dir) if actual_dir != 0 else None
                            if weight_updater_fn and pred.get("raw_signals"):
                                item["signal_weights"] = weight_updater_fn(
                                    item.get("signal_weights", {}), pred["raw_signals"], actual_dir
                                )
                                item.setdefault("weights_history", []).append({
                                    "at": now_iso,
                                    "weights": dict(item["signal_weights"]),
                                    "trigger": "live_prediction",
                                })
                                item["weights_history"] = item["weights_history"][-100:]
    _with_store(mutate)
