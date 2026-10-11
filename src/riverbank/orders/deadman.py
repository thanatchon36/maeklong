"""Dead-man switch (SPEC §5.5): POST /fapi/v1/countdownCancelAll heartbeats.

The countdown is per symbol (DISCREPANCIES #3): every `heartbeat_s` each symbol that has
(or may get) our orders is re-armed with `countdown_s`. If the process dies or loses the
network, Binance cancels all open orders of those symbols when the countdown expires.
If heartbeats fail, the venue stops quoting the symbol (`healthy()` is a quoting gate) so
we never rely on orders that the exchange is about to cancel.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterable

from riverbank.exchange import endpoints as ep
from riverbank.exchange.rate_limiter import RateLimiter
from riverbank.exchange.rest import RestClient
from riverbank.ops.logging import get_logger

log = get_logger("deadman")


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


class DeadmanSwitch:
    def __init__(self, rest: RestClient, limiter: RateLimiter | None, symbols: Callable[[], Iterable[str]], *,
                 heartbeat_s: float, countdown_s: float, on_failure: Callable[[str, str], None] | None = None):
        self.rest = rest
        self.limiter = limiter
        self.symbols = symbols
        self.heartbeat_s = heartbeat_s
        self.countdown_ms = int(countdown_s * 1000)
        self.on_failure = on_failure
        self.last_ok: dict[str, int] = {}
        self.armed: set[str] = set()
        self.failures = 0
        self.beats = 0

    async def _post(self, symbol: str, countdown_ms: int) -> None:
        if self.limiter is not None:
            await self.limiter.acquire(ep.weight("POST", ep.COUNTDOWN_CANCEL_ALL), priority=True)
        await self.rest.signed("POST", ep.COUNTDOWN_CANCEL_ALL, {"symbol": symbol, "countdownTime": countdown_ms})

    async def beat_once(self) -> None:
        for sym in sorted(set(self.symbols())):
            try:
                await self._post(sym, self.countdown_ms)
                self.last_ok[sym] = _now_ms()
                self.armed.add(sym)
                self.beats += 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.failures += 1
                log.error("deadman_heartbeat_failed", symbol=sym, error=repr(e))
                if self.on_failure is not None:
                    self.on_failure(sym, repr(e))

    async def run(self) -> None:
        while True:
            await self.beat_once()
            await asyncio.sleep(self.heartbeat_s)

    def healthy(self, symbol: str, now_ms: int) -> bool:
        """Quoting gate: a heartbeat succeeded within half the countdown."""
        last = self.last_ok.get(symbol)
        return last is not None and now_ms - last < self.countdown_ms / 2

    async def disarm(self) -> None:
        """Clean shutdown (after our cancels): stop the countdown timers (countdownTime=0)."""
        for sym in sorted(self.armed):
            try:
                await self._post(sym, 0)
            except Exception as e:
                log.warning("deadman_disarm_failed", symbol=sym, error=repr(e))
        self.armed.clear()
