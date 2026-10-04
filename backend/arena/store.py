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


def load(ip: str) -> dict | None:
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
