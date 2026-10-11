from decimal import Decimal

from riverbank.core.events import AggTrade, BookTicker, DepthDiff, DepthSnapshot, OrderAck, OrderUpdate
from riverbank.core.models import Order, OrderStatus, Side
from riverbank.marketdata.market_state import MarketState
from riverbank.orders.client_ids import ClientIdAllocator, parse
from riverbank.orders.manager import CancelCmd, OrderManager, PlaceCmd
from riverbank.orders.state_machine import can_transition
from riverbank.quoting.engine import DesiredQuote
from riverbank.risk.limits import Decision
from riverbank.sim.paper_fills import PaperExchange
from tests.helpers import T0, spec

S = OrderStatus
SYM = "AAAUSDT"


def market(bids=((0.9998, 100.0),), asks=((1.0002, 100.0),)):
    ms = MarketState([SYM], stale_ms=10_000, request_snapshot=lambda *a: None, refresh_interval_ms=0)
    ms.on_depth(DepthDiff(SYM, T0, T0, 99, 101, 98, [], [], T0))
    ms.on_snapshot(DepthSnapshot(SYM, 100, list(bids), list(asks), T0))
    ms.on_book_ticker(BookTicker(SYM, 1, T0, T0, bids[0][0], bids[0][1], asks[0][0], asks[0][1], T0))
    return ms


def order(cid="rb-AAAUSDT-BUY-1", side=Side.BUY, px="0.9998", qty="10", tif="GTX", typ="LIMIT"):
    return Order(cid, None, SYM, side, Decimal(px), Decimal(qty), Decimal(0), S.PENDING_NEW, T0, T0,
                 time_in_force=tif, order_type=typ)


def trade(px, qty, buyer_maker, ts=T0 + 1000, nq=None, a=1):
    return AggTrade(SYM, a, px, qty, qty if nq is None else nq, ts, ts, buyer_maker, ts)


def paper(ms, latency=150):
    out = []
    return PaperExchange("paper", ms, out.append, latency_ms=latency, fee_maker_bps=2.0, fee_taker_bps=5.0), out


def fills(out):
    return [e for e in out if isinstance(e, OrderUpdate) and e.exec_type == "TRADE"]


# ---------------- paper fill simulator ----------------

def test_latency_and_queue_position_then_fill_at_price():
    ms = market()
    px, out = paper(ms)
    px.submit(PlaceCmd(order(), "paper"), T0)
    px.advance(T0 + 100)
    assert out == []  # not yet arrived (150 ms latency)
    px.advance(T0 + 150)
    so = px.open_orders(SYM)[0]
    assert so.queue_ahead == 100.0  # joined behind visible qty at 0.9998
    px.on_trade(trade(0.9998, 60, True, a=1))  # seller hits bid: queue 100 -> 40
    assert not fills(out) and so.queue_ahead == 40.0
    px.on_trade(trade(0.9998, 45, True, a=2))  # 5 beyond the queue fill us partially
    f = fills(out)
    assert len(f) == 1 and f[0].last_fill_qty == Decimal(5) and f[0].status == "PARTIALLY_FILLED"
    assert f[0].fee == Decimal("0.9998") * 5 * Decimal("0.0002") and f[0].is_maker


def test_buyer_aggressor_at_bid_price_does_not_fill_bid():
    ms = market()
    px, out = paper(ms, latency=0)
    px.submit(PlaceCmd(order(), "paper"), T0)
    px.advance(T0)
    px.on_trade(trade(0.9998, 500, False))
    assert not fills(out) and px.open_orders(SYM)[0].queue_ahead == 100.0


def test_trade_through_fills_fully():
    ms = market()
    px, out = paper(ms, latency=0)
    px.submit(PlaceCmd(order(), "paper"), T0)
    px.advance(T0)
    px.on_trade(trade(0.9997, 1, True))
    f = fills(out)
    assert len(f) == 1 and f[0].last_fill_qty == 10 and f[0].last_fill_price == Decimal("0.9998")
    assert f[0].status == "FILLED" and px.open_orders(SYM) == []


def test_rpi_quantity_does_not_advance_queue():
    ms = market()
    px, out = paper(ms, latency=0)
    px.submit(PlaceCmd(order(), "paper"), T0)
    px.advance(T0)
    px.on_trade(trade(0.9998, 150, True, nq=20))  # 130 of the print hit hidden RPI orders
    assert not fills(out) and px.open_orders(SYM)[0].queue_ahead == 80.0


def test_unexplained_decrease_does_not_advance_queue():
    ms = market()
    px, _out = paper(ms, latency=0)
    px.submit(PlaceCmd(order(), "paper"), T0)
    px.advance(T0)
    ms.on_depth(DepthDiff(SYM, T0, T0, 102, 103, 101, [(0.9998, 10.0)], [], T0 + 10))  # cancels at our level
    px.on_book(SYM, T0 + 10)
    assert px.open_orders(SYM)[0].queue_ahead == 100.0


