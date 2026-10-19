"""
Arena persistence, one state dict per client id.

Same two backends as storage.py and the same rule: Postgres when
DATABASE_URL is set (survives Render restarts), otherwise a JSON file under
backend/data/. Reuses storage.py's connection pool rather than opening a
second one. Every write goes through jsonsafe.clean, because Postgres JSONB
rejects NaN and a rejected write would silently lose a day of trades.
"""
from __future__ import annotations

import json
import os
import threading

import jsonsafe
import storage

ARENA_PATH = os.path.join(storage.DATA_DIR, "arena.json")
_lock = threading.RLock()

if storage._pg_pool:
    _conn = storage._pg_pool.getconn()
    try:
        with _conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS arena_store (
                    ip TEXT PRIMARY KEY,
                    data JSONB NOT NULL DEFAULT '{}'::jsonb
                )
            """)
        _conn.commit()
    finally:
        storage._pg_pool.putconn(_conn)


def backend_name() -> str:
    """Which backend is actually in use. Surfaced in the API so the UI can
    warn that a JSON file on Render's free tier is wiped on every sleep."""
    return "postgres" if storage._pg_pool else "json_file"


def backend_status() -> dict:
    """Enough for the UI to say something true.

    "Ephemeral" has two causes that need opposite advice: DATABASE_URL was
    never set, or it was set and Postgres could not be reached. Telling
    someone who has already set it to set it is how a broken driver stays
    broken for a week.
    """
    name = backend_name()
    return {
        "backend": name,
        "ephemeral": name == "json_file",
        "configured": bool(getattr(storage, "DATABASE_URL", "")),
        "error": getattr(storage, "PG_ERROR", None),
    }


def _file_load() -> dict:
    if not os.path.exists(ARENA_PATH):
        return {}
    try:
        with open(ARENA_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _file_save(data: dict) -> None:
    tmp = ARENA_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, ARENA_PATH)


# ---------------------------------------------------------------------------
# Keys: one arena per client PER MARKET
# ---------------------------------------------------------------------------
#
# A key is "<client>::<market>". The arena predates markets, so the keys it
# wrote are bare client ids with no suffix; those are read as the client's
# NSE arena and rewritten under the new key the first time they are loaded.
# Migrating on read rather than in a batch means it works on a free Render
# instance that may never be awake long enough to run one.

SEP = "::"
LEGACY_MARKET = "nse"


def key(client: str, market: str = LEGACY_MARKET) -> str:
    return f"{client}{SEP}{market or LEGACY_MARKET}"


def split_key(stored: str) -> tuple[str, str]:
    """(client, market) for any key, old-style or new."""
    if SEP in stored:
        client, _, market = stored.rpartition(SEP)
        return client, (market or LEGACY_MARKET)
    return stored, LEGACY_MARKET


# Backtest results are stored beside live arenas under a prefixed id; the
# prefix is defined in service.py but named here so key handling can see it.
BACKTEST_HINT = "bt::"


def _legacy_key_for(stored: str) -> str | None:
    """The pre-markets key a given key would have been stored under, if any.

    Only a live NSE arena ever had one. Backtests are excluded outright:
    their ids carry a prefix that contains the separator, so splitting one
    produces nonsense that is not worth trying to migrate.
    """
    if stored.startswith(BACKTEST_HINT):
        return None
    client, market = split_key(stored)
    if market == LEGACY_MARKET and stored != client:
        return client
    return None


def load(ip: str) -> dict | None:
    """Load a state, migrating a pre-markets NSE arena on first read."""
    found = _load_raw(ip)
    if found is not None:
        return found
    legacy = _legacy_key_for(ip)
    if legacy is None:
        return None
    old = _load_raw(legacy)
    if old is None:
        return None
    # Stamp the market it was always trading, then write it under the new
    # key and drop the old one, so this happens exactly once.
    old.setdefault("config", {})["market"] = LEGACY_MARKET
    save(ip, old)
    delete(legacy)
    print(f"[info] arena: migrated {legacy!r} to {ip!r}")
    return _load_raw(ip)


def delete(ip: str) -> None:
    with _lock:
        if storage._pg_pool:
            conn = storage._pg_pool.getconn()
            try:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM arena_store WHERE ip = %s", (ip,))
                conn.commit()
            finally:
                storage._pg_pool.putconn(conn)
            return
        data = _file_load()
        if data.pop(ip, None) is not None:
            _file_save(data)


def _load_raw(ip: str) -> dict | None:
    with _lock:
        if storage._pg_pool:
            conn = storage._pg_pool.getconn()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT data FROM arena_store WHERE ip = %s", (ip,))
                    row = cur.fetchone()
                return row[0] if row else None
            finally:
                storage._pg_pool.putconn(conn)
        return _file_load().get(ip)


def save(ip: str, state: dict) -> None:
    state = jsonsafe.clean(state)
    with _lock:
        if storage._pg_pool:
            conn = storage._pg_pool.getconn()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO arena_store (ip, data) VALUES (%s, %s)
                           ON CONFLICT (ip) DO UPDATE SET data = EXCLUDED.data""",
                        (ip, json.dumps(state)),
                    )
                conn.commit()
            finally:
                storage._pg_pool.putconn(conn)
            return
        data = _file_load()
        data[ip] = state
        _file_save(data)


def all_ids() -> list[str]:
    with _lock:
        if storage._pg_pool:
            conn = storage._pg_pool.getconn()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT ip FROM arena_store")
                    return [r[0] for r in cur.fetchall()]
            finally:
                storage._pg_pool.putconn(conn)
        return list(_file_load().keys())


# Callers that read-modify-write hold this so the scheduler and an API call
# can't interleave and lose each other's changes. Re-entrant, so load/save
# can be called while holding it.
lock = _lock
