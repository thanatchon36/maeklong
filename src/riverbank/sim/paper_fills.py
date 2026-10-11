"""Paper Fill Simulator (SPEC §5.8) — replaces the order gateway in shadow/replay.

Conservative model:
  * commands take effect after `latency_ms`, against the book state at arrival time
  * post-only (GTX) order that would cross at arrival → rejected (-5022, like Binance)
  * joining level P: queue_ahead = visible qty at P at that moment (0 if P improves the touch)
  * aggTrade THROUGH our price → full fill at our price
  * aggTrade AT our price with the aggressor hitting our side → queue_ahead −= nq
    (non-RPI qty, DISCREPANCIES #10); the excess over queue_ahead fills us
  * visible-qty decreases not explained by trades never advance our queue
  * visible book crossing our resting order (best ask ≤ our bid) → filled at our price
    (adverse fills must not be skipped; OPEN_QUESTIONS B8)
  * maker fee on every passive fill; taker orders (MARKET/IOC) fill at the touch with taker fee
Output: the same `OrderUpdate`/`OrderAck` events a real venue produces, so the order
manager, risk engine and analytics are mode-agnostic.
"""

from __future__ import annotations

from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from riverbank.core.events import AggTrade, OrderAck, OrderUpdate
from riverbank.core.models import Order, OrderStatus, Side
from riverbank.marketdata.market_state import MarketState
from riverbank.orders.manager import CancelCmd, Command, PlaceCmd

ZERO = Decimal(0)


@dataclass(slots=True)
class SimOrder:
    client_id: str
    exchange_id: int
    symbol: str
    side: Side
    price: Decimal
    price_f: float
    qty: Decimal
    filled: Decimal
    queue_ahead: float
    reduce_only: bool
    tif: str

    @property
    def remaining(self) -> Decimal:
        return self.qty - self.filled


