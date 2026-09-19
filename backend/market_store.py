"""
Persistent local database - the memory of the whole platform.

Everything that must survive market close, a restart or a redeploy lives
here: collected OHLCV bars, the NSE/BSE stock master, every prediction the
engine has ever made and how it turned out, paper trades, per-regime model
performance, and user risk settings.

SQLite by default (a single file, zero setup). The schema is deliberately
plain SQL with no ORM so moving to PostgreSQL later is a driver swap plus a
handful of parameter-style tweaks, not a rewrite.

Design rules:
 - Bars are UPSERTed on (symbol, exchange, timeframe, ts) so re-collecting an
   overlapping window is idempotent and never duplicates history.
 - NOTHING here is ever truncated on a schedule. The database only grows;
   that growing history is what later makes ML training possible.
 - Every connection is short-lived and opened per call, which keeps this
   safe to use from FastAPI request threads and the background scheduler at
   the same time.
"""
import json
import sqlite3
import threading
import datetime as dt
from contextlib import contextmanager

import config

_init_lock = threading.Lock()
_initialised = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    symbol      TEXT NOT NULL,
    exchange    TEXT NOT NULL,
    timeframe   TEXT NOT NULL,
    ts          INTEGER NOT NULL,          -- epoch seconds, UTC
    open        REAL NOT NULL,
    high        REAL NOT NULL,
    low         REAL NOT NULL,
    close       REAL NOT NULL,
    volume      REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (symbol, exchange, timeframe, ts)
);
CREATE INDEX IF NOT EXISTS idx_bars_lookup ON bars (symbol, timeframe, ts);

CREATE TABLE IF NOT EXISTS stock_master (
    symbol      TEXT NOT NULL,
    exchange    TEXT NOT NULL,
    name        TEXT NOT NULL,
    isin        TEXT,
    sector      TEXT,
    industry    TEXT,
    bse_code    TEXT,
    updated_at  TEXT,
    PRIMARY KEY (symbol, exchange)
);
CREATE INDEX IF NOT EXISTS idx_master_name ON stock_master (name);

CREATE TABLE IF NOT EXISTS predictions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id         TEXT NOT NULL DEFAULT 'default',
    symbol            TEXT NOT NULL,
    exchange          TEXT NOT NULL,
    display_name      TEXT,
    timeframe         TEXT NOT NULL,
    kind              TEXT NOT NULL DEFAULT 'intraday',   -- intraday | analysis
    made_at           TEXT NOT NULL,
    horizon_minutes   REAL NOT NULL,
    target_at         TEXT NOT NULL,
    recommendation    TEXT NOT NULL,
    direction         INTEGER NOT NULL DEFAULT 0,          -- +1 long, -1 short, 0 none
    entry_price       REAL NOT NULL,
    stop_loss         REAL,
    target_price      REAL,
    position_size     INTEGER,
    risk_amount       REAL,
    reward_amount     REAL,
    risk_reward       REAL,
    confidence        REAL NOT NULL,
    market_regime     TEXT,
    indicators_json   TEXT,
    components_json   TEXT,
    resolved          INTEGER NOT NULL DEFAULT 0,
    resolved_at       TEXT,
    actual_price      REAL,
    move_pct          REAL,
    correct_direction INTEGER,
    outcome           TEXT                                  -- target | stop | expired | flat
);
CREATE INDEX IF NOT EXISTS idx_pred_open ON predictions (resolved, target_at);
CREATE INDEX IF NOT EXISTS idx_pred_client ON predictions (client_id, made_at);

CREATE TABLE IF NOT EXISTS paper_trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id       TEXT NOT NULL DEFAULT 'default',
    prediction_id   INTEGER,
    symbol          TEXT NOT NULL,
    exchange        TEXT NOT NULL,
    display_name    TEXT,
    timeframe       TEXT NOT NULL,
    side            TEXT NOT NULL,              -- BUY | SELL (short)
    quantity        INTEGER NOT NULL,
    entry_price     REAL NOT NULL,
    stop_loss       REAL,
    target_price    REAL,
    opened_at       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'OPEN',   -- OPEN | CLOSED
    closed_at       TEXT,
    exit_price      REAL,
    exit_reason     TEXT,                        -- target | stop | manual | eod
    pnl             REAL,
    pnl_pct         REAL,
    confidence      REAL,
    market_regime   TEXT,
    last_price      REAL
);
CREATE INDEX IF NOT EXISTS idx_trades_client ON paper_trades (client_id, status);

