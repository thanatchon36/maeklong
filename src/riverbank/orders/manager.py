"""Order Manager (SPEC §5.5) — venue-agnostic part.

* One order per (symbol, side, level) slot. Requote = cancel, wait until the cancel is
  confirmed (slot empty), then place the new price (OPEN_QUESTIONS B3).
* Every new order passes `risk.check(intent)`; cancels are never blocked.
* The stream (`OrderUpdate`) is the source of truth; REST outcomes (`OrderAck`) are hints.
* UNKNOWN orders are never resent; the venue gateway resolves them (query by
  origClientOrderId) and feeds the result back as `OrderUpdate`/`OrderAck`.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from riverbank.core.events import OrderAck, OrderUpdate
from riverbank.core.models import Fill, Order, OrderStatus, QuoteIntent, Side, SymbolSpec
from riverbank.orders.client_ids import ClientIdAllocator, is_ours
from riverbank.orders.state_machine import can_transition, exchange_status
from riverbank.quoting.engine import DesiredQuote

S = OrderStatus


@dataclass(frozen=True, slots=True)
class PlaceCmd:
    order: Order
    venue: str


@dataclass(frozen=True, slots=True)
class CancelCmd:
    symbol: str
    client_id: str
    venue: str
    priority: bool = True


Command = PlaceCmd | CancelCmd


class RiskDecision(Protocol):
    approved: bool
    reason: str


class RiskCheck(Protocol):
    def check(self, intent: QuoteIntent, mark: float, now: int) -> RiskDecision: ...


class Repo(Protocol):
    def upsert_order(self, order: Order, venue: str, mode: str) -> None: ...
    def insert_fill(self, fill: Fill, venue: str) -> None: ...


FillListener = Callable[[Fill, Order], None]


class OrderManager:
    def __init__(
        self,
        venue: str,
        mode: str,
        ids: ClientIdAllocator,
        risk: RiskCheck,
        submit: Callable[[Command], None],
        *,
        mid_fn: Callable[[str], float] | None = None,
        repo: Repo | None = None,
        requote_ticks: int = 1,
        min_requote_interval_ms: int = 0,
    ) -> None:
        self.venue = venue
        self.mode = mode
        self.ids = ids
        self.risk = risk
        self.submit = submit
        self.mid_fn = mid_fn or (lambda s: 0.0)
        self.repo = repo
        self.requote_ticks = requote_ticks
        self.min_requote_interval_ms = min_requote_interval_ms
        self.orders: dict[str, Order] = {}
        self._open: dict[str, Order] = {}  # index of non-terminal orders (hot path)
        self.slots: dict[tuple[str, Side, int], str] = {}
        self._fills_seen: OrderedDict[tuple[str, int], None] = OrderedDict()
        self.fill_listeners: list[FillListener] = []
        self.unknown_listeners: list[Callable[[Order], None]] = []
        self.rejects: dict[str, int] = {}
        self.post_only_rejects = 0
        self.placed = 0
        self.canceled = 0

    # ---- views ----

    def live_orders(self, symbol: str | None = None) -> list[Order]:
        if symbol is None:
            return list(self._open.values())
        return [o for o in self._open.values() if o.symbol == symbol]

    def open_count(self) -> int:
        return len(self._open)

    def slot_order(self, symbol: str, side: Side, level: int) -> Order | None:
        cid = self.slots.get((symbol, side, level))
        return self.orders.get(cid) if cid else None

    def has_two_sided(self, symbol: str) -> bool:
        sides = {o.side for o in self.live_orders(symbol) if o.status in (S.NEW, S.PARTIALLY_FILLED)}
        return len(sides) == 2

    # ---- quoting ----

    def sync_quotes(self, symbol: str, desired: Iterable[DesiredQuote], spec: SymbolSpec, now: int,
                    mark: float, reason: str = "quote") -> None:
        want = {(d.side, d.level): d for d in desired}
        slots = {(side, lvl) for (sym, side, lvl) in self.slots if sym == symbol} | set(want)
        min_move = spec.tick * self.requote_ticks
        for side, level in sorted(slots, key=lambda x: (x[0].value, x[1])):
            live = self.slot_order(symbol, side, level)
            d = want.get((side, level))
            if live is None or not live.is_open:
                if live is not None:
                    self._release(live)
                if d is not None:
                    self.place(symbol, side, d.price, d.qty, level=level, reduce_only=d.reduce_only, now=now,
                               mark=mark, reason=reason)
                continue
            if live.status in (S.PENDING_NEW, S.PENDING_CANCEL, S.UNKNOWN):
                continue  # wait for the exchange
            if d is None:
                self.cancel(live, now, "pulled")
            elif d.qty != live.qty or d.reduce_only != live.reduce_only:
                self.cancel(live, now, "requote")
            elif abs(d.price - live.price) >= min_move:
                # Protective moves (quote too aggressive vs desired) are immediate; moves toward the
                # touch wait until the order is `min_requote_interval_ms` old (limits churn / UFR).
                protective = d.price < live.price if side is Side.BUY else d.price > live.price
                if protective or now - live.ts_created >= self.min_requote_interval_ms:
                    self.cancel(live, now, "requote")

    def place(self, symbol: str, side: Side, price: Decimal, qty: Decimal, *, level: int = 0,
              reduce_only: bool = False, tif: str = "GTX", order_type: str = "LIMIT", reason: str = "quote",
              now: int, mark: float) -> Order | None:
        cid = self.ids.next(symbol, side)
        intent = QuoteIntent(symbol, side, price, qty, cid, reason, reduce_only, tif, order_type, self.venue, level)
        decision = self.risk.check(intent, mark, now)
        if not decision.approved:
            self.rejects[decision.reason] = self.rejects.get(decision.reason, 0) + 1
            return None
        order = Order(cid, None, symbol, side, price, qty, Decimal(0), S.PENDING_NEW, now, now,
                      reduce_only=reduce_only, time_in_force=tif, order_type=order_type, reason=reason, level=level)
        self.orders[cid] = order
        self._open[cid] = order
        if order_type == "LIMIT" and reason != "flatten" and reason != "hedge":
            self.slots[(symbol, side, level)] = cid
        self.placed += 1
        self._persist(order)
        self.submit(PlaceCmd(order, self.venue))
        return order

    def cancel(self, order: Order, now: int, reason: str = "") -> None:
        if not order.is_open or order.status is S.PENDING_CANCEL:
            return
        self._set(order, S.PENDING_CANCEL, now)
        self.canceled += 1
        self.submit(CancelCmd(order.symbol, order.client_id, self.venue))

    def cancel_all(self, now: int, symbol: str | None = None, reason: str = "") -> int:
        n = 0
        for o in self.live_orders(symbol):
            if o.status in (S.NEW, S.PARTIALLY_FILLED, S.PENDING_NEW, S.UNKNOWN):
                # PENDING_NEW/UNKNOWN: cancel by client id too — the order may exist.
                self._set(o, S.PENDING_CANCEL, now)
                self.submit(CancelCmd(o.symbol, o.client_id, self.venue))
                n += 1
        return n

    # ---- feedback ----

    def on_update(self, ev: OrderUpdate) -> Fill | None:
        order = self.orders.get(ev.client_id)
        if order is None:
            if not is_ours(ev.client_id):
                return None
            order = Order(ev.client_id, ev.exchange_id, ev.symbol, Side(ev.side), ev.price, ev.qty, Decimal(0),
                          S.UNKNOWN, ev.recv_ts, ev.recv_ts, reduce_only=ev.reduce_only, reason="adopted")
            self.orders[ev.client_id] = order
            self._open[ev.client_id] = order
        if ev.exchange_id is not None:
            order.exchange_id = ev.exchange_id
        fill: Fill | None = None
        if ev.exec_type == "TRADE" and ev.last_fill_qty > 0:
            key = (ev.client_id, ev.trade_id)
            if key not in self._fills_seen:
                self._fills_seen[key] = None
                if len(self._fills_seen) > 50_000:
                    self._fills_seen.popitem(last=False)
                prev = order.filled_qty
                order.filled_qty = max(order.filled_qty, ev.filled_qty, prev + ev.last_fill_qty)
                if order.filled_qty > 0:
                    order.avg_fill_price = ((order.avg_fill_price * prev + ev.last_fill_price * ev.last_fill_qty)
                                            / (prev + ev.last_fill_qty))
                fill = Fill(ev.T or ev.recv_ts, ev.symbol, Side(ev.side), ev.last_fill_price, ev.last_fill_qty,
                            ev.fee, ev.is_maker, self.mode, ev.client_id, self.mid_fn(ev.symbol),
                            str(ev.trade_id), order.reason)
        if ev.exec_type == "AMENDMENT":
            order.price, order.qty = ev.price, ev.qty
        new = exchange_status(ev.status)
        if order.status is S.PENDING_CANCEL and new in (S.NEW, S.PARTIALLY_FILLED) and ev.exec_type != "CANCELED":
            new = S.PENDING_CANCEL  # a fill during a pending cancel keeps the cancel pending
        if ev.exec_type == "EXPIRED" and order.time_in_force == "GTX" and order.filled_qty == 0:
            self.post_only_rejects += 1
        self._set(order, new, ev.recv_ts)
        if fill is not None:
            if self.repo is not None:
                self.repo.insert_fill(fill, self.venue)
            for cb in self.fill_listeners:
                cb(fill, order)
        return fill

    def on_ack(self, ev: OrderAck) -> None:
        order = self.orders.get(ev.client_id)
        if order is None:
            return
        if ev.exchange_id is not None and order.exchange_id is None:
            order.exchange_id = ev.exchange_id
        if ev.action == "place":
            if ev.outcome == "ok" and order.status is S.PENDING_NEW:
                self._set(order, exchange_status(ev.status) if ev.status else S.NEW, ev.recv_ts)
            elif ev.outcome in ("rejected", "failed") and order.status in (S.PENDING_NEW, S.UNKNOWN):
                order.reject_reason = "post_only" if ev.code == -5022 else f"{ev.code}:{ev.msg}"[:120]
                if ev.code == -5022:
                    self.post_only_rejects += 1
                self._set(order, S.REJECTED, ev.recv_ts)
            elif ev.outcome == "unknown" and not order.status.is_terminal:
                self._set(order, S.UNKNOWN, ev.recv_ts)
                for cb in self.unknown_listeners:
                    cb(order)
        elif ev.action == "cancel":
            if ev.outcome == "ok" and not order.status.is_terminal:
                self._set(order, exchange_status(ev.status) if ev.status in ("CANCELED", "FILLED", "EXPIRED")
                          else S.CANCELED, ev.recv_ts)
            elif ev.outcome in ("not_found", "unknown") and not order.status.is_terminal:
                self._set(order, S.UNKNOWN, ev.recv_ts)
                for cb in self.unknown_listeners:
                    cb(order)
            elif ev.outcome in ("failed", "rejected") and order.status is S.PENDING_CANCEL:
                self._set(order, S.PARTIALLY_FILLED if order.filled_qty > 0 else S.NEW, ev.recv_ts)

    def resolve(self, client_id: str, status: OrderStatus, filled: Decimal, now: int) -> None:
        """Apply the result of a REST query for an UNKNOWN order (used by gateway/reconciler)."""
        order = self.orders.get(client_id)
        if order is None:
            return
        order.filled_qty = max(order.filled_qty, filled)
        order.status = status
        order.ts_updated = now
        self._persist(order)
        if status.is_terminal:
            self._release(order)
        else:
            self._open[client_id] = order

    # ---- internals ----

    def _set(self, order: Order, new: OrderStatus, now: int) -> None:
        if new is order.status:
            order.ts_updated = now
            return
        if not can_transition(order.status, new):
            return  # stale / out-of-order hint
        order.status = new
        order.ts_updated = now
        self._persist(order)
        if new.is_terminal:
            self._release(order)

    def _release(self, order: Order) -> None:
        self._open.pop(order.client_id, None)
        key = (order.symbol, order.side, order.level)
        if self.slots.get(key) == order.client_id:
            del self.slots[key]

    def _persist(self, order: Order) -> None:
        if self.repo is not None:
            self.repo.upsert_order(order, self.venue, self.mode)

    def prune(self, keep_terminal: int = 5000) -> None:
        """Bound memory: forget the oldest terminal orders (they are persisted)."""
        term = [cid for cid, o in self.orders.items() if o.status.is_terminal]
        for cid in term[:-keep_terminal] if len(term) > keep_terminal else []:
            del self.orders[cid]
