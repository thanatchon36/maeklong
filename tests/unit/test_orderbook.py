import random

from hypothesis import given, settings
from hypothesis import strategies as st

from riverbank.core.events import DepthDiff
from riverbank.marketdata.orderbook import BookSync, OrderBook, SyncState


def diff(U, u, pu, bids=(), asks=(), sym="X"):
    return DepthDiff(sym, 0, 0, U, u, pu, list(bids), list(asks), 0)


def test_orderbook_basic():
    b = OrderBook()
    b.load([(10.0, 1.0), (9.0, 2.0)], [(11.0, 3.0), (12.0, 4.0)])
    assert b.best_bid() == (10.0, 1.0) and b.best_ask() == (11.0, 3.0)
    b.apply([(10.0, 0.0), (9.5, 5.0)], [(11.0, 0.0), (13.0, 1.0)])
    assert b.top(2) == ([(9.5, 5.0), (9.0, 2.0)], [(12.0, 4.0), (13.0, 1.0)])
    b.apply([(1.0, 0.0)], [])  # removing an unknown level is normal
    assert b.qty_at(True, 9.0) == 2.0 and b.qty_at(False, 9.0) == 0.0
    assert not b.crossed()


def test_sync_procedure_happy_path():
    s = BookSync("X")
    assert s.need_snapshot
    s.on_diff(diff(90, 95, 80, [(1.0, 1.0)]))  # too old -> dropped at snapshot
    s.on_diff(diff(96, 105, 95, [(2.0, 2.0)]))  # bridges snapshot 100
    s.on_diff(diff(106, 110, 105, [(3.0, 3.0)]))
    assert s.on_snapshot(100, [(1.5, 1.0)], [(9.0, 1.0)])
    assert s.synced and s.last_u == 110
    assert s.book.top(5)[0] == [(3.0, 3.0), (2.0, 2.0), (1.5, 1.0)]


def test_first_event_after_snapshot_live():
    s = BookSync("X")
    assert not s.on_snapshot(100, [(1.0, 1.0)], [(2.0, 1.0)])
    assert s.state is SyncState.WAIT_FIRST
    s.on_diff(diff(90, 99, 85))  # u < lastUpdateId -> drop
    assert s.state is SyncState.WAIT_FIRST
    s.on_diff(diff(100, 101, 99, [(1.0, 5.0)]))
    assert s.synced and s.book.best_bid() == (1.0, 5.0)


def test_snapshot_too_old_triggers_resync():
    s = BookSync("X")
    s.on_diff(diff(200, 210, 190))
    assert not s.on_snapshot(100, [], [])  # first buffered U=200 > 100: missed events
    assert s.need_snapshot and s.stale_snapshots == 1
    assert s.on_snapshot(205, [(1.0, 1.0)], [(2.0, 1.0)])  # buffer kept the event -> bridges now


def test_gap_detected_and_duplicates_dropped():
    s = BookSync("X")
    s.on_snapshot(100, [(1.0, 1.0)], [(2.0, 1.0)])
    s.on_diff(diff(99, 101, 98))
    s.on_diff(diff(102, 104, 101))
    assert not s.on_diff(diff(102, 104, 101))  # duplicate (e.g. overlapping connections)
    assert s.duplicates == 1 and s.synced
    s.on_diff(diff(110, 112, 108))  # pu != 104 -> gap
    assert not s.synced and s.need_snapshot and s.gaps == 1
    assert s.book.depth() == (0, 0)


def test_invalidate_resets():
    s = BookSync("X")
    s.on_snapshot(100, [(1.0, 1.0)], [(2.0, 1.0)])
    s.on_diff(diff(99, 101, 98))
    s.invalidate()
    assert not s.synced and s.need_snapshot and s.book.depth() == (0, 0)


# ---- property test: the synced book is never wrong ----

class TrueMarket:
    """Ground truth: a book changed by single updates with non-contiguous ids."""

    def __init__(self, rng: random.Random):
        self.rng = rng
        self.book: dict[tuple[bool, float], float] = {}
        self.uid = 1000
        self.history: dict[int, dict] = {self.uid: {}}
        self.events: list[DepthDiff] = []
        self.event_end_ids: list[int] = []

    def gen_event(self) -> DepthDiff:
        changed: dict[tuple[bool, float], float] = {}
        first = self.uid + 1
        for _ in range(self.rng.randint(1, 4)):
            self.uid += self.rng.randint(1, 3)
            if not changed:
                first = self.uid
            is_bid = self.rng.random() < 0.5
            px = float(self.rng.randint(1, 20) if is_bid else self.rng.randint(21, 40))
            qty = 0.0 if self.rng.random() < 0.3 else float(self.rng.randint(1, 9))
            if qty == 0.0:
                self.book.pop((is_bid, px), None)
            else:
                self.book[(is_bid, px)] = qty
            changed[(is_bid, px)] = qty
            self.history[self.uid] = dict(self.book)
        pu = self.event_end_ids[-1] if self.event_end_ids else 1000
        ev = diff(first, self.uid, pu, [(p, q) for (b, p), q in changed.items() if b],
                  [(p, q) for (b, p), q in changed.items() if not b])
        self.events.append(ev)
        self.event_end_ids.append(self.uid)
        return ev

    def state_at(self, uid: int) -> dict:
        ids = [i for i in self.history if i <= uid]
        return self.history[max(ids)]


def book_dict(b: OrderBook) -> dict:
    out = {(True, -k): q for k, q in b.bids.items()}
    out.update({(False, k): q for k, q in b.asks.items()})
    return out


@settings(max_examples=150, deadline=None)
@given(st.integers(min_value=0, max_value=10_000), st.floats(0.0, 0.15), st.floats(0.0, 0.15))
def test_property_synced_book_matches_truth(seed, p_drop, p_dup):
    rng = random.Random(seed)
    m = TrueMarket(rng)
    s = BookSync("X")
    pending_snapshot_at: int | None = None
    for i in range(300):
        ev = m.gen_event()
        r = rng.random()
        if r < p_drop:
            pass  # lost on the wire
        else:
            s.on_diff(ev)
            if rng.random() < p_dup:
                s.on_diff(ev)
        if s.need_snapshot and not s.snapshot_requested:
            s.snapshot_requested = True
            pending_snapshot_at = i + rng.randint(0, 3)
        if pending_snapshot_at is not None and i >= pending_snapshot_at:
            # REST snapshot taken at an arbitrary single-update id, possibly mid-event or ahead.
            lo = m.event_end_ids[max(0, len(m.event_end_ids) - 3)]
            candidates = [k for k in m.history if k >= lo]
            x = rng.choice(candidates)
            st_ = m.state_at(x)
            s.on_snapshot(x, [(p, q) for (b, p), q in st_.items() if b], [(p, q) for (b, p), q in st_.items() if not b])
            pending_snapshot_at = None
        if s.synced:
            assert book_dict(s.book) == m.state_at(s.last_u), f"wrong book at u={s.last_u}"