CREATE TABLE IF NOT EXISTS model_performance (
    component   TEXT NOT NULL,
    regime      TEXT NOT NULL,
    timeframe   TEXT NOT NULL,
    hits        INTEGER NOT NULL DEFAULT 0,
    misses      INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT,
    PRIMARY KEY (component, regime, timeframe)
);

CREATE TABLE IF NOT EXISTS settings (
    client_id   TEXT PRIMARY KEY,
    data        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS broker_orders (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id         TEXT NOT NULL DEFAULT 'default',
    prediction_id     INTEGER,
    reference_id      TEXT UNIQUE,              -- our idempotency key
    broker            TEXT NOT NULL,
    broker_order_id   TEXT,
    symbol            TEXT NOT NULL,
    exchange          TEXT NOT NULL,
    segment           TEXT NOT NULL DEFAULT 'CASH',
    product           TEXT NOT NULL DEFAULT 'MIS',
    side              TEXT NOT NULL,             -- BUY | SELL
    order_type        TEXT NOT NULL,             -- LIMIT | MARKET | SL | SL_M
    quantity          INTEGER NOT NULL,
    price             REAL,
    trigger_price     REAL,
    intent            TEXT NOT NULL DEFAULT 'ENTRY',   -- ENTRY | EXIT | SQUARE_OFF
    status            TEXT NOT NULL DEFAULT 'NEW',
    filled_quantity   INTEGER DEFAULT 0,
    average_price     REAL,
    stop_loss         REAL,
    target_price      REAL,
    confidence        REAL,
    market_regime     TEXT,
    timeframe         TEXT,
    dry_run           INTEGER NOT NULL DEFAULT 1,
    placed_at         TEXT NOT NULL,
    updated_at        TEXT,
    closed_at         TEXT,
    realised_pnl      REAL,
    request_json      TEXT,
    response_json     TEXT,
    error             TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_client ON broker_orders (client_id, placed_at);
CREATE INDEX IF NOT EXISTS idx_orders_status ON broker_orders (status);

-- Append-only record of every order decision, including the ones that were
-- refused. A blocked order is the most important thing to be able to audit:
-- it is the evidence that the safety gates are doing their job.
CREATE TABLE IF NOT EXISTS trade_audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    client_id   TEXT,
    event       TEXT NOT NULL,        -- ARM | DISARM | KILL | ORDER_ALLOWED | ORDER_BLOCKED | ORDER_SENT | ORDER_FAILED | ...
    symbol      TEXT,
    detail      TEXT,
    payload     TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_at ON trade_audit (at);

CREATE TABLE IF NOT EXISTS kv (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.MARKET_DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Idempotent - safe to call on every startup and from tests."""
    global _initialised
    with _init_lock:
        if _initialised:
            return
        conn = _connect()
        try:
            # WAL lets the scheduler write bars while requests read them.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()
        _initialised = True


@contextmanager
def cursor(commit: bool = False):
    init_db()
    conn = _connect()
    try:
        yield conn
        if commit:
            conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat()


# ---------------------------------------------------------------------------
# Bars
# ---------------------------------------------------------------------------

def save_bars(symbol: str, exchange: str, timeframe: str, rows: list[dict]) -> int:
    """rows: [{ts, open, high, low, close, volume}]. Returns rows written.
    Idempotent - re-saving an overlapping window updates in place."""
    if not rows:
        return 0
    payload = [
        (symbol, exchange, timeframe, int(r["ts"]), float(r["open"]), float(r["high"]),
         float(r["low"]), float(r["close"]), float(r.get("volume") or 0))
        for r in rows
    ]
    with cursor(commit=True) as conn:
        conn.executemany(
            """INSERT INTO bars (symbol, exchange, timeframe, ts, open, high, low, close, volume)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(symbol, exchange, timeframe, ts) DO UPDATE SET
                 open=excluded.open, high=excluded.high, low=excluded.low,
                 close=excluded.close, volume=excluded.volume""",
            payload,
        )
    return len(payload)


def load_bars(symbol: str, exchange: str, timeframe: str, limit: int = 2000,
              since_ts: int | None = None) -> list[dict]:
    """Most recent `limit` bars, returned oldest-first."""
    sql = "SELECT ts, open, high, low, close, volume FROM bars WHERE symbol=? AND exchange=? AND timeframe=?"
    params: list = [symbol, exchange, timeframe]
    if since_ts is not None:
        sql += " AND ts >= ?"
        params.append(int(since_ts))
    sql += " ORDER BY ts DESC LIMIT ?"
    params.append(int(limit))
    with cursor() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in reversed(rows)]


def bar_stats() -> dict:
    with cursor() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM bars").fetchone()["c"]
        symbols = conn.execute("SELECT COUNT(DISTINCT symbol) c FROM bars").fetchone()["c"]
        oldest = conn.execute("SELECT MIN(ts) t FROM bars").fetchone()["t"]
        newest = conn.execute("SELECT MAX(ts) t FROM bars").fetchone()["t"]
        per_tf = conn.execute(
            "SELECT timeframe, COUNT(*) c FROM bars GROUP BY timeframe ORDER BY c DESC"
        ).fetchall()
    return {
        "total_bars": total,
        "distinct_symbols": symbols,
        "oldest_ts": oldest,
        "newest_ts": newest,
        "bars_by_timeframe": {r["timeframe"]: r["c"] for r in per_tf},
    }


# ---------------------------------------------------------------------------
# Stock master
# ---------------------------------------------------------------------------

def replace_stock_master(records: list[dict]) -> int:
    """Upsert the stock universe. Existing rows are updated rather than
    deleted-and-reinserted, so a partially-failed refresh (e.g. NSE reachable
    but BSE not) never wipes the half that still works."""
    if not records:
        return 0
    now = _now()
    payload = [
        (r["symbol"], r["exchange"], r["name"], r.get("isin"), r.get("sector"),
         r.get("industry"), r.get("bse_code"), now)
        for r in records
    ]
    with cursor(commit=True) as conn:
        conn.executemany(
            """INSERT INTO stock_master (symbol, exchange, name, isin, sector, industry, bse_code, updated_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(symbol, exchange) DO UPDATE SET
                 name=excluded.name,
                 isin=COALESCE(excluded.isin, stock_master.isin),
                 sector=COALESCE(excluded.sector, stock_master.sector),
                 industry=COALESCE(excluded.industry, stock_master.industry),
                 bse_code=COALESCE(excluded.bse_code, stock_master.bse_code),
                 updated_at=excluded.updated_at""",
            payload,
        )
    return len(payload)


def load_stock_master() -> list[dict]:
    with cursor() as conn:
        rows = conn.execute(
            "SELECT symbol, exchange, name, isin, sector, industry, bse_code FROM stock_master"
        ).fetchall()
    return [dict(r) for r in rows]


def stock_master_count() -> int:
    with cursor() as conn:
        return conn.execute("SELECT COUNT(*) c FROM stock_master").fetchone()["c"]


def get_stock(symbol: str, exchange: str) -> dict | None:
    with cursor() as conn:
        row = conn.execute(
            "SELECT * FROM stock_master WHERE symbol=? AND exchange=?", (symbol, exchange)
        ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Key/value (universe refresh timestamps, etc.)
# ---------------------------------------------------------------------------

def kv_set(key: str, value):
    with cursor(commit=True) as conn:
        conn.execute(
            "INSERT INTO kv (key, value, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value), _now()),
        )


def kv_get(key: str, default=None):
    with cursor() as conn:
        row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        return default


# ---------------------------------------------------------------------------
# Predictions
# ---------------------------------------------------------------------------

PREDICTION_FIELDS = [
    "client_id", "symbol", "exchange", "display_name", "timeframe", "kind", "made_at",
    "horizon_minutes", "target_at", "recommendation", "direction", "entry_price",
    "stop_loss", "target_price", "position_size", "risk_amount", "reward_amount",
    "risk_reward", "confidence", "market_regime", "indicators_json", "components_json",
]


def save_prediction(record: dict) -> int:
    row = {k: record.get(k) for k in PREDICTION_FIELDS}
    row["client_id"] = row.get("client_id") or "default"
    row["made_at"] = row.get("made_at") or _now()
    # The column default only applies when the column is omitted; passing an
    # explicit NULL for it violates NOT NULL, so fill it here.
    row["kind"] = row.get("kind") or "intraday"
    row["direction"] = row.get("direction") or 0
    placeholders = ",".join("?" for _ in PREDICTION_FIELDS)
    with cursor(commit=True) as conn:
        cur = conn.execute(
            f"INSERT INTO predictions ({','.join(PREDICTION_FIELDS)}) VALUES ({placeholders})",
            [row[k] for k in PREDICTION_FIELDS],
        )
        return cur.lastrowid


def list_predictions(client_id: str | None = None, symbol: str | None = None,
                     timeframe: str | None = None, limit: int = 200,
                     resolved: bool | None = None) -> list[dict]:
    sql = "SELECT * FROM predictions WHERE 1=1"
    params: list = []
    if client_id:
        sql += " AND client_id=?"
        params.append(client_id)
    if symbol:
        sql += " AND symbol=?"
        params.append(symbol)
    if timeframe:
        sql += " AND timeframe=?"
        params.append(timeframe)
    if resolved is not None:
        sql += " AND resolved=?"
        params.append(1 if resolved else 0)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    with cursor() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_decode_prediction(dict(r)) for r in rows]


def _decode_prediction(row: dict) -> dict:
    for key, out in (("indicators_json", "indicators"), ("components_json", "components")):
        raw = row.pop(key, None)
        try:
            row[out] = json.loads(raw) if raw else None
        except (json.JSONDecodeError, TypeError):
            row[out] = None
    row["resolved"] = bool(row.get("resolved"))
    if row.get("correct_direction") is not None:
        row["correct_direction"] = bool(row["correct_direction"])
    return row


def due_predictions(now_iso: str, limit: int = 200) -> list[dict]:
    with cursor() as conn:
        rows = conn.execute(
            "SELECT * FROM predictions WHERE resolved=0 AND target_at<=? ORDER BY id ASC LIMIT ?",
            (now_iso, int(limit)),
        ).fetchall()
    return [_decode_prediction(dict(r)) for r in rows]


def resolve_prediction(pred_id: int, actual_price: float, move_pct: float,
                       correct_direction: bool | None, outcome: str):
    with cursor(commit=True) as conn:
        conn.execute(
            """UPDATE predictions SET resolved=1, resolved_at=?, actual_price=?, move_pct=?,
                   correct_direction=?, outcome=? WHERE id=?""",
            (_now(), actual_price, move_pct,
             None if correct_direction is None else int(correct_direction),
             outcome, pred_id),
        )


# ---------------------------------------------------------------------------
# Paper trades
# ---------------------------------------------------------------------------

TRADE_FIELDS = [
    "client_id", "prediction_id", "symbol", "exchange", "display_name", "timeframe",
    "side", "quantity", "entry_price", "stop_loss", "target_price", "opened_at",
    "confidence", "market_regime", "last_price",
]


def open_trade(record: dict) -> int:
    row = {k: record.get(k) for k in TRADE_FIELDS}
    row["client_id"] = row.get("client_id") or "default"
    row["opened_at"] = row.get("opened_at") or _now()
    row["last_price"] = row.get("last_price") or row.get("entry_price")
    placeholders = ",".join("?" for _ in TRADE_FIELDS)
    with cursor(commit=True) as conn:
        cur = conn.execute(
            f"INSERT INTO paper_trades ({','.join(TRADE_FIELDS)}) VALUES ({placeholders})",
            [row[k] for k in TRADE_FIELDS],
        )
        return cur.lastrowid


def list_trades(client_id: str, status: str | None = None, limit: int = 200) -> list[dict]:
    sql = "SELECT * FROM paper_trades WHERE client_id=?"
    params: list = [client_id]
    if status:
        sql += " AND status=?"
        params.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    with cursor() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_trade(trade_id: int) -> dict | None:
    with cursor() as conn:
        row = conn.execute("SELECT * FROM paper_trades WHERE id=?", (trade_id,)).fetchone()
    return dict(row) if row else None


def open_trades_all() -> list[dict]:
    with cursor() as conn:
        rows = conn.execute("SELECT * FROM paper_trades WHERE status='OPEN'").fetchall()
    return [dict(r) for r in rows]


def update_trade_price(trade_id: int, last_price: float):
    with cursor(commit=True) as conn:
        conn.execute("UPDATE paper_trades SET last_price=? WHERE id=?", (last_price, trade_id))


def close_trade(trade_id: int, exit_price: float, exit_reason: str, pnl: float, pnl_pct: float):
    with cursor(commit=True) as conn:
        conn.execute(
            """UPDATE paper_trades SET status='CLOSED', closed_at=?, exit_price=?,
                   exit_reason=?, pnl=?, pnl_pct=?, last_price=? WHERE id=?""",
            (_now(), exit_price, exit_reason, pnl, pnl_pct, exit_price, trade_id),
        )


# ---------------------------------------------------------------------------
# Model performance (drives the adaptive ensemble weights)
# ---------------------------------------------------------------------------

def record_component_outcome(component: str, regime: str, timeframe: str, hit: bool):
    with cursor(commit=True) as conn:
        conn.execute(
            """INSERT INTO model_performance (component, regime, timeframe, hits, misses, updated_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(component, regime, timeframe) DO UPDATE SET
                 hits = model_performance.hits + excluded.hits,
                 misses = model_performance.misses + excluded.misses,
                 updated_at = excluded.updated_at""",
            (component, regime, timeframe, 1 if hit else 0, 0 if hit else 1, _now()),
        )


def component_performance(regime: str | None = None, timeframe: str | None = None) -> list[dict]:
    sql = "SELECT * FROM model_performance WHERE 1=1"
    params: list = []
    if regime:
        sql += " AND regime=?"
        params.append(regime)
    if timeframe:
        sql += " AND timeframe=?"
        params.append(timeframe)
    with cursor() as conn:
        rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        total = d["hits"] + d["misses"]
        d["samples"] = total
        d["hit_rate"] = round(d["hits"] / total, 4) if total else None
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Per-user settings (risk configuration)
# ---------------------------------------------------------------------------

def get_settings(client_id: str) -> dict | None:
    with cursor() as conn:
        row = conn.execute("SELECT data FROM settings WHERE client_id=?", (client_id,)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["data"])
    except (json.JSONDecodeError, TypeError):
        return None


def save_settings(client_id: str, data: dict):
    with cursor(commit=True) as conn:
        conn.execute(
            "INSERT INTO settings (client_id, data) VALUES (?,?) "
            "ON CONFLICT(client_id) DO UPDATE SET data=excluded.data",
            (client_id, json.dumps(data)),
        )


# ---------------------------------------------------------------------------
# Broker orders + trade audit (live trading)
# ---------------------------------------------------------------------------

ORDER_FIELDS = [
    "client_id", "prediction_id", "reference_id", "broker", "broker_order_id",
    "symbol", "exchange", "segment", "product", "side", "order_type", "quantity",
    "price", "trigger_price", "intent", "status", "stop_loss", "target_price",
    "confidence", "market_regime", "timeframe", "dry_run", "placed_at",
    "request_json", "response_json", "error",
]


def save_order(record: dict) -> int:
    row = {k: record.get(k) for k in ORDER_FIELDS}
    row["client_id"] = row.get("client_id") or "default"
    row["placed_at"] = row.get("placed_at") or _now()
    row["status"] = row.get("status") or "NEW"
    row["intent"] = row.get("intent") or "ENTRY"
    row["segment"] = row.get("segment") or "CASH"
    row["product"] = row.get("product") or "MIS"
    row["dry_run"] = 1 if row.get("dry_run", True) else 0
    placeholders = ",".join("?" for _ in ORDER_FIELDS)
    with cursor(commit=True) as conn:
        cur = conn.execute(
            f"INSERT INTO broker_orders ({','.join(ORDER_FIELDS)}) VALUES ({placeholders})",
            [row[k] for k in ORDER_FIELDS],
        )
        return cur.lastrowid


def update_order(order_id: int, **fields):
    if not fields:
        return
    fields["updated_at"] = _now()
    assignments = ",".join(f"{k}=?" for k in fields)
    with cursor(commit=True) as conn:
        conn.execute(f"UPDATE broker_orders SET {assignments} WHERE id=?",
                     [*fields.values(), order_id])


def get_order(order_id: int) -> dict | None:
    with cursor() as conn:
        row = conn.execute("SELECT * FROM broker_orders WHERE id=?", (order_id,)).fetchone()
    return dict(row) if row else None


def get_order_by_reference(reference_id: str) -> dict | None:
    with cursor() as conn:
        row = conn.execute("SELECT * FROM broker_orders WHERE reference_id=?",
                           (reference_id,)).fetchone()
    return dict(row) if row else None


def list_orders(client_id: str | None = None, status: str | None = None,
                since_iso: str | None = None, limit: int = 200) -> list[dict]:
    sql = "SELECT * FROM broker_orders WHERE 1=1"
    params: list = []
    if client_id:
        sql += " AND client_id=?"
        params.append(client_id)
    if status:
        sql += " AND status=?"
        params.append(status)
    if since_iso:
        sql += " AND placed_at >= ?"
        params.append(since_iso)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    with cursor() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def live_orders_open() -> list[dict]:
    """Orders that are still working or filled-and-unexited, real money only."""
    with cursor() as conn:
        rows = conn.execute(
            "SELECT * FROM broker_orders WHERE dry_run=0 AND status IN "
            "('NEW','OPEN','PENDING','TRIGGER_PENDING','ACKED','PARTIALLY_FILLED','FILLED') "
            "AND closed_at IS NULL ORDER BY id ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def audit(event: str, detail: str = "", symbol: str | None = None,
          client_id: str | None = None, payload=None):
    """Append-only. Never updated, never deleted - it is the record of what
    the system did with real money and why."""
    with cursor(commit=True) as conn:
        conn.execute(
            "INSERT INTO trade_audit (at, client_id, event, symbol, detail, payload) "
            "VALUES (?,?,?,?,?,?)",
            (_now(), client_id, event, symbol, detail,
             json.dumps(payload, default=str) if payload is not None else None),
        )


def list_audit(limit: int = 200, since_iso: str | None = None) -> list[dict]:
    sql = "SELECT * FROM trade_audit WHERE 1=1"
    params: list = []
    if since_iso:
        sql += " AND at >= ?"
        params.append(since_iso)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    with cursor() as conn:
        rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("payload"):
            try:
                d["payload"] = json.loads(d["payload"])
            except (json.JSONDecodeError, TypeError):
                pass
        out.append(d)
    return out


def count_orders_today(client_id: str | None = None, dry_run: bool = False) -> int:
    today = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).date().isoformat()
    sql = "SELECT COUNT(*) c FROM broker_orders WHERE placed_at >= ? AND dry_run=? AND intent='ENTRY'"
    params: list = [today, 1 if dry_run else 0]
    if client_id:
        sql += " AND client_id=?"
        params.append(client_id)
    with cursor() as conn:
        return conn.execute(sql, params).fetchone()["c"]
