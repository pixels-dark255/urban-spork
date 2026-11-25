"""Where the harness keeps what it must not lose or duplicate.

Two backends behind one interface. Postgres when DATABASE_URL is set, and
an in-memory store for tests, so the whole harness can be exercised
without a database and without the tests inventing their own idea of what
persistence does.

The order table is the idempotency guarantee. An order id is
`date|ticker|side|strategy`, which is deterministic, so a rerun of the same
session produces the same ids and the insert is refused. That is what makes
"run it again" safe rather than a way to double a position.
"""
from __future__ import annotations

import abc
import datetime as dt
import json
import threading

PAUSE = "PAUSE"


def order_id(day, ticker: str, side: str, strategy: str) -> str:
    """Deterministic, so a rerun collides with itself instead of adding."""
    day = day.isoformat() if hasattr(day, "isoformat") else str(day)
    return "|".join([day, str(ticker).upper(), str(side).upper(), str(strategy)])


class Store(abc.ABC):
    @abc.abstractmethod
    def acquire_lock(self, name: str, holder: str, seconds: int) -> bool: ...
    @abc.abstractmethod
    def release_lock(self, name: str, holder: str) -> None: ...
    @abc.abstractmethod
    def positions(self, account: str) -> list[dict]: ...
    @abc.abstractmethod
    def put_position(self, account: str, row: dict) -> None: ...
    @abc.abstractmethod
    def record_order(self, row: dict) -> bool: ...
    @abc.abstractmethod
    def orders_for(self, day) -> list[dict]: ...
    @abc.abstractmethod
    def log(self, run_id: str, step: str, status: str, detail: dict) -> None: ...
    @abc.abstractmethod
    def logs(self, run_id: str | None = None) -> list[dict]: ...
    @abc.abstractmethod
    def get_flag(self, name: str) -> dict | None: ...
    @abc.abstractmethod
    def set_flag(self, name: str, value: str, note: str = "") -> None: ...
    @abc.abstractmethod
    def clear_flag(self, name: str) -> None: ...
    @abc.abstractmethod
    def last_run_at(self) -> dt.datetime | None: ...
    @abc.abstractmethod
    def mark_alert(self, key: str) -> bool:
        """True the first time this key is seen, False every time after.

        Alerts are deduplicated for the same reason orders are: a rerun
        must not fire the same message twice, or the alerts become noise
        and the one that matters is skimmed past."""


class MemoryStore(Store):
    """Everything in process. Used by the tests and by --dry-run offline."""

    def __init__(self):
        self._lock = threading.RLock()
        self._locks: dict = {}
        self._positions: dict = {}
        self._orders: dict = {}
        self._logs: list = []
        self._flags: dict = {}

    def acquire_lock(self, name, holder, seconds):
        now = dt.datetime.now(dt.timezone.utc)
        with self._lock:
            held = self._locks.get(name)
            if held and held["expires_at"] > now and held["holder"] != holder:
                return False
            self._locks[name] = {"holder": holder,
                                 "expires_at": now + dt.timedelta(seconds=seconds)}
            return True

    def release_lock(self, name, holder):
        with self._lock:
            if (self._locks.get(name) or {}).get("holder") == holder:
                self._locks.pop(name, None)

    def positions(self, account):
        return [dict(r) for r in sorted(self._positions.get(account, {}).values(),
                                        key=lambda r: (r["ticker"], r["strategy"]))]

    def put_position(self, account, row):
        key = (row["ticker"], row["strategy"])
        book = self._positions.setdefault(account, {})
        if int(row.get("qty", 0)) == 0:
            book.pop(key, None)
        else:
            book[key] = dict(row)

    def record_order(self, row):
        with self._lock:
            if row["order_id"] in self._orders:
                return False
            self._orders[row["order_id"]] = dict(row)
            return True

    def orders_for(self, day):
        day = day.isoformat() if hasattr(day, "isoformat") else str(day)
        return [dict(r) for r in self._orders.values() if r.get("date") == day]

    def log(self, run_id, step, status, detail):
        self._logs.append({"run_id": run_id, "step": step, "status": status,
                           "detail": detail,
                           "at": dt.datetime.now(dt.timezone.utc)})

    def logs(self, run_id=None):
        return [dict(r) for r in self._logs
                if run_id is None or r["run_id"] == run_id]

    def get_flag(self, name):
        row = self._flags.get(name)
        return dict(row) if row else None

    def set_flag(self, name, value, note=""):
        self._flags[name] = {"name": name, "value": value, "note": note,
                             "set_at": dt.datetime.now(dt.timezone.utc)}

    def clear_flag(self, name):
        self._flags.pop(name, None)

    def last_run_at(self):
        return max((r["at"] for r in self._logs), default=None)

    def mark_alert(self, key):
        with self._lock:
            if key in getattr(self, "_alerts", {}):
                return False
            if not hasattr(self, "_alerts"):
                self._alerts = {}
            self._alerts[key] = dt.datetime.now(dt.timezone.utc)
            return True


