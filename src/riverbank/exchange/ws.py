"""Reconnecting WebSocket with exponential backoff + full jitter and make-before-break rolls.

Binance closes every connection at 24 h; we roll proactively at `roll_after_s`: a second
session is opened and buffers while the old one keeps delivering; after a short overlap
the old one is closed and the buffer is replayed (consumers drop duplicates by update id).
Server pings (every 3 min) are answered automatically by aiohttp (autoping); a client
heartbeat detects half-open TCP connections.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Callable

import aiohttp

from riverbank.ops.logging import get_logger

MessageHandler = Callable[[str, int], None]  # (text, recv_ts_ms)
StatusHandler = Callable[[bool, str], None]  # (connected, detail)

log = get_logger("ws")


def backoff_delay(attempt: int, base_s: float = 0.5, cap_s: float = 30.0, rng: random.Random | None = None) -> float:
    """Full-jitter exponential backoff: uniform(0, min(cap, base * 2**attempt))."""
    r = rng or random
    return r.uniform(0.0, min(cap_s, base_s * (2 ** min(attempt, 16))))


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


class ReconnectingWs:
    def __init__(
        self,
        url: str,
        on_message: MessageHandler,
        on_status: StatusHandler | None = None,
        *,
        name: str = "ws",
        roll_after_s: float = 23 * 3600,
        heartbeat_s: float = 30.0,
        idle_timeout_s: float = 60.0,
        overlap_s: float = 3.0,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self.url = url
        self.on_message = on_message
        self.on_status = on_status
        self.name = name
        self.roll_after_s = roll_after_s
        self.heartbeat_s = heartbeat_s
        self.idle_timeout_s = idle_timeout_s
        self.overlap_s = overlap_s
        self._session = session
        self._stop = asyncio.Event()
        self.connects = 0
        self.disconnects = 0
        self.rolls = 0
        self.messages = 0
        self._live_sessions = 0

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        own = self._session is None
        sess = self._session or aiohttp.ClientSession()
        attempt = 0
        try:
            while not self._stop.is_set():
                started = time.monotonic()
                try:
                    await self._run_with_rolls(sess)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # network errors of any kind -> reconnect
                    log.warning("ws_error", name=self.name, error=repr(e))
                if self._stop.is_set():
                    break
                self.disconnects += 1
                if self.on_status:
                    self.on_status(False, "disconnected")
                attempt = 0 if time.monotonic() - started > 60 else attempt + 1
                await asyncio.sleep(backoff_delay(attempt))
        finally:
            if own:
                await sess.close()

    async def _connect(self, sess: aiohttp.ClientSession) -> aiohttp.ClientWebSocketResponse:
        ws = await sess.ws_connect(self.url, heartbeat=self.heartbeat_s, autoping=True,
                                   max_msg_size=16 * 1024 * 1024, timeout=aiohttp.ClientWSTimeout(ws_close=5))
        self.connects += 1
        return ws

    async def _run_with_rolls(self, sess: aiohttp.ClientSession) -> None:
        ws = await self._connect(sess)
        if self.on_status:
            self.on_status(True, "connected")
        log.info("ws_connected", name=self.name, url=self.url.split("?")[0])
        while not self._stop.is_set():
            roll_deadline = time.monotonic() + self.roll_after_s
            ended = await self._pump(ws, roll_deadline)
            if ended or self._stop.is_set():
                await ws.close()
                return
            # Roll (make-before-break): the old session keeps delivering while the new one
            # buffers for `overlap_s`; then the old one is closed and the buffer replayed in
            # order (consumers drop the duplicates), so per-connection ordering is preserved.
            try:
                new_ws = await self._connect(sess)
            except Exception as e:
                log.warning("ws_roll_failed", name=self.name, error=repr(e))
                continue  # keep the old connection; retry after another period
            buffered: list[tuple[str, int]] = []
            overlap_end = time.monotonic() + self.overlap_s
            old_ended, new_ended = await asyncio.gather(
                self._pump(ws, overlap_end), self._pump(new_ws, overlap_end, buffered.append))
            if new_ended or not buffered:
                await new_ws.close()
                if old_ended:
                    return
                continue
            await ws.close()
            for text, ts in buffered:
                self.on_message(text, ts)
            self.rolls += 1
            log.info("ws_rolled", name=self.name, replayed=len(buffered))
            ws = new_ws

    async def _pump(self, ws: aiohttp.ClientWebSocketResponse, roll_deadline: float,
                    sink: Callable[[tuple[str, int]], None] | None = None) -> bool:
        """Read until the deadline (returns False) or the connection ends (returns True).

        Messages go to `on_message`, or to `sink` (buffer) when given.
        """
        while not self._stop.is_set():
            remaining = roll_deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                msg = await ws.receive(timeout=min(self.idle_timeout_s, remaining))
            except TimeoutError:
                if roll_deadline - time.monotonic() <= 0:
                    return False
                log.warning("ws_idle_timeout", name=self.name)
                return True
            if msg.type is aiohttp.WSMsgType.TEXT:
                self.messages += 1
                if sink is not None:
                    sink((msg.data, _now_ms()))
                else:
                    self.on_message(msg.data, _now_ms())
            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING,
                              aiohttp.WSMsgType.ERROR):
                log.warning("ws_closed", name=self.name, type=str(msg.type), extra=str(msg.extra))
                return True
        return True
