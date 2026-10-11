"""Clocks. Strategy code reads time only through a Clock so replay is deterministic."""

from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    def now_ms(self) -> int: ...


class RealClock:
    """Local wall clock. `offset_ms` = server_time - local_time (for signed requests)."""

    def __init__(self) -> None:
        self.offset_ms = 0

    def now_ms(self) -> int:
        return time.time_ns() // 1_000_000

    def server_ms(self) -> int:
        return self.now_ms() + self.offset_ms


class SimClock:
    """Manually advanced clock for replay and tests. Never goes backwards."""

    def __init__(self, start_ms: int = 0) -> None:
        self._now = start_ms

    def now_ms(self) -> int:
        return self._now

    def server_ms(self) -> int:
        return self._now

    def advance_to(self, ts_ms: int) -> None:
        if ts_ms > self._now:
            self._now = ts_ms

    def advance(self, delta_ms: int) -> None:
        self._now += max(0, delta_ms)