def test_improving_price_has_empty_queue():
    ms = market()
    px, out = paper(ms, latency=0)
    px.submit(PlaceCmd(order(px="0.9999"), "paper"), T0)
    px.advance(T0)
    assert px.open_orders(SYM)[0].queue_ahead == 0.0
    px.on_trade(trade(0.9999, 3, True))
    assert fills(out)[0].last_fill_qty == 3


def test_post_only_rejected_if_crossing_at_arrival():
    ms = market()
    px, out = paper(ms, latency=150)
    px.submit(PlaceCmd(order(px="1.0000"), "paper"), T0)
    # market moves before arrival: ask drops to 0.9999 -> our 1.0000 bid would cross
    ms.on_book_ticker(BookTicker(SYM, 2, T0, T0, 0.9997, 5, 0.9999, 5, T0 + 100))
    px.advance(T0 + 150)
    acks = [e for e in out if isinstance(e, OrderAck)]
    assert acks[0].outcome == "rejected" and acks[0].code == -5022 and px.post_only_rejects == 1


def test_book_crossing_resting_order_fills_adversely():
    ms = market()
    px, out = paper(ms, latency=0)
    px.submit(PlaceCmd(order(px="0.9998"), "paper"), T0)
    px.advance(T0)
    ms.on_book_ticker(BookTicker(SYM, 3, T0, T0, 0.9995, 5, 0.9997, 5, T0 + 50))
    px.on_book(SYM, T0 + 50)
    assert fills(out)[0].last_fill_qty == 10


def test_cancel_and_cancel_after_fill():
    ms = market()
    px, out = paper(ms, latency=0)
    px.submit(PlaceCmd(order(), "paper"), T0)
    px.advance(T0)
    px.submit(CancelCmd(SYM, "rb-AAAUSDT-BUY-1", "paper"), T0)
    px.advance(T0)
    assert [e.status for e in out if isinstance(e, OrderUpdate)][-1] == "CANCELED"
    px.submit(PlaceCmd(order(cid="rb-AAAUSDT-BUY-2"), "paper"), T0)
    px.advance(T0)
    px.on_trade(trade(0.9990, 1, True))
    px.submit(CancelCmd(SYM, "rb-AAAUSDT-BUY-2", "paper"), T0)
    px.advance(T0)
    ack = [e for e in out if isinstance(e, OrderAck) and e.action == "cancel"][-1]
    assert ack.outcome == "ok" and ack.status == "FILLED"
    px.submit(CancelCmd(SYM, "rb-AAAUSDT-BUY-999", "paper"), T0)
    px.advance(T0)
    assert [e for e in out if isinstance(e, OrderAck)][-1].outcome == "not_found"


def test_market_order_takes_touch_with_taker_fee():
    ms = market()
    px, out = paper(ms, latency=0)
    px.submit(PlaceCmd(order(side=Side.SELL, px="0", tif="IOC", typ="MARKET"), "paper"), T0)
    px.advance(T0)
    f = fills(out)[0]
    assert f.last_fill_price == Decimal("0.9998") and not f.is_maker
    assert f.fee == Decimal("0.9998") * 10 * Decimal("0.0005")


# ---------------- state machine / client ids ----------------

def test_state_machine_table():
    assert can_transition(S.PENDING_NEW, S.NEW)
    assert can_transition(S.PENDING_NEW, S.UNKNOWN)
    assert can_transition(S.UNKNOWN, S.FILLED)
    assert can_transition(S.PENDING_CANCEL, S.FILLED)
    assert can_transition(S.PARTIALLY_FILLED, S.PARTIALLY_FILLED)
    assert not can_transition(S.FILLED, S.NEW)
    assert not can_transition(S.CANCELED, S.PENDING_CANCEL)
    assert not can_transition(S.NEW, S.PENDING_NEW)


def test_client_ids_unique_and_parseable():
    reserved = []

    def reserve(n):
        start = 1 + len(reserved) * n
        reserved.append(start)
        return start

    ids = ClientIdAllocator(reserve, block=3)
    got = [ids.next("DOGEUSDT", Side.BUY) for _ in range(7)]
    assert len(set(got)) == 7 and len(reserved) == 3
    sym, side, seq = parse(got[0])
    assert (sym, side, seq) == ("DOGEUSDT", Side.BUY, 1)
    assert all(len(c) <= 36 for c in got)


# ---------------- order manager ----------------

class Approve:
    def __init__(self, approve=True):
        self.approve = approve

    def check(self, intent, mark, now):
        return Decision(self.approve, "" if self.approve else "nope")


def manager(approve=True):
    sent = []
    om = OrderManager("paper", "shadow", ClientIdAllocator(), Approve(approve), sent.append, requote_ticks=1)
    return om, sent


def upd(o, status, exec_type, filled="0", last="0", px="0", tid=0, ts=T0 + 10):
    return OrderUpdate(SYM, o.client_id, 7, o.side.value, exec_type, status, o.price, o.qty, Decimal(filled),
                       Decimal(last), Decimal(px), Decimal("0.001"), "USDT", True, tid, ts, ts, ts, "paper")


