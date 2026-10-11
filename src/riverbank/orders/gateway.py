"""Real order gateway (demo/live): REST execution with Binance error semantics (SPEC §5.5).

The engine calls `submit(cmd)` synchronously; commands are queued (cancels first) and sent
by worker tasks through the rate limiter. Every outcome goes back to the engine as an event
(`OrderAck`), never by mutating engine state, so the engine stays single-threaded.

HTTP 503 handling (developers.binance.com, DISCREPANCIES #4):
  * "Unknown error…" / client timeout on a mutating call → UNKNOWN: never resend; query by
    origClientOrderId (and wait for the user stream)
  * "Service Unavailable." / "Internal error" → definite failure: retry 200→400→800 ms (max 4)
  * -1008 throttled → definite failure: back off, reduce concurrency (cancels/reduce-only exempt)
  * 429 / 418 → halt all traffic (limiter) and HALT the venue (OPEN_QUESTIONS B7)
"""

from __future__ import annotations

import asyncio
import itertools
import time
from collections.abc import Callable
from typing import Any

from riverbank.core.decimal_utils import fmt
from riverbank.core.events import OrderAck
from riverbank.exchange import endpoints as ep
from riverbank.exchange.rate_limiter import RateLimiter, RateLimitHalted
from riverbank.exchange.rest import (
    ApiError,
    IpBanError,
    RateLimitError,
    RestClient,
    RetryableError,
    ThrottledError,
    UnknownExecutionError,
)
from riverbank.ops.logging import get_logger
from riverbank.orders.manager import CancelCmd, Command, PlaceCmd

log = get_logger("gateway.orders")

RETRY_DELAYS_S = (0.2, 0.4, 0.8, 1.6)
QUERY_GRACE_MS = 10_000  # -2013 younger than this: maybe still in flight -> query again later


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