DDL = [
    """CREATE TABLE IF NOT EXISTS harness_lock (
           name TEXT PRIMARY KEY, holder TEXT NOT NULL,
           expires_at TIMESTAMPTZ NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS harness_positions (
           account TEXT NOT NULL, ticker TEXT NOT NULL, strategy TEXT NOT NULL,
           qty INTEGER NOT NULL, avg_price DOUBLE PRECISION,
           stop DOUBLE PRECISION, opened_at TIMESTAMPTZ DEFAULT now(),
           PRIMARY KEY (account, ticker, strategy))""",
    """CREATE TABLE IF NOT EXISTS harness_orders (
           order_id TEXT PRIMARY KEY, date TEXT NOT NULL, ticker TEXT NOT NULL,
           side TEXT NOT NULL, strategy TEXT NOT NULL, qty INTEGER NOT NULL,
           price DOUBLE PRECISION, status TEXT NOT NULL,
           created_at TIMESTAMPTZ DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS harness_log (
           id BIGSERIAL PRIMARY KEY, run_id TEXT NOT NULL, step TEXT NOT NULL,
           status TEXT NOT NULL, detail JSONB NOT NULL DEFAULT '{}'::jsonb,
           created_at TIMESTAMPTZ DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS harness_alerts (
           key TEXT PRIMARY KEY, sent_at TIMESTAMPTZ DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS harness_flags (
           name TEXT PRIMARY KEY, value TEXT NOT NULL, note TEXT DEFAULT '',
           set_at TIMESTAMPTZ DEFAULT now())""",
]


