"""Client-side rate limiter built from exchangeInfo.rateLimits (never constants).

Binance counts weight per IP and orders per account in fixed windows aligned to the
interval (the X-MBX-USED-WEIGHT-1M counter resets every minute). We mirror those
windows, keep usage below a budget fraction, and correct drift from response headers.

On HTTP 429/418 the limiter halts all traffic until Retry-After (min 60 s) elapses
(OPEN_QUESTIONS B7); the caller must also move the risk engine to HALTED.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from riverbank.exchange.specs import RateLimit


class RateLimitHalted(RuntimeError):
    def __init__(self, until: float) -> None:
        super().__init__(f"rate-limit halt until {until:.0f}")
        self.until = until


@dataclass(slots=True)
class FixedWindow:
    limit: int
    window_s: int
    header: str  # e.g. "x-mbx-used-weight-1m"
    used: int = 0
    window_id: int = -1

    def roll(self, now: float) -> None:
        wid = int(now // self.window_s)
        if wid != self.window_id:
            self.window_id = wid
            self.used = 0

    def headroom(self, cap: int, now: float) -> int:
        self.roll(now)
        return cap - self.used

    def seconds_to_reset(self, now: float) -> float:
        return (math.floor(now / self.window_s) + 1) * self.window_s - now


class RateLimiter:
    def __init__(
        self,
        rate_limits: tuple[RateLimit, ...] | list[RateLimit],
        *,
        weight_frac: float = 0.8,
        order_frac: float = 0.5,
        priority_frac: float = 0.95,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.clock = clock
        self.weight_frac = weight_frac
        self.order_frac = order_frac
        self.priority_frac = priority_frac
        self.weight: list[FixedWindow] = []
        self.orders: list[FixedWindow] = []
        for rl in rate_limits:
            if rl.limit_type == "REQUEST_WEIGHT":
                self.weight.append(FixedWindow(rl.limit, rl.window_s, f"x-mbx-used-weight-{rl.header_suffix.lower()}"))
            elif rl.limit_type == "ORDERS":
                self.orders.append(FixedWindow(rl.limit, rl.window_s, f"x-mbx-order-count-{rl.header_suffix.lower()}"))
        if not self.weight:
            raise ValueError("exchangeInfo has no REQUEST_WEIGHT limit")
        self.halted_until = 0.0
        self.halt_reason = ""

    # ---- admission ----

    def _caps(self, priority: bool) -> tuple[float, float]:
        return (self.priority_frac if priority else self.weight_frac,
                self.priority_frac if priority else self.order_frac)

    def try_acquire(self, weight: int, orders: int = 0, *, priority: bool = False) -> float:
        """Take capacity if available. Returns 0.0 on success, else seconds to wait."""
        now = self.clock()
        if now < self.halted_until:
            raise RateLimitHalted(self.halted_until)
        wf, of = self._caps(priority)
        wait = 0.0
        for w in self.weight:
            if w.headroom(int(w.limit * wf), now) < weight:
                wait = max(wait, w.seconds_to_reset(now))
        if orders:
            for o in self.orders:
                if o.headroom(int(o.limit * of), now) < orders:
                    wait = max(wait, o.seconds_to_reset(now))
        if wait > 0:
            return wait
        for w in self.weight:
            w.used += weight
        if orders:
            for o in self.orders:
                o.used += orders
        return 0.0

    async def acquire(self, weight: int, orders: int = 0, *, priority: bool = False,
                      max_wait_s: float = 120.0) -> None:
        waited = 0.0
        while True:
            wait = self.try_acquire(weight, orders, priority=priority)
            if wait == 0.0:
                return
            if waited + wait > max_wait_s:
                raise TimeoutError(f"rate limiter wait would exceed {max_wait_s}s")
            await asyncio.sleep(wait + 0.01)
            waited += wait

    # ---- feedback ----

    def on_headers(self, headers: Mapping[str, str], *, ignore_weight: bool = False) -> None:
        now = self.clock()
        low = {k.lower(): v for k, v in headers.items()}
        for win in ([] if ignore_weight else self.weight) + self.orders:
            v = low.get(win.header)
            if v is None:
                continue
            try:
                used = int(v)
            except ValueError:
                continue
            win.roll(now)
            win.used = max(win.used, used)

    def halt(self, retry_after_s: float | None, reason: str) -> None:
        self.halted_until = max(self.halted_until, self.clock() + max(retry_after_s or 0.0, 60.0))
        self.halt_reason = reason

    @property
    def halted(self) -> bool:
        return self.clock() < self.halted_until

    def usage(self) -> dict[str, float]:
        now = self.clock()
        out = {}
        for w in self.weight:
            w.roll(now)
            out[w.header] = w.used / w.limit
        for o in self.orders:
            o.roll(now)
            out[o.header] = o.used / o.limit
        return out