class OrderGateway:
    def __init__(self, venue: str, rest: RestClient, limiter: RateLimiter, sink: Callable[[Any], None], *,
                 on_rate_limit: Callable[[str], None] | None = None, workers: int = 4) -> None:
        self.venue = venue
        self.rest = rest
        self.limiter = limiter
        self.sink = sink
        self.on_rate_limit = on_rate_limit
        self.n_workers = workers
        self._q: asyncio.PriorityQueue[tuple[int, int, Any]] = asyncio.PriorityQueue()
        self._seq = itertools.count()
        self._tasks: list[asyncio.Task[None]] = []
        self._sem = asyncio.Semaphore(workers)
        self._throttled_until = 0.0
        self.stats = {"placed": 0, "canceled": 0, "unknown": 0, "rejected": 0, "failed": 0, "queries": 0,
                      "retries": 0, "throttled": 0}
        self._created: dict[str, int] = {}

    # ---- intake (sync, called by the engine) ----

    def submit(self, cmd: Command) -> None:
        prio = 0 if isinstance(cmd, CancelCmd) else 1
        self._q.put_nowait((prio, next(self._seq), cmd))

    def query(self, symbol: str, client_id: str, created_ms: int, delay_s: float = 0.5) -> None:
        self._created.setdefault(client_id, created_ms)
        asyncio.get_running_loop().call_later(delay_s, lambda: self._q.put_nowait(
            (0, next(self._seq), ("query", symbol, client_id))))

    # ---- workers ----

    def start(self) -> list[asyncio.Task[None]]:
        self._tasks = [asyncio.create_task(self._worker()) for _ in range(self.n_workers)]
        return self._tasks

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def drain(self, timeout_s: float = 5.0) -> None:
        deadline = time.monotonic() + timeout_s
        while not self._q.empty() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)

    async def _worker(self) -> None:
        while True:
            _, _, item = await self._q.get()
            try:
                if isinstance(item, PlaceCmd):
                    await self._place(item)
                elif isinstance(item, CancelCmd):
                    await self._cancel(item)
                elif isinstance(item, tuple) and item[0] == "query":
                    await self._query(item[1], item[2])
            except asyncio.CancelledError:
                raise
            except Exception as e:  # never let a worker die
                log.exception("gateway_worker_error", error=repr(e))

    def _ack(self, client_id: str, symbol: str, action: str, outcome: str, **kw: Any) -> None:
        self.sink(OrderAck(client_id, symbol, action, outcome, _now_ms(), self.venue, **kw))

    def _rate_limited(self, e: RateLimitError | IpBanError) -> None:
        self.limiter.halt(e.retry_after_s, f"HTTP {e.status}")
        log.error("rate_limited", status=e.status, retry_after=e.retry_after_s, msg=e.msg)
        if self.on_rate_limit is not None:
            self.on_rate_limit(f"HTTP {e.status} from exchange: {e.msg}")

    async def _place(self, cmd: PlaceCmd) -> None:
        o = cmd.order
        self._created[o.client_id] = o.ts_created
        params: dict[str, Any] = {"symbol": o.symbol, "side": o.side.value, "type": o.order_type,
                                  "quantity": fmt(o.qty), "newClientOrderId": o.client_id,
                                  "newOrderRespType": "ACK"}
        if o.order_type == "LIMIT":
            params["price"] = fmt(o.price)
            params["timeInForce"] = o.time_in_force
        if o.reduce_only:
            params["reduceOnly"] = "true"
        for attempt in range(len(RETRY_DELAYS_S) + 1):
            if not o.reduce_only and time.monotonic() < self._throttled_until:
                await asyncio.sleep(self._throttled_until - time.monotonic())
            try:
                await self.limiter.acquire(ep.weight("POST", ep.ORDER), orders=1, priority=o.reduce_only)
                async with self._sem:
                    res = await self.rest.signed("POST", ep.ORDER, params)
            except RateLimitHalted:
                self.stats["failed"] += 1
                self._ack(o.client_id, o.symbol, "place", "failed", msg="rate-limit halt")
                return
            except UnknownExecutionError as e:
                self.stats["unknown"] += 1
                log.warning("place_unknown", client_id=o.client_id, msg=e.msg)
                self._ack(o.client_id, o.symbol, "place", "unknown", msg=e.msg[:200])
                self.query(o.symbol, o.client_id, o.ts_created)
                return
            except (RateLimitError, IpBanError) as e:
                self._rate_limited(e)
                self._ack(o.client_id, o.symbol, "place", "failed", code=e.code, msg=e.msg)
                return
            except ThrottledError as e:
                self.stats["throttled"] += 1
                self._throttled_until = time.monotonic() + RETRY_DELAYS_S[min(attempt, len(RETRY_DELAYS_S) - 1)] * 4
                if attempt < len(RETRY_DELAYS_S):
                    self.stats["retries"] += 1
                    await asyncio.sleep(RETRY_DELAYS_S[attempt])
                    continue
                self.stats["failed"] += 1
                self._ack(o.client_id, o.symbol, "place", "failed", code=e.code, msg=e.msg)
                return
            except RetryableError as e:
                if attempt < len(RETRY_DELAYS_S):
                    self.stats["retries"] += 1
                    await asyncio.sleep(RETRY_DELAYS_S[attempt])
                    continue
                self.stats["failed"] += 1
                self._ack(o.client_id, o.symbol, "place", "failed", code=e.code, msg=e.msg)
                return
            except ApiError as e:
                self.stats["rejected"] += 1
                if e.code == -1021:  # timestamp outside recvWindow: caller resyncs the clock
                    log.warning("timestamp_rejected", msg=e.msg)
                self._ack(o.client_id, o.symbol, "place", "rejected", code=e.code, msg=e.msg)
                return
            self.stats["placed"] += 1
            self._ack(o.client_id, o.symbol, "place", "ok", exchange_id=res.get("orderId"),
                      status=res.get("status", "NEW"))
            return

    async def _cancel(self, cmd: CancelCmd) -> None:
        params = {"symbol": cmd.symbol, "origClientOrderId": cmd.client_id}
        for attempt in range(len(RETRY_DELAYS_S) + 1):
            try:
                await self.limiter.acquire(ep.weight("DELETE", ep.ORDER), priority=True)
                res = await self.rest.signed("DELETE", ep.ORDER, params)
            except RateLimitHalted:
                self._ack(cmd.client_id, cmd.symbol, "cancel", "failed", msg="rate-limit halt")
                return
            except UnknownExecutionError as e:
                self._ack(cmd.client_id, cmd.symbol, "cancel", "unknown", msg=e.msg[:200])
                self.query(cmd.symbol, cmd.client_id, self._created.get(cmd.client_id, _now_ms()))
                return
            except (RateLimitError, IpBanError) as e:
                self._rate_limited(e)
                self._ack(cmd.client_id, cmd.symbol, "cancel", "failed", code=e.code, msg=e.msg)
                return
            except (RetryableError, ThrottledError):
                if attempt < len(RETRY_DELAYS_S):
                    self.stats["retries"] += 1
                    await asyncio.sleep(RETRY_DELAYS_S[attempt])
                    continue
                self._ack(cmd.client_id, cmd.symbol, "cancel", "failed")
                return
            except ApiError as e:
                if e.code in (-2011, -2013):  # unknown order: filled/cancelled already or never existed
                    self._ack(cmd.client_id, cmd.symbol, "cancel", "not_found", code=e.code, msg=e.msg)
                    self.query(cmd.symbol, cmd.client_id, self._created.get(cmd.client_id, _now_ms()), 0.2)
                else:
                    self._ack(cmd.client_id, cmd.symbol, "cancel", "rejected", code=e.code, msg=e.msg)
                return
            self.stats["canceled"] += 1
            self._ack(cmd.client_id, cmd.symbol, "cancel", "ok", exchange_id=res.get("orderId"),
                      status=res.get("status", "CANCELED"))
            return

    async def _query(self, symbol: str, client_id: str) -> None:
        self.stats["queries"] += 1
        try:
            await self.limiter.acquire(ep.weight("GET", ep.ORDER), priority=True)
            res = await self.rest.signed("GET", ep.ORDER, {"symbol": symbol, "origClientOrderId": client_id})
        except ApiError as e:
            if e.code == -2013:
                created = self._created.get(client_id, 0)
                if _now_ms() - created < QUERY_GRACE_MS:
                    self.query(symbol, client_id, created, 1.0)  # may still be in flight
                    return
                self._ack(client_id, symbol, "query", "not_found", code=e.code, msg=e.msg)
                return
            self.query(symbol, client_id, self._created.get(client_id, 0), 2.0)
            return
        except (RateLimitError, IpBanError) as e:
            self._rate_limited(e)
            return
        except (RetryableError, ThrottledError, UnknownExecutionError, RateLimitHalted, TimeoutError):
            self.query(symbol, client_id, self._created.get(client_id, 0), 2.0)
            return
        self._ack(client_id, symbol, "query", "ok", exchange_id=res.get("orderId"), status=res.get("status", ""),
                  raw={"executedQty": res.get("executedQty", "0"), "avgPrice": res.get("avgPrice", "0")})
