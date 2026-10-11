"""User data stream (SPEC §5.5: the source of truth for orders and positions).

listenKey: POST /fapi/v1/listenKey (valid 60 min), PUT to keep alive (we do it every 30 min),
-1125 / `listenKeyExpired` → create a new key. Connection: {ws_root}/private/ws/<listenKey>
(DISCREPANCIES #2). Events are normalized into `OrderUpdate` / `AccountUpdate`; connection
changes are reported as `UserStreamStatus` so the engine can pull quotes immediately.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import orjson

from riverbank.core.events import AccountUpdate, OrderUpdate, PositionUpdate, UserStreamStatus
from riverbank.exchange import endpoints as ep
from riverbank.exchange.environments import Environment
from riverbank.exchange.rate_limiter import RateLimiter
from riverbank.exchange.rest import ApiError, RestClient
from riverbank.exchange.ws import ReconnectingWs, backoff_delay
from riverbank.ops.logging import get_logger

log = get_logger("user_stream")


def _d(x: Any, default: str = "0") -> Decimal:
    return Decimal(str(x)) if x not in (None, "") else Decimal(default)


def parse_user_event(data: dict[str, Any], recv_ts: int, venue: str) -> Any:
    e = data.get("e")
    if e == "ORDER_TRADE_UPDATE":
        o = data["o"]
        if o.get("N") not in (None, "", "USDT") and _d(o.get("n")) != 0:
            log.warning("fee_not_in_usdt", asset=o.get("N"), client_id=o.get("c"))
        return OrderUpdate(
            symbol=o["s"], client_id=o.get("c", ""), exchange_id=o.get("i"), side=o["S"], exec_type=o["x"],
            status=o["X"], price=_d(o.get("p")), qty=_d(o.get("q")), filled_qty=_d(o.get("z")),
            last_fill_qty=_d(o.get("l")), last_fill_price=_d(o.get("L")), fee=_d(o.get("n")),
            fee_asset=o.get("N") or "USDT", is_maker=bool(o.get("m", False)), trade_id=int(o.get("t") or 0),
            T=int(data.get("T") or o.get("T") or 0), E=int(data.get("E") or 0), recv_ts=recv_ts, venue=venue,
            reduce_only=bool(o.get("R", False)), expire_reason=str(o.get("er", "")), realized_pnl=_d(o.get("rp")))
    if e == "ACCOUNT_UPDATE":
        a = data["a"]
        balances = {b["a"]: _d(b.get("wb")) for b in a.get("B", [])}
        positions = [PositionUpdate(p["s"], _d(p.get("pa")), _d(p.get("ep")), _d(p.get("up")), p.get("mt", ""))
                     for p in a.get("P", []) if p.get("ps", "BOTH") == "BOTH"]
        return AccountUpdate(a.get("m", ""), balances, positions, int(data.get("E") or 0), int(data.get("T") or 0),
                             recv_ts, venue)
    if e == "listenKeyExpired":
        return "listenKeyExpired"
    return None


class UserDataStream:
    def __init__(self, venue: str, env: Environment, rest: RestClient, sink: Callable[[Any], None], *,
                 limiter: RateLimiter | None = None, keepalive_s: float = 1800.0, roll_after_s: float = 23 * 3600):
        self.venue = venue
        self.env = env
        self.rest = rest
        self.sink = sink
        self.limiter = limiter
        self.keepalive_s = keepalive_s
        self.roll_after_s = roll_after_s
        self.listen_key = ""
        self._ws: ReconnectingWs | None = None
        self._restart = asyncio.Event()
        self.reconnects = 0
        self.events = 0
        self.connected = False

    async def _key(self, method: str) -> Any:
        if self.limiter is not None:
            await self.limiter.acquire(ep.weight(method, ep.LISTEN_KEY), priority=True)
        return await self.rest.keyed(method, ep.LISTEN_KEY)

    def _on_text(self, text: str, recv_ts: int) -> None:
        try:
            data = orjson.loads(text)
            ev = parse_user_event(data, recv_ts, self.venue)
        except (KeyError, ValueError, TypeError) as e:
            log.error("user_event_parse_error", error=repr(e), sample=text[:300])
            return
        if ev == "listenKeyExpired":
            log.warning("listen_key_expired")
            self._restart.set()
            return
        if ev is not None:
            self.events += 1
            self.sink(ev)

    def _on_status(self, connected: bool, detail: str) -> None:
        self.connected = connected
        if not connected:
            self.reconnects += 1
        self.sink(UserStreamStatus(connected, time.time_ns() // 1_000_000, self.venue, detail))

    async def run(self) -> None:
        attempt = 0
        while True:
            try:
                res = await self._key("POST")
                self.listen_key = res["listenKey"]
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                attempt += 1
                log.error("listen_key_create_failed", error=repr(e))
                self._on_status(False, f"listenKey create failed: {e!r}")
                await asyncio.sleep(backoff_delay(attempt, 1.0, 60.0))
                continue
            self._restart.clear()
            url = f"{self.env.ws_private}/ws/{self.listen_key}"
            self._ws = ReconnectingWs(url, self._on_text, self._on_status, name=f"user-{self.venue}",
                                      roll_after_s=self.roll_after_s)
            ws_task = asyncio.create_task(self._ws.run())
            ka_task = asyncio.create_task(self._keepalive())
            restart_task = asyncio.create_task(self._restart.wait())
            _done, _ = await asyncio.wait({ws_task, ka_task, restart_task}, return_when=asyncio.FIRST_COMPLETED)
            self._ws.stop()
            for t in (ws_task, ka_task, restart_task):
                t.cancel()
            await asyncio.gather(ws_task, ka_task, restart_task, return_exceptions=True)
            if self.connected:
                self._on_status(False, "restarting user stream")

    async def _keepalive(self) -> None:
        while True:
            await asyncio.sleep(self.keepalive_s)
            try:
                await self._key("PUT")
            except ApiError as e:
                if e.code == -1125:
                    log.warning("listen_key_gone_recreating")
                    return  # ends the session -> new key
                log.error("listen_key_keepalive_failed", error=repr(e))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error("listen_key_keepalive_failed", error=repr(e))

    async def close(self) -> None:
        if self._ws is not None:
            self._ws.stop()
        if self.listen_key:
            try:
                await self._key("DELETE")
            except Exception as e:
                log.warning("listen_key_close_failed", error=repr(e))
