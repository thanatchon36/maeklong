"""Local order book + Binance futures snapshot/diff synchronization.

Procedure (developers.binance.com, "How to manage a local order book correctly", USDⓈ-M):
  1. open the diff stream and buffer events
  2. GET /fapi/v1/depth snapshot -> lastUpdateId
  3. drop events with u < lastUpdateId
  4. first processed event must have U <= lastUpdateId <= u
  5. afterwards every event must have pu == previous event's u, otherwise resync from step 2
  6. quantities are absolute; qty 0 removes the level (removing an unknown level is normal)

Additions: events with u <= last applied u are duplicates (e.g. overlapping connections during
a make-before-break roll) and are dropped, so continuity is still checked through `pu`.
Prices are float keys parsed from the exchange strings (exact for equality/ordering of the
same decimal strings). Order prices sent to the exchange are Decimal elsewhere.
"""

from __future__ import annotations

from collections import deque
from enum import StrEnum

from sortedcontainers import SortedDict

from riverbank.core.events import DepthDiff, Levels


class OrderBook:
    __slots__ = ("asks", "bids")

    def __init__(self) -> None:
        self.bids: SortedDict = SortedDict()  # key = -price -> qty (best bid first)
        self.asks: SortedDict = SortedDict()  # key = price -> qty (best ask first)

    def clear(self) -> None:
        self.bids.clear()
        self.asks.clear()

    def load(self, bids: Levels, asks: Levels) -> None:
        self.clear()
        self.apply(bids, asks)

    def apply(self, bids: Levels, asks: Levels) -> None:
        b, a = self.bids, self.asks
        for px, qty in bids:
            if qty == 0.0:
                b.pop(-px, None)
            else:
                b[-px] = qty
        for px, qty in asks:
            if qty == 0.0:
                a.pop(px, None)
            else:
                a[px] = qty

    def best_bid(self) -> tuple[float, float] | None:
        if not self.bids:
            return None
        k, q = self.bids.peekitem(0)
        return -k, q

    def best_ask(self) -> tuple[float, float] | None:
        if not self.asks:
            return None
        return self.asks.peekitem(0)

    def top(self, n: int) -> tuple[Levels, Levels]:
        bids = [(-k, self.bids[k]) for k in self.bids.islice(0, n)]
        asks = [(k, self.asks[k]) for k in self.asks.islice(0, n)]
        return bids, asks

    def qty_at(self, is_bid: bool, px: float) -> float:
        return self.bids.get(-px, 0.0) if is_bid else self.asks.get(px, 0.0)

    def depth(self) -> tuple[int, int]:
        return len(self.bids), len(self.asks)

    def crossed(self) -> bool:
        bb, ba = self.best_bid(), self.best_ask()
        return bb is not None and ba is not None and bb[0] >= ba[0]


class SyncState(StrEnum):
    WAIT_SNAPSHOT = "WAIT_SNAPSHOT"  # buffering diffs, snapshot requested
    WAIT_FIRST = "WAIT_FIRST"  # snapshot loaded, waiting for the bridging event
    SYNCED = "SYNCED"


class BookSync:
    """Per-symbol sync state machine. Pure and synchronous (no I/O).

    The owner requests a REST snapshot whenever `need_snapshot` is True and feeds
    it back with `on_snapshot`.
    """

    MAX_BUFFER = 20_000

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self.book = OrderBook()
        self.state = SyncState.WAIT_SNAPSHOT
        self.last_u = 0
        self.need_snapshot = True
        self.snapshot_requested = False
        self._buffer: deque[DepthDiff] = deque(maxlen=self.MAX_BUFFER)
        self.gaps = 0  # detected continuity breaks
        self.syncs = 0  # successful (re)syncs
        self.stale_snapshots = 0  # snapshot older than buffered stream
        self.duplicates = 0
        self.last_event_ts = 0
        # Price range covered by the last snapshot (levels beyond it may be incomplete).
        self.cover_low = 0.0
        self.cover_high = 0.0

    @property
    def synced(self) -> bool:
        return self.state is SyncState.SYNCED

    def invalidate(self, reason: str = "") -> None:
        """Drop the book (e.g. disconnect) and start over."""
        self.book.clear()
        self.state = SyncState.WAIT_SNAPSHOT
        self.last_u = 0
        self._buffer.clear()
        self.need_snapshot = True
        self.snapshot_requested = False

    def on_diff(self, ev: DepthDiff) -> bool:
        """Feed one diff event. Returns True if the book changed."""
        self.last_event_ts = ev.recv_ts
        if self.state is SyncState.SYNCED:
            if ev.u <= self.last_u:
                self.duplicates += 1
                return False
            if ev.pu != self.last_u:
                self._gap(ev)
                return False
            self.book.apply(ev.bids, ev.asks)
            self.last_u = ev.u
            return True
        if self.state is SyncState.WAIT_FIRST:
            return self._try_first(ev)
        self._buffer.append(ev)
        return False

    def _gap(self, ev: DepthDiff, *, count: bool = True) -> None:
        if count:
            self.gaps += 1
        self.book.clear()
        self.state = SyncState.WAIT_SNAPSHOT
        self.last_u = 0
        self._buffer.clear()
        self._buffer.append(ev)
        self.need_snapshot = True
        self.snapshot_requested = False

    def _try_first(self, ev: DepthDiff) -> bool:
        x = self.last_u  # snapshot lastUpdateId while in WAIT_FIRST
        if ev.u < x:
            return False  # step 3: drop
        # Doc rule: U <= lastUpdateId <= u. Also accept pu == lastUpdateId: the snapshot was taken
        # exactly at the previous event's final id (quiet market), so this event continues it.
        if ev.U <= x <= ev.u or ev.pu == x:
            self.book.apply(ev.bids, ev.asks)
            self.last_u = ev.u
            self.state = SyncState.SYNCED
            self.syncs += 1
            return True
        # ev.U > x: we missed the bridging event -> snapshot is too old relative to the stream
        self.stale_snapshots += 1
        self._gap(ev, count=False)
        return False

    def on_snapshot(self, last_update_id: int, bids: Levels, asks: Levels) -> bool:
        """Load a REST snapshot and replay the buffer. Returns True if now SYNCED."""
        self.snapshot_requested = False
        if self.state is SyncState.SYNCED:
            return True  # late duplicate snapshot; ignore
        self.book.load(bids, asks)
        self.cover_low = min((p for p, _ in bids), default=0.0)
        self.cover_high = max((p for p, _ in asks), default=0.0)
        self.last_u = last_update_id
        self.state = SyncState.WAIT_FIRST
        self.need_snapshot = False
        buffered = list(self._buffer)
        self._buffer.clear()
        for ev in buffered:
            if self.state is SyncState.SYNCED:
                if ev.u <= self.last_u:
                    continue
                if ev.pu != self.last_u:
                    self._gap(ev)
                    return False
                self.book.apply(ev.bids, ev.asks)
                self.last_u = ev.u
            elif self.state is SyncState.WAIT_FIRST:
                self._try_first(ev)
            else:  # a gap happened inside the buffer replay
                self._buffer.append(ev)
        return self.state is SyncState.SYNCED

    def coverage_ok(self, n: int) -> bool:
        """True if the top-n levels lie inside the snapshot's price range."""
        if not self.synced:
            return False
        bids, asks = self.book.top(n)
        if len(bids) < n or len(asks) < n:
            return True  # thin book: nothing better to compare against
        return bids[-1][0] >= self.cover_low and asks[-1][0] <= self.cover_high