def test_sync_places_requotes_by_cancel_then_new():
    om, sent = manager()
    sp = spec()
    om.sync_quotes(SYM, [DesiredQuote(Side.BUY, 0, Decimal("0.9990"), Decimal(10))], sp, T0, 1.0)
    assert len(sent) == 1 and isinstance(sent[0], PlaceCmd)
    o = sent[0].order
    om.sync_quotes(SYM, [DesiredQuote(Side.BUY, 0, Decimal("0.9980"), Decimal(10))], sp, T0, 1.0)
    assert len(sent) == 1  # PENDING_NEW: wait for the exchange
    om.on_update(upd(o, "NEW", "NEW"))
    om.sync_quotes(SYM, [DesiredQuote(Side.BUY, 0, Decimal("0.9990"), Decimal(10))], sp, T0, 1.0)
    assert len(sent) == 1  # same price: keep queue position
    om.sync_quotes(SYM, [DesiredQuote(Side.BUY, 0, Decimal("0.9980"), Decimal(10))], sp, T0, 1.0)
    assert isinstance(sent[-1], CancelCmd) and o.status is S.PENDING_CANCEL
    om.sync_quotes(SYM, [DesiredQuote(Side.BUY, 0, Decimal("0.9980"), Decimal(10))], sp, T0, 1.0)
    assert len(sent) == 2  # never two live orders in one slot
    om.on_update(upd(o, "CANCELED", "CANCELED"))
    om.sync_quotes(SYM, [DesiredQuote(Side.BUY, 0, Decimal("0.9980"), Decimal(10))], sp, T0, 1.0)
    assert isinstance(sent[-1], PlaceCmd) and sent[-1].order.price == Decimal("0.9980")


def test_stream_before_ack_and_late_ack_does_not_regress():
    om, _sent = manager()
    o = om.place(SYM, Side.BUY, Decimal("0.999"), Decimal(10), now=T0, mark=1.0)
    om.on_update(upd(o, "FILLED", "TRADE", filled="10", last="10", px="0.999", tid=1))
    assert o.status is S.FILLED
    om.on_ack(OrderAck(o.client_id, SYM, "place", "ok", T0 + 20, "paper", 7, "NEW"))
    assert o.status is S.FILLED  # REST hint arriving late is ignored


def test_unknown_path_and_resolution():
    om, sent = manager()
    unknown = []
    om.unknown_listeners.append(unknown.append)
    o = om.place(SYM, Side.SELL, Decimal("1.001"), Decimal(10), now=T0, mark=1.0)
    om.on_ack(OrderAck(o.client_id, SYM, "place", "unknown", T0 + 5, "paper", msg="503 Unknown error"))
    assert o.status is S.UNKNOWN and unknown == [o]
    om.sync_quotes(SYM, [], spec(), T0, 1.0)
    assert len(sent) == 1  # UNKNOWN orders are never resent/cancelled blindly by the quoting loop
    om.on_update(upd(o, "NEW", "NEW"))
    assert o.status is S.NEW


def test_fill_during_pending_cancel_and_dedupe():
    om, _sent = manager()
    got = []
    om.fill_listeners.append(lambda f, o: got.append(f))
    o = om.place(SYM, Side.BUY, Decimal("0.999"), Decimal(10), now=T0, mark=1.0)
    om.on_update(upd(o, "NEW", "NEW"))
    om.cancel(o, T0)
    om.on_update(upd(o, "PARTIALLY_FILLED", "TRADE", filled="4", last="4", px="0.999", tid=5))
    om.on_update(upd(o, "PARTIALLY_FILLED", "TRADE", filled="4", last="4", px="0.999", tid=5))  # duplicate
    assert o.status is S.PENDING_CANCEL and o.filled_qty == 4 and len(got) == 1
    om.on_update(upd(o, "CANCELED", "CANCELED", filled="4"))
    assert o.status is S.CANCELED and om.slot_order(SYM, Side.BUY, 0) is None


def test_post_only_reject_and_cancel_not_found():
    om, _sent = manager()
    unknown = []
    om.unknown_listeners.append(unknown.append)
    o = om.place(SYM, Side.BUY, Decimal("1.0"), Decimal(10), now=T0, mark=1.0)
    om.on_ack(OrderAck(o.client_id, SYM, "place", "rejected", T0, "paper", code=-5022, msg="post only"))
    assert o.status is S.REJECTED and o.reject_reason == "post_only" and om.post_only_rejects == 1
    o2 = om.place(SYM, Side.BUY, Decimal("0.99"), Decimal(10), now=T0, mark=1.0)
    om.on_update(upd(o2, "NEW", "NEW"))
    om.cancel(o2, T0)
    om.on_ack(OrderAck(o2.client_id, SYM, "cancel", "not_found", T0, "paper", code=-2011))
    assert o2.status is S.UNKNOWN and unknown == [o2]


def test_risk_rejection_places_nothing():
    om, sent = manager(approve=False)
    assert om.place(SYM, Side.BUY, Decimal("0.99"), Decimal(10), now=T0, mark=1.0) is None
    assert sent == [] and om.rejects == {"nope": 1}
