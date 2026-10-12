"""SQLite repository (SPEC §5.10): orders, fills (+markouts), risk events, recon events,
PnL snapshots, quote uptime, universe decisions, funding, alerts, kv.

All writes go through one background thread (the event loop never blocks on disk).
`kv_set_sync` is synchronous on purpose (client-id sequence reservation, HALT flag).
The class is the repository interface; a Postgres implementation can replace it later.
"""

from __future__ import annotations

import queue
import sqlite3
import threading
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from riverbank.core.models import Fill, Order, RiskEvent

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS orders (
  client_id TEXT PRIMARY KEY, venue TEXT, mode TEXT, symbol TEXT, side TEXT, price TEXT, qty TEXT, filled TEXT,
  status TEXT, reduce_only INTEGER, tif TEXT, type TEXT, reason TEXT, level INTEGER, exchange_id INTEGER,
  reject_reason TEXT, ts_created INTEGER, ts_updated INTEGER);
CREATE INDEX IF NOT EXISTS orders_sym ON orders(venue, symbol, ts_created);
CREATE TABLE IF NOT EXISTS fills (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, venue TEXT, mode TEXT, symbol TEXT, side TEXT, price TEXT,
  qty TEXT, fee TEXT, is_maker INTEGER, client_id TEXT, mid_at_fill REAL, trade_id TEXT, reason TEXT,
  mo_1 REAL, mo_5 REAL, mo_30 REAL, mo_60 REAL, mo_300 REAL,
  UNIQUE(venue, client_id, trade_id));
CREATE INDEX IF NOT EXISTS fills_ts ON fills(venue, symbol, ts);
CREATE TABLE IF NOT EXISTS risk_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, venue TEXT, level TEXT, rule TEXT, detail TEXT,
  state_before TEXT, state_after TEXT);
CREATE TABLE IF NOT EXISTS recon_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, venue TEXT, symbol TEXT, kind TEXT, detail TEXT,
  resolved INTEGER DEFAULT 0, resolved_ts INTEGER);
CREATE TABLE IF NOT EXISTS pnl_snapshots (
  ts INTEGER, venue TEXT, symbol TEXT, position REAL, mid REAL, realized REAL, fees REAL, funding REAL,
  unrealized REAL, equity REAL);
CREATE INDEX IF NOT EXISTS pnl_ts ON pnl_snapshots(venue, symbol, ts);
CREATE TABLE IF NOT EXISTS quote_uptime (
  date TEXT, venue TEXT, symbol TEXT, quoted_ms INTEGER, total_ms INTEGER, PRIMARY KEY(date, venue, symbol));
CREATE TABLE IF NOT EXISTS universe_decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, symbol TEXT, from_state TEXT, to_state TEXT, reason TEXT,
  n INTEGER, mean REAL, se REAL, detail TEXT);
CREATE TABLE IF NOT EXISTS funding (ts INTEGER, venue TEXT, symbol TEXT, rate REAL, amount REAL);
CREATE TABLE IF NOT EXISTS alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, level TEXT, kind TEXT,
  message TEXT);
