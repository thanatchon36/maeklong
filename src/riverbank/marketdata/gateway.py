"""Market Data Gateway (SPEC §5.1).

Per symbol (quoted symbols and hedge instruments):
  /public : <s>@depth@100ms, <s>@bookTicker
  /market : <s>@aggTrade,    <s>@markPrice@1s
Combined streams (`/stream?streams=`) multiplex symbols; symbols are sharded across
connections (`symbols_per_connection`) to stay far below 1024 streams/connection and to
limit head-of-line blocking. Messages are parsed into typed events and handed to `sink`
(normally the engine's inbound queue). Book state is NOT kept here (see market_state.py)
so that replay and live share the same processing path.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterable
from typing import Any

import orjson

from riverbank.core.events import AggTrade, BookTicker, DepthDiff, DepthSnapshot, MarkPrice, StreamStatus
from riverbank.exchange import endpoints as ep
from riverbank.exchange.environments import Environment
from riverbank.exchange.rate_limiter import RateLimiter
from riverbank.exchange.rest import RateLimitError, RestClient
from riverbank.exchange.ws import ReconnectingWs, backoff_delay
from riverbank.ops.logging import get_logger

log = get_logger("gateway")

Sink = Callable[[Any], None]

ROUTE_STREAMS = {
    "public": ("depth@100ms", "bookTicker"),
    "market": ("aggTrade", "markPrice@1s"),
}


def stream_names(symbols: Iterable[str], route: str) -> list[str]:
    return [f"{s.lower()}@{suffix}" for s in symbols for suffix in ROUTE_STREAMS[route]]


def combined_url(env: Environment, route: str, symbols: Iterable[str]) -> str:
    base = env.ws_public if route == "public" else env.ws_market
    return f"{base}/stream?streams={'/'.join(stream_names(symbols, route))}"


def shard(symbols: Iterable[str], per_conn: int) -> list[tuple[str, ...]]:
    syms = sorted(set(symbols))
    return [tuple(syms[i:i + per_conn]) for i in range(0, len(syms), per_conn)]


def _levels(raw: list[list[str]]) -> list[tuple[float, float]]:
    return [(float(p), float(q)) for p, q in raw]


def parse_message(text: str | bytes, recv_ts: int) -> Any:
    """Combined-stream payload {"stream":..., "data":{...}} (or raw payload) -> event or None."""
    msg = orjson.loads(text)
    d = msg.get("data", msg) if isinstance(msg, dict) else None
    if not isinstance(d, dict):
        return None
    e = d.get("e")
    if e == "depthUpdate":
        return DepthDiff(d["s"], d["E"], d.get("T", d["E"]), d["U"], d["u"], d["pu"],
                         _levels(d["b"]), _levels(d["a"]), recv_ts)
    if e == "bookTicker":
        return BookTicker(d["s"], d["u"], d["E"], d.get("T", d["E"]), float(d["b"]), float(d["B"]),
                          float(d["a"]), float(d["A"]), recv_ts)
    if e == "aggTrade":
        q = float(d["q"])
        nq = float(d["nq"]) if "nq" in d else q
        return AggTrade(d["s"], d["a"], float(d["p"]), q, nq, d["T"], d["E"], bool(d["m"]), recv_ts)
    if e == "markPriceUpdate":
        return MarkPrice(d["s"], d["E"], float(d["p"]), float(d.get("i", 0) or 0), float(d.get("r", 0) or 0),
                         int(d.get("T", 0) or 0), recv_ts)
    return None


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


class MarketDataGateway:
    def __init__(
        self,
        env: Environment,
        rest: RestClient,
        sink: Sink,
        symbols: Iterable[str],
        *,
        limiter: RateLimiter | None = None,
        symbols_per_connection: int = 10,
        roll_after_s: float = 23 * 3600,
        resync_min_interval_s: float = 5.0,
        snapshot_workers: int = 2,
        on_raw: Callable[[str, int], None] | None = None,
    ) -> None:
        self.env = env
        self.rest = rest
        self.sink = sink
        self.limiter = limiter
        self.per_conn = symbols_per_connection
        self.roll_after_s = roll_after_s
        self.resync_min_interval_s = resync_min_interval_s
        self.snapshot_workers = snapshot_workers
        self.on_raw = on_raw
        self.symbols: set[str] = set(symbols)
        self._conns: dict[tuple[str, tuple[str, ...]], tuple[ReconnectingWs, asyncio.Task[None]]] = {}
        self._snap_q: asyncio.Queue[tuple[str, int, str]] = asyncio.Queue()
        self._snap_pending: set[tuple[str, str]] = set()
        self._last_sync_snapshot: dict[str, float] = {}
        self._workers: list[asyncio.Task[None]] = []
        self.parse_errors = 0
        self.snapshot_errors = 0
        self.snapshots = 0
        self._started = False

    # ---- lifecycle ----

    async def start(self) -> None:
        self._started = True
        self._workers = [asyncio.create_task(self._snapshot_worker(i)) for i in range(self.snapshot_workers)]
        self._reshard()

    async def stop(self) -> None:
        for ws, _ in self._conns.values():
            ws.stop()
        tasks = [t for _, t in self._conns.values()] + self._workers
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._conns.clear()
        self._workers = []

    def set_symbols(self, symbols: Iterable[str]) -> None:
        new = set(symbols)
        if new == self.symbols:
            return
        self.symbols = new
        if self._started:
            self._reshard()

    def _reshard(self) -> None:
        wanted: set[tuple[str, tuple[str, ...]]] = set()
        for route in ("public", "market"):
            for i, syms in enumerate(shard(self.symbols, self.per_conn)):
                wanted.add((route, syms))
                key = (route, syms)
                if key in self._conns:
                    continue
                ws = ReconnectingWs(combined_url(self.env, route, syms), self._on_text,
                                    self._status_cb(route, i, syms), name=f"{route}-{i}",
                                    roll_after_s=self.roll_after_s)
                self._conns[key] = (ws, asyncio.create_task(ws.run()))
        for key in list(self._conns):
            if key not in wanted:
                ws, task = self._conns.pop(key)
                ws.stop()
                task.cancel()
                if key[0] == "public":
                    self.sink(StreamStatus(key[0], -1, False, key[1], _now_ms(), "unsubscribed"))

    def _status_cb(self, route: str, i: int, syms: tuple[str, ...]) -> Callable[[bool, str], None]:
        def cb(connected: bool, detail: str) -> None:
            self.sink(StreamStatus(route, i, connected, syms, _now_ms(), detail))
        return cb

    # ---- messages ----

    def _on_text(self, text: str, recv_ts: int) -> None:
        if self.on_raw is not None:
            self.on_raw(text, recv_ts)
        try:
            ev = parse_message(text, recv_ts)
        except (KeyError, ValueError, TypeError, orjson.JSONDecodeError) as e:
            self.parse_errors += 1
            log.warning("parse_error", error=repr(e), sample=text[:200])
            return
        if ev is not None:
            self.sink(ev)

    # ---- snapshots ----

    def request_snapshot(self, symbol: str, limit: int, purpose: str) -> None:
        key = (symbol, purpose)
        if key in self._snap_pending:
            return
        self._snap_pending.add(key)
        self._snap_q.put_nowait((symbol, limit, purpose))

    async def _snapshot_worker(self, idx: int) -> None:
        while True:
            symbol, limit, purpose = await self._snap_q.get()
            try:
                if purpose == "sync":
                    last = self._last_sync_snapshot.get(symbol, 0.0)
                    wait = last + self.resync_min_interval_s - time.monotonic()
                    if wait > 0:
                        await asyncio.sleep(wait)
                attempt = 0
                while symbol in self.symbols:
                    try:
                        snap = await self.fetch_snapshot(symbol, limit, purpose)
                    except asyncio.CancelledError:
                        raise
                    except RateLimitError as e:
                        if self.limiter is not None:
                            self.limiter.halt(e.retry_after_s, "429 on depth snapshot")
                        self.snapshot_errors += 1
                        await asyncio.sleep(max(e.retry_after_s or 60.0, 60.0))
                        continue
                    except Exception as e:
                        self.snapshot_errors += 1
                        log.warning("snapshot_error", symbol=symbol, error=repr(e))
                        attempt += 1
                        await asyncio.sleep(backoff_delay(attempt, base_s=1.0, cap_s=30.0))
                        continue
                    if purpose == "sync":
                        self._last_sync_snapshot[symbol] = time.monotonic()
                    self.snapshots += 1
                    self.sink(snap)
                    break
            finally:
                self._snap_pending.discard((symbol, purpose))

    async def fetch_snapshot(self, symbol: str, limit: int, purpose: str) -> DepthSnapshot:
        params = {"symbol": symbol, "limit": limit}
        if self.limiter is not None:
            await self.limiter.acquire(ep.weight("GET", ep.DEPTH, params))
        d = await self.rest.public("GET", ep.DEPTH, params)
        return DepthSnapshot(symbol, int(d["lastUpdateId"]), _levels(d["bids"]), _levels(d["asks"]),
                             _now_ms(), purpose)
