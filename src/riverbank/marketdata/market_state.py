"""Per-symbol market state: book sync, BBO, last trade id, mark/funding, staleness.

Synchronous; fed by the engine from the inbound event queue. Snapshot fetching is
delegated to `request_snapshot(symbol, limit, purpose)` which must eventually feed a
`DepthSnapshot` event back.

Book self-check / refresh (OPEN_QUESTIONS B15): a second BookSync is built from a new
snapshot while the main book keeps serving; both receive the same diffs, and once the
fresh book reaches the main book's update id their L1–L5 are compared exactly. For
`refresh` the fresh book then replaces the main one (no INVALID window).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from riverbank.core.events import AggTrade, BookTicker, DepthDiff, DepthSnapshot, MarkPrice, StreamStatus
from riverbank.core.models import BookStatus
from riverbank.marketdata.orderbook import BookSync

SnapshotRequester = Callable[[str, int, str], None]
CheckListener = Callable[[str, bool, str], None]

SNAPSHOT_TIMEOUT_MS = 30_000


@dataclass(slots=True)
class SymbolMarket:
    symbol: str
    sync: BookSync
    bbo: BookTicker | None = None
    last_agg_id: int = 0
    last_trade: AggTrade | None = None
    mark: MarkPrice | None = None
    prev_funding_ms: int = 0
    last_md_ts: int = 0
    stale: bool = True
    status: BookStatus = BookStatus.INVALID
    snapshot_req_ts: int = 0
    pending: BookSync | None = None
    pending_purpose: str = ""
    pending_req_ts: int = 0
    checks: int = 0
    check_matches: int = 0
    check_mismatches: int = 0
    mismatch_details: list[str] = field(default_factory=list)
    last_refresh_ts: int = 0


class MarketState:
    def __init__(
        self,
        symbols: list[str] | tuple[str, ...],
        *,
        stale_ms: int,
        request_snapshot: SnapshotRequester,
        snapshot_limit: int = 1000,
        check_levels: int = 5,
        refresh_interval_ms: int = 30 * 60 * 1000,
        on_check: CheckListener | None = None,
    ) -> None:
        self.stale_ms = stale_ms
        self._request = request_snapshot
        self.snapshot_limit = snapshot_limit
        self.check_levels = check_levels
        self.refresh_interval_ms = refresh_interval_ms
        self.on_check = on_check
        self.symbols: dict[str, SymbolMarket] = {}
        for s in symbols:
            self.add_symbol(s)

    def add_symbol(self, symbol: str) -> None:
        if symbol not in self.symbols:
            self.symbols[symbol] = SymbolMarket(symbol, BookSync(symbol))

    def remove_symbol(self, symbol: str) -> None:
        self.symbols.pop(symbol, None)

    def get(self, symbol: str) -> SymbolMarket | None:
        return self.symbols.get(symbol)

    def status(self, symbol: str) -> BookStatus:
        m = self.symbols.get(symbol)
        return m.status if m else BookStatus.INVALID

    # ---- market data ----

    def on_depth(self, ev: DepthDiff) -> bool:
        m = self.symbols.get(ev.symbol)
        if m is None:
            return False
        m.last_md_ts = max(m.last_md_ts, ev.recv_ts)
        was_synced = m.sync.synced
        changed = m.sync.on_diff(ev)
        if was_synced and not m.sync.synced:
            m.pending = None  # a gap invalidates any check in progress
        if m.pending is not None:
            m.pending.on_diff(ev)
            self._maybe_finish_pending(m)
        self._request_if_needed(m, ev.recv_ts)
        return changed

    def on_snapshot(self, ev: DepthSnapshot) -> bool:
        m = self.symbols.get(ev.symbol)
        if m is None:
            return False
        if ev.purpose == "sync":
            m.snapshot_req_ts = 0
            ok = m.sync.on_snapshot(ev.last_update_id, ev.bids, ev.asks)
            if ok:
                m.last_refresh_ts = ev.recv_ts
            self._request_if_needed(m, ev.recv_ts)
            return ok
        if m.pending is None or not m.sync.synced:
            return False
        m.pending.on_snapshot(ev.last_update_id, ev.bids, ev.asks)
        self._maybe_finish_pending(m)
        return True

    def on_book_ticker(self, ev: BookTicker) -> bool:
        m = self.symbols.get(ev.symbol)
        if m is None or (m.bbo is not None and ev.u <= m.bbo.u):
            return False
        m.bbo = ev
        m.last_md_ts = max(m.last_md_ts, ev.recv_ts)
        return True

    def on_agg_trade(self, ev: AggTrade) -> bool:
        m = self.symbols.get(ev.symbol)
        if m is None or ev.a <= m.last_agg_id:
            return False
        m.last_agg_id = ev.a
        m.last_trade = ev
        return True

    def on_mark(self, ev: MarkPrice) -> bool:
        m = self.symbols.get(ev.symbol)
        if m is None or (m.mark is not None and ev.E <= m.mark.E):
            return False
        if m.mark is not None and ev.next_funding_ms != m.mark.next_funding_ms:
            m.prev_funding_ms = m.mark.next_funding_ms
        m.mark = ev
        return True

    def on_stream_status(self, ev: StreamStatus) -> list[str]:
        """Unexpected public-stream disconnect: books of that shard become INVALID now."""
        if ev.connected or ev.route != "public":
            return []
        out = []
        for s in ev.symbols:
            m = self.symbols.get(s)
            if m is not None:
                m.sync.invalidate("disconnect")
                m.pending = None
                out.append(s)
        return out

    # ---- periodic ----

    def on_timer(self, now: int) -> list[tuple[str, BookStatus, BookStatus]]:
        changes = []
        for m in self.symbols.values():
            m.stale = (now - m.last_md_ts) > self.stale_ms
            new = BookStatus.INVALID if not m.sync.synced else (BookStatus.STALE if m.stale else BookStatus.OK)
            if new is not m.status:
                changes.append((m.symbol, m.status, new))
                m.status = new
            self._request_if_needed(m, now)
            if m.sync.synced and m.last_refresh_ts == 0:
                m.last_refresh_ts = now  # first sync completed via a live bridging event
            if m.pending is not None and now - m.pending_req_ts > 4 * SNAPSHOT_TIMEOUT_MS:
                m.pending = None  # abandon a check that never completed
            if (m.sync.synced and m.pending is None and self.refresh_interval_ms > 0
                    and (now - m.last_refresh_ts > self.refresh_interval_ms
                         or not m.sync.coverage_ok(self.check_levels))):
                self.start_pending(m.symbol, "refresh", now)
        return changes

    def start_pending(self, symbol: str, purpose: str, now: int, limit: int | None = None) -> bool:
        m = self.symbols.get(symbol)
        if m is None or not m.sync.synced or m.pending is not None:
            return False
        m.pending = BookSync(symbol)
        m.pending.snapshot_requested = True
        m.pending_purpose = purpose
        m.pending_req_ts = now
        if purpose == "refresh":
            m.last_refresh_ts = now
        self._request(symbol, limit or (self.snapshot_limit if purpose == "refresh" else 100), purpose)
        return True

    def _request_if_needed(self, m: SymbolMarket, now: int) -> None:
        s = m.sync
        if not s.need_snapshot:
            return
        if s.snapshot_requested and now - m.snapshot_req_ts < SNAPSHOT_TIMEOUT_MS:
            return
        s.snapshot_requested = True
        m.snapshot_req_ts = now
        self._request(m.symbol, self.snapshot_limit, "sync")

    def _maybe_finish_pending(self, m: SymbolMarket) -> None:
        p = m.pending
        if p is None or not p.synced or p.last_u != m.sync.last_u:
            return
        n = self.check_levels
        main_top = m.sync.book.top(n)
        fresh_top = p.book.top(n)
        match = main_top == fresh_top
        m.checks += 1
        detail = ""
        if match:
            m.check_matches += 1
        else:
            m.check_mismatches += 1
            detail = f"u={p.last_u} main={main_top} fresh={fresh_top}"
            if len(m.mismatch_details) < 20:
                m.mismatch_details.append(detail)
        if self.on_check is not None:
            self.on_check(m.symbol, match, detail)
        if m.pending_purpose == "refresh" or not match:
            # Self-heal: the freshly synced book replaces the long-running one.
            p.gaps, p.syncs = m.sync.gaps, m.sync.syncs + 1
            p.duplicates, p.stale_snapshots = m.sync.duplicates, m.sync.stale_snapshots
            m.sync = p
        m.pending = None