"""

_STOP = object()


class SqliteRepository:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._q: queue.Queue[Any] = queue.Queue()
        self.errors = 0
        self._ready = threading.Event()
        self._open_error: sqlite3.Error | None = None
        self._thread = threading.Thread(target=self._writer, name="sqlite-writer", daemon=True)
        self._thread.start()
        self._ready.wait()
        if self._open_error is not None:
            raise self._open_error  # e.g. a corrupted file: fail startup loudly instead of hanging

    # ---- writer thread ----

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _writer(self) -> None:
        try:
            conn = self._connect()
            conn.executescript(SCHEMA)
        except sqlite3.Error as e:
            self._open_error = e
            self._ready.set()
            return
        self._mem_conn = conn  # ":memory:" databases are only visible on this connection
        self._ready.set()
        while True:
            item = self._q.get()
            batch = [item]
            while len(batch) < 500:
                try:
                    batch.append(self._q.get_nowait())
                except queue.Empty:
                    break
            stop = any(it is _STOP for it in batch)
            work = [it for it in batch if it is not _STOP]
            try:
                with conn:
                    for it in work:
                        it(conn) if callable(it) else conn.execute(*it)
            except sqlite3.Error:
                # One bad statement must not lose the rest of the batch: retry one by one.
                for it in work:
                    try:
                        with conn:
                            it(conn) if callable(it) else conn.execute(*it)
                    except sqlite3.Error:
                        self.errors += 1
            for _ in batch:
                self._q.task_done()
            if stop:
                conn.close()
                return

    def _put(self, sql: str, params: tuple[Any, ...]) -> None:
        self._q.put((sql, params))

    def flush(self) -> None:
        self._q.join()

    def close(self) -> None:
        self._q.put(_STOP)
        self._thread.join(timeout=10)

    # ---- reads (synchronous; run in a thread from async code) ----

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        self.flush()
        if self.path == ":memory:":
            result: list[list[tuple[Any, ...]]] = []
            done = threading.Event()

            def run(conn: sqlite3.Connection) -> None:
                try:
                    result.append(conn.execute(sql, params).fetchall())
                finally:
                    done.set()

            self._q.put(run)
            done.wait()
            return result[0] if result else []
        conn = self._connect()
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    # ---- kv ----

    def kv_get(self, key: str) -> str | None:
        rows = self.query("SELECT value FROM kv WHERE key=?", (key,))
        return rows[0][0] if rows else None

    def kv_set(self, key: str, value: str) -> None:
        self._put("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, value))

    def kv_set_sync(self, key: str, value: str) -> None:
        self.kv_set(key, value)
        self.flush()

    def kv_delete_sync(self, key: str) -> None:
        self._put("DELETE FROM kv WHERE key=?", (key,))
        self.flush()

    def reserve_seq_block(self, size: int) -> int:
        """Durably reserve `size` client-id sequence numbers; returns the first one."""
        start = int(self.kv_get("client_id_seq_next") or 1)
        self.kv_set_sync("client_id_seq_next", str(start + size))
        return start

    # ---- domain writes ----

    def upsert_order(self, o: Order, venue: str, mode: str) -> None:
        self._put(
            "INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(client_id) DO UPDATE SET "
            "price=excluded.price, qty=excluded.qty, filled=excluded.filled, status=excluded.status, "
            "exchange_id=excluded.exchange_id, reject_reason=excluded.reject_reason, ts_updated=excluded.ts_updated",
            (o.client_id, venue, mode, o.symbol, o.side.value, str(o.price), str(o.qty), str(o.filled_qty),
             o.status.value, int(o.reduce_only), o.time_in_force, o.order_type, o.reason, o.level, o.exchange_id,
             o.reject_reason, o.ts_created, o.ts_updated))

    def insert_fill(self, f: Fill, venue: str) -> None:
        self._put(
            "INSERT OR IGNORE INTO fills(ts, venue, mode, symbol, side, price, qty, fee, is_maker, client_id, "
            "mid_at_fill, trade_id, reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f.ts, venue, f.mode, f.symbol, f.side.value, str(f.price), str(f.qty), str(f.fee), int(f.is_maker),
             f.client_id, f.mid_at_fill, f.trade_id, f.reason))

    def update_markout(self, venue: str, client_id: str, trade_id: str, horizon_s: int, value: float) -> None:
        if horizon_s not in (1, 5, 30, 60, 300):
            return
        self._put(f"UPDATE fills SET mo_{horizon_s}=? WHERE venue=? AND client_id=? AND trade_id=?",
                  (value, venue, client_id, trade_id))

    def insert_risk_event(self, e: RiskEvent, venue: str) -> None:
        self._put("INSERT INTO risk_events(ts, venue, level, rule, detail, state_before, state_after) "
                  "VALUES(?,?,?,?,?,?,?)",
                  (e.ts, venue, e.level, e.rule, e.detail, e.state_before.value, e.state_after.value))

    def insert_recon(self, ts: int, venue: str, symbol: str, kind: str, detail: str) -> None:
        self._put("INSERT INTO recon_events(ts, venue, symbol, kind, detail) VALUES(?,?,?,?,?)",
                  (ts, venue, symbol, kind, detail))

    def resolve_recon(self, ts: int, venue: str, symbol: str) -> None:
        self._put("UPDATE recon_events SET resolved=1, resolved_ts=? WHERE venue=? AND symbol=? AND resolved=0",
                  (ts, venue, symbol))

    def insert_pnl_snapshots(self, rows: Iterable[tuple[Any, ...]]) -> None:
        rows = list(rows)

        def run(conn: sqlite3.Connection) -> None:
            conn.executemany("INSERT INTO pnl_snapshots VALUES(?,?,?,?,?,?,?,?,?,?)", rows)

        self._q.put(run)

    def add_uptime(self, date: str, venue: str, symbol: str, quoted_ms: int, total_ms: int) -> None:
        self._put("INSERT INTO quote_uptime VALUES(?,?,?,?,?) ON CONFLICT(date, venue, symbol) DO UPDATE SET "
                  "quoted_ms=quoted_ms+excluded.quoted_ms, total_ms=total_ms+excluded.total_ms",
                  (date, venue, symbol, quoted_ms, total_ms))

    def insert_decision(self, ts: int, symbol: str, from_state: str, to_state: str, reason: str, n: int,
                        mean: float, se: float, detail: str = "") -> None:
        self._put("INSERT INTO universe_decisions(ts, symbol, from_state, to_state, reason, n, mean, se, detail) "
                  "VALUES(?,?,?,?,?,?,?,?,?)", (ts, symbol, from_state, to_state, reason, n, mean, se, detail))

    def insert_funding(self, ts: int, venue: str, symbol: str, rate: float, amount: float) -> None:
        self._put("INSERT INTO funding VALUES(?,?,?,?,?)", (ts, venue, symbol, rate, amount))

    def insert_alert(self, ts: int, level: str, kind: str, message: str) -> None:
        self._put("INSERT INTO alerts(ts, level, kind, message) VALUES(?,?,?,?)", (ts, level, kind, message))

    def run_in_writer(self, fn: Callable[[sqlite3.Connection], None]) -> None:
        self._q.put(fn)