class PostgresStore(Store):
    """The real one. A lease row rather than pg_advisory_lock.

    An advisory lock lives on its connection, and a pooled connection that
    is handed back or dropped takes the lock with it. A row with an expiry
    survives a crashed runner: the next run takes over once the lease has
    run out, rather than waiting forever for a process that is gone.
    """

    def __init__(self, pool):
        self.pool = pool
        self._ensure()

    def _run(self, sql, args=None, fetch=None):
        conn = self.pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, args or ())
                out = None
                if fetch == "one":
                    out = cur.fetchone()
                elif fetch == "all":
                    out = cur.fetchall()
            conn.commit()
            return out
        finally:
            self.pool.putconn(conn)

    def _ensure(self):
        for statement in DDL:
            self._run(statement)

    def acquire_lock(self, name, holder, seconds):
        row = self._run(
            """INSERT INTO harness_lock (name, holder, expires_at)
               VALUES (%s, %s, now() + make_interval(secs => %s))
               ON CONFLICT (name) DO UPDATE
                 SET holder = EXCLUDED.holder, expires_at = EXCLUDED.expires_at
                 WHERE harness_lock.expires_at < now()
                    OR harness_lock.holder = EXCLUDED.holder
               RETURNING holder""", (name, holder, seconds), fetch="one")
        return bool(row and row[0] == holder)

    def release_lock(self, name, holder):
        self._run("DELETE FROM harness_lock WHERE name = %s AND holder = %s",
                  (name, holder))

    def positions(self, account):
        rows = self._run(
            """SELECT ticker, strategy, qty, avg_price, stop FROM harness_positions
               WHERE account = %s ORDER BY ticker, strategy""",
            (account,), fetch="all") or []
        return [{"ticker": r[0], "strategy": r[1], "qty": int(r[2]),
                 "avg_price": r[3], "stop": r[4]} for r in rows]

    def put_position(self, account, row):
        if int(row.get("qty", 0)) == 0:
            self._run("""DELETE FROM harness_positions
                         WHERE account=%s AND ticker=%s AND strategy=%s""",
                      (account, row["ticker"], row["strategy"]))
            return
        self._run(
            """INSERT INTO harness_positions
                 (account, ticker, strategy, qty, avg_price, stop)
               VALUES (%s,%s,%s,%s,%s,%s)
               ON CONFLICT (account, ticker, strategy) DO UPDATE
                 SET qty=EXCLUDED.qty, avg_price=EXCLUDED.avg_price,
                     stop=EXCLUDED.stop""",
            (account, row["ticker"], row["strategy"], int(row["qty"]),
             row.get("avg_price"), row.get("stop")))

    def record_order(self, row):
        got = self._run(
            """INSERT INTO harness_orders
                 (order_id, date, ticker, side, strategy, qty, price, status)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (order_id) DO NOTHING
               RETURNING order_id""",
            (row["order_id"], row["date"], row["ticker"], row["side"],
             row["strategy"], int(row["qty"]), row.get("price"),
             row.get("status", "paper")), fetch="one")
        return bool(got)

    def orders_for(self, day):
        day = day.isoformat() if hasattr(day, "isoformat") else str(day)
        rows = self._run(
            """SELECT order_id, date, ticker, side, strategy, qty, price, status
               FROM harness_orders WHERE date = %s ORDER BY order_id""",
            (day,), fetch="all") or []
        return [{"order_id": r[0], "date": r[1], "ticker": r[2], "side": r[3],
                 "strategy": r[4], "qty": int(r[5]), "price": r[6],
                 "status": r[7]} for r in rows]

    def log(self, run_id, step, status, detail):
        import jsonsafe
        self._run("""INSERT INTO harness_log (run_id, step, status, detail)
                     VALUES (%s,%s,%s,%s)""",
                  (run_id, step, status, json.dumps(jsonsafe.clean(detail))))

    def logs(self, run_id=None):
        if run_id:
            rows = self._run("""SELECT run_id, step, status, detail, created_at
                                FROM harness_log WHERE run_id=%s ORDER BY id""",
                             (run_id,), fetch="all")
        else:
            rows = self._run("""SELECT run_id, step, status, detail, created_at
                                FROM harness_log ORDER BY id DESC LIMIT 200""",
                             fetch="all")
        return [{"run_id": r[0], "step": r[1], "status": r[2],
                 "detail": r[3], "at": r[4]} for r in (rows or [])]

    def get_flag(self, name):
        row = self._run("SELECT name, value, note, set_at FROM harness_flags "
                        "WHERE name=%s", (name,), fetch="one")
        return {"name": row[0], "value": row[1], "note": row[2],
                "set_at": row[3]} if row else None

    def set_flag(self, name, value, note=""):
        self._run("""INSERT INTO harness_flags (name, value, note)
                     VALUES (%s,%s,%s)
                     ON CONFLICT (name) DO UPDATE
                       SET value=EXCLUDED.value, note=EXCLUDED.note,
                           set_at=now()""", (name, value, note))

    def clear_flag(self, name):
        self._run("DELETE FROM harness_flags WHERE name=%s", (name,))

    def last_run_at(self):
        row = self._run("SELECT max(created_at) FROM harness_log", fetch="one")
        return row[0] if row else None

    def mark_alert(self, key):
        got = self._run("""INSERT INTO harness_alerts (key) VALUES (%s)
                           ON CONFLICT (key) DO NOTHING RETURNING key""",
                        (key,), fetch="one")
        return bool(got)


def open_store() -> Store:
    """Postgres when configured, memory otherwise."""
    import storage
    if storage._pg_pool:
        return PostgresStore(storage._pg_pool)
    return MemoryStore()
