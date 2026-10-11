"""Market-data processing shared by the M1 validator, shadow, demo and live engines.

The gateway pushes parsed events into the owner's queue; the owner calls `handle(ev)`
sequentially (single task) and `on_timer(now)` every 100 ms.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from riverbank.core.config import OpsCfg
from riverbank.core.events import (
    AggTrade,
    BookTicker,
    BookTopN,
    DepthDiff,
    DepthSnapshot,
    MarkPrice,
    StreamStatus,
)
from riverbank.core.models import BookStatus
from riverbank.exchange.environments import Environment
from riverbank.exchange.rate_limiter import RateLimiter
from riverbank.exchange.rest import RestClient
from riverbank.marketdata.gateway import MarketDataGateway
from riverbank.marketdata.market_state import CheckListener, MarketState
from riverbank.marketdata.recorder import Recorder
from riverbank.ops.logging import get_logger

log = get_logger("marketdata")


class MarketDataPipeline:
    def __init__(
        self,
        ops: OpsCfg,
        env: Environment,
        rest: RestClient,
        sink: Callable[[Any], None],
        symbols: list[str],
        *,
        limiter: RateLimiter | None = None,
        recorder: Recorder | None = None,
        record_interval_ms: int = 100,
        on_check: CheckListener | None = None,
        refresh_interval_ms: int = 30 * 60 * 1000,
    ) -> None:
        self.ops = ops
        self.recorder = recorder
        self.record_interval_ms = record_interval_ms
        self._last_record = 0
        self.gateway = MarketDataGateway(
            env, rest, sink, symbols, limiter=limiter, symbols_per_connection=ops.symbols_per_connection,
            roll_after_s=ops.ws_roll_hours * 3600, resync_min_interval_s=ops.resync_min_interval_s)
        self.state = MarketState(symbols, stale_ms=ops.stale_ms, request_snapshot=self.gateway.request_snapshot,
                                 snapshot_limit=ops.depth_snapshot_limit, on_check=on_check,
                                 refresh_interval_ms=refresh_interval_ms)
        self.status_changes: list[tuple[int, str, BookStatus, BookStatus]] = []

    async def start(self) -> None:
        await self.gateway.start()

    async def stop(self) -> None:
        await self.gateway.stop()

    def set_symbols(self, symbols: list[str]) -> None:
        for s in symbols:
            self.state.add_symbol(s)
        for s in list(self.state.symbols):
            if s not in symbols:
                self.state.remove_symbol(s)
        self.gateway.set_symbols(symbols)

    def handle(self, ev: Any) -> bool:
        """Apply a market-data event. Returns True if the event was new (not a duplicate)."""
        if isinstance(ev, DepthDiff):
            if self.recorder:
                self.recorder.on_event(ev)
            self.state.on_depth(ev)
            return True
        if isinstance(ev, BookTicker):
            new = self.state.on_book_ticker(ev)
        elif isinstance(ev, AggTrade):
            new = self.state.on_agg_trade(ev)
        elif isinstance(ev, MarkPrice):
            new = self.state.on_mark(ev)
        elif isinstance(ev, DepthSnapshot):
            self.state.on_snapshot(ev)
            return True
        elif isinstance(ev, StreamStatus):
            invalidated = self.state.on_stream_status(ev)
            if invalidated:
                log.warning("stream_down_books_invalid", route=ev.route, symbols=invalidated)
            return True
        else:
            return False
        if new and self.recorder:
            self.recorder.on_event(ev)
        return new

    def on_timer(self, now: int) -> list[tuple[str, BookStatus, BookStatus]]:
        changes = self.state.on_timer(now)
        for sym, old, new in changes:
            self.status_changes.append((now, sym, old, new))
            log.info("book_status", symbol=sym, old=str(old), new=str(new))
        if self.recorder and now - self._last_record >= self.record_interval_ms:
            self._last_record = now
            n = self.ops.record_top_n
            for m in self.state.symbols.values():
                if m.status is BookStatus.OK:
                    bids, asks = m.sync.book.top(n)
                    self.recorder.on_event(BookTopN(m.symbol, m.sync.last_u, bids, asks, now))
        return changes
