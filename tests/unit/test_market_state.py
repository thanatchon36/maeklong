from riverbank.core.events import AggTrade, BookTicker, DepthDiff, DepthSnapshot, MarkPrice, StreamStatus
from riverbank.core.models import BookStatus
from riverbank.marketdata.market_state import MarketState


def diff(U, u, pu, bids=(), asks=(), ts=0, sym="AAAUSDT"):
    return DepthDiff(sym, ts, ts, U, u, pu, list(bids), list(asks), ts)


def make(stale_ms=3000):
    reqs = []
    checks = []
    ms = MarketState(["AAAUSDT"], stale_ms=stale_ms, request_snapshot=lambda s, lim, p: reqs.append((s, lim, p)),
                     on_check=lambda s, ok, d: checks.append((s, ok)), refresh_interval_ms=0)
    return ms, reqs, checks


def sync(ms, ts=1000):
    ms.on_depth(diff(99, 101, 98, [(10.0, 1.0)], [(11.0, 1.0)], ts))
    ms.on_snapshot(DepthSnapshot("AAAUSDT", 100, [(10.0, 2.0), (9.0, 1.0)], [(11.0, 2.0), (12.0, 1.0)], ts))
    assert ms.get("AAAUSDT").sync.synced


def test_snapshot_requested_once_and_status_transitions():
    ms, reqs, _ = make()
    ms.on_depth(diff(99, 101, 98, ts=1000))
    ms.on_depth(diff(102, 103, 101, ts=1050))
    assert reqs == [("AAAUSDT", 1000, "sync")]
    assert ms.on_timer(1100) == []  # still INVALID (not synced)
    ms.on_snapshot(DepthSnapshot("AAAUSDT", 100, [(10.0, 2.0)], [(11.0, 2.0)], 1100))
    assert ms.on_timer(1200) == [("AAAUSDT", BookStatus.INVALID, BookStatus.OK)]
    assert ms.on_timer(1050 + 3001) == [("AAAUSDT", BookStatus.OK, BookStatus.STALE)]
    ms.on_book_ticker(BookTicker("AAAUSDT", 1, 0, 0, 10.0, 1.0, 11.0, 1.0, 5000))
    assert ms.on_timer(5001) == [("AAAUSDT", BookStatus.STALE, BookStatus.OK)]


def test_dedupe_bbo_trade_mark_and_funding_history():
    ms, _, _ = make()
    assert ms.on_book_ticker(BookTicker("AAAUSDT", 5, 0, 0, 1, 1, 2, 1, 0))
    assert not ms.on_book_ticker(BookTicker("AAAUSDT", 5, 0, 0, 1, 1, 2, 1, 0))
    assert not ms.on_book_ticker(BookTicker("AAAUSDT", 4, 0, 0, 1, 1, 2, 1, 0))
    assert ms.on_agg_trade(AggTrade("AAAUSDT", 10, 1.0, 1.0, 1.0, 0, 0, True, 0))
    assert not ms.on_agg_trade(AggTrade("AAAUSDT", 10, 1.0, 1.0, 1.0, 0, 0, True, 0))
    assert ms.on_mark(MarkPrice("AAAUSDT", 1000, 1.0, 1.0, 0.0001, 8 * 3600_000, 0))
    assert not ms.on_mark(MarkPrice("AAAUSDT", 1000, 1.0, 1.0, 0.0001, 8 * 3600_000, 0))
    assert ms.on_mark(MarkPrice("AAAUSDT", 2000, 1.0, 1.0, 0.0001, 16 * 3600_000, 0))
    assert ms.get("AAAUSDT").prev_funding_ms == 8 * 3600_000


def test_public_disconnect_invalidates_books():
    ms, _reqs, _ = make()
    sync(ms)
    ms.on_stream_status(StreamStatus("market", 0, False, ("AAAUSDT",), 2000))
    assert ms.get("AAAUSDT").sync.synced  # market route does not carry depth
    ms.on_stream_status(StreamStatus("public", 0, False, ("AAAUSDT",), 2000))
    assert not ms.get("AAAUSDT").sync.synced


def test_check_matches_and_mismatch_self_heals():
    ms, reqs, checks = make()
    sync(ms)
    assert ms.start_pending("AAAUSDT", "check", 2000)
    assert reqs[-1] == ("AAAUSDT", 100, "check")
    ms.on_depth(diff(102, 104, 101, [(10.0, 3.0)], [], 2100))
    # fresh snapshot at 103 (mid-event): the buffered diff 102..104 bridges it -> same u as main
    ms.on_snapshot(DepthSnapshot("AAAUSDT", 103, [(10.0, 3.0), (9.0, 1.0)], [(11.0, 1.0), (12.0, 1.0)], 2150,
                                 "check"))
    assert checks == [("AAAUSDT", True)]
    ms.on_depth(diff(105, 106, 104, [(9.0, 4.0)], [], 2200))
    # Corrupt the main book (simulates an undetected gap) -> next check mismatches and swaps.
    m = ms.get("AAAUSDT")
    m.sync.book.apply([(9.5, 7.0)], [])
    ms.start_pending("AAAUSDT", "check", 3000)
    ms.on_snapshot(DepthSnapshot("AAAUSDT", 106, [(10.0, 3.0), (9.0, 4.0)], [(11.0, 1.0), (12.0, 1.0)], 3050,
                                 "check"))
    ms.on_depth(diff(107, 108, 106, [], [(13.0, 1.0)], 3100))
    assert checks[-1] == ("AAAUSDT", False)
    assert m.check_mismatches == 1 and ms.get("AAAUSDT").sync.book.qty_at(True, 9.5) == 0.0