class PaperExchange:
    def __init__(self, venue: str, market: MarketState, emit: Callable[[Any], None], *, latency_ms: int = 150,
                 fee_maker_bps: float = 2.0, fee_taker_bps: float = 5.0) -> None:
        self.venue = venue
        self.market = market
        self.emit = emit
        self.latency_ms = latency_ms
        self.maker = Decimal(str(fee_maker_bps)) / Decimal(10_000)
        self.taker = Decimal(str(fee_taker_bps)) / Decimal(10_000)
        self._pending: deque[tuple[int, Command]] = deque()
        self.resting: dict[str, dict[str, SimOrder]] = {}
        self._final: OrderedDict[str, str] = OrderedDict()
        self._next_id = 1
        self._trade_id = 1
        self.fills = 0
        self.post_only_rejects = 0

    # ---- command intake ----

    def submit(self, cmd: Command, now: int) -> None:
        self._pending.append((now + self.latency_ms, cmd))

    def advance(self, now: int) -> None:
        while self._pending and self._pending[0][0] <= now:
            ts, cmd = self._pending.popleft()
            if isinstance(cmd, PlaceCmd):
                self._place(cmd.order, ts)
            else:
                self._cancel(cmd, ts)

    def open_orders(self, symbol: str | None = None) -> list[SimOrder]:
        if symbol is not None:
            return list(self.resting.get(symbol, {}).values())
        return [o for d in self.resting.values() for o in d.values()]

    # ---- market data hooks ----

    def on_trade(self, t: AggTrade) -> None:
        book = self.resting.get(t.symbol)
        if not book:
            return
        for so in list(book.values()):
            if so.side is Side.BUY:
                through = t.price < so.price_f
                at_hit = t.price == so.price_f and t.buyer_maker
            else:
                through = t.price > so.price_f
                at_hit = t.price == so.price_f and not t.buyer_maker
            if through:
                self._fill(so, so.remaining, so.price, True, t.recv_ts)
            elif at_hit:
                so.queue_ahead -= t.nq
                if so.queue_ahead < 0:
                    q = min(so.remaining, Decimal(repr(-so.queue_ahead)))
                    so.queue_ahead = 0.0
                    if q > 0:
                        self._fill(so, q, so.price, True, t.recv_ts)

    def on_book(self, symbol: str, now: int) -> None:
        book = self.resting.get(symbol)
        if not book:
            return
        bb, ba = self._best(symbol)
        for so in list(book.values()):
            if so.side is Side.BUY and ba > 0 and ba <= so.price_f:
                self._fill(so, so.remaining, so.price, True, now)
            elif so.side is Side.SELL and bb > 0 and bb >= so.price_f:
                self._fill(so, so.remaining, so.price, True, now)

    # ---- internals ----

    def _best(self, symbol: str) -> tuple[float, float]:
        m = self.market.get(symbol)
        if m is None:
            return 0.0, 0.0
        if m.bbo is not None:
            return m.bbo.bid_px, m.bbo.ask_px
        b, a = m.sync.book.best_bid(), m.sync.book.best_ask()
        return (b[0] if b else 0.0), (a[0] if a else 0.0)

    def _visible_qty(self, symbol: str, is_bid: bool, px: float) -> float:
        m = self.market.get(symbol)
        if m is None:
            return 0.0
        q = m.sync.book.qty_at(is_bid, px)
        if m.bbo is not None:  # fresher L1 from bookTicker
            if is_bid and m.bbo.bid_px == px:
                q = m.bbo.bid_qty
            elif not is_bid and m.bbo.ask_px == px:
                q = m.bbo.ask_qty
        return q

    def _ack(self, o: Order, action: str, outcome: str, ts: int, status: str = "", code: int | None = None,
             msg: str = "", exchange_id: int | None = None) -> None:
        self.emit(OrderAck(o.client_id, o.symbol, action, outcome, ts, self.venue, exchange_id, status, code, msg))

    def _update(self, so: SimOrder, exec_type: str, status: str, ts: int, last_qty: Decimal = ZERO,
                last_px: Decimal = ZERO, fee: Decimal = ZERO, maker: bool = True) -> None:
        tid = 0
        if exec_type == "TRADE":
            tid = self._trade_id
            self._trade_id += 1
        self.emit(OrderUpdate(so.symbol, so.client_id, so.exchange_id, so.side.value, exec_type, status, so.price,
                              so.qty, so.filled, last_qty, last_px, fee, "USDT", maker, tid, ts, ts, ts, self.venue,
                              so.reduce_only))

    def _place(self, o: Order, ts: int) -> None:
        eid = self._next_id
        self._next_id += 1
        bb, ba = self._best(o.symbol)
        is_buy = o.side is Side.BUY
        if o.order_type == "MARKET" or o.time_in_force == "IOC":
            self._taker(o, eid, ts, bb, ba)
            return
        px = float(o.price)
        crosses = (is_buy and ba > 0 and px >= ba) or (not is_buy and bb > 0 and px <= bb)
        if crosses:
            if o.time_in_force == "GTX":
                self.post_only_rejects += 1
                self._remember(o.client_id, "REJECTED")
                self._ack(o, "place", "rejected", ts, code=-5022,
                          msg="Due to the order could not be executed as maker, the Post Only order will be rejected.")
                return
            self._taker(o, eid, ts, bb, ba)
            return
        so = SimOrder(o.client_id, eid, o.symbol, o.side, o.price, px, o.qty, ZERO,
                      self._visible_qty(o.symbol, is_buy, px), o.reduce_only, o.time_in_force)
        self.resting.setdefault(o.symbol, {})[o.client_id] = so
        self._ack(o, "place", "ok", ts, "NEW", exchange_id=eid)
        self._update(so, "NEW", "NEW", ts)

    def _taker(self, o: Order, eid: int, ts: int, bb: float, ba: float) -> None:
        px_f = ba if o.side is Side.BUY else bb
        so = SimOrder(o.client_id, eid, o.symbol, o.side, o.price, px_f, o.qty, ZERO, 0.0, o.reduce_only,
                      o.time_in_force)
        if px_f <= 0:
            self._remember(o.client_id, "EXPIRED")
            self._ack(o, "place", "ok", ts, "EXPIRED", exchange_id=eid)
            self._update(so, "EXPIRED", "EXPIRED", ts)
            return
        self._ack(o, "place", "ok", ts, "NEW", exchange_id=eid)
        self._update(so, "NEW", "NEW", ts)
        self._fill(so, o.qty, Decimal(repr(px_f)), False, ts)

    def _cancel(self, cmd: CancelCmd, ts: int) -> None:
        book = self.resting.get(cmd.symbol, {})
        so = book.pop(cmd.client_id, None)
        dummy = Order(cmd.client_id, None, cmd.symbol, Side.BUY, ZERO, ZERO, ZERO, OrderStatus.NEW, ts, ts)
        if so is None:
            final = self._final.get(cmd.client_id)
            if final is not None:
                self._ack(dummy, "cancel", "ok", ts, final)
            else:
                self._ack(dummy, "cancel", "not_found", ts, code=-2011, msg="Unknown order sent.")
            return
        self._remember(so.client_id, "CANCELED")
        self._ack(dummy, "cancel", "ok", ts, "CANCELED", exchange_id=so.exchange_id)
        self._update(so, "CANCELED", "CANCELED", ts)

    def _fill(self, so: SimOrder, qty: Decimal, price: Decimal, maker: bool, ts: int) -> None:
        if qty <= 0:
            return
        so.filled += qty
        fee = (price * qty * (self.maker if maker else self.taker))
        done = so.filled >= so.qty
        self.fills += 1
        if done:
            self.resting.get(so.symbol, {}).pop(so.client_id, None)
            self._remember(so.client_id, "FILLED")
        self._update(so, "TRADE", "FILLED" if done else "PARTIALLY_FILLED", ts, qty, price, fee, maker)

    def _remember(self, cid: str, status: str) -> None:
        self._final[cid] = status
        if len(self._final) > 20_000:
            self._final.popitem(last=False)
