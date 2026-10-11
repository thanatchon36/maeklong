"""Engine — the single-threaded event dispatcher shared by replay, shadow, demo and live.

All strategy state changes happen inside `on_event`, called sequentially from one task
(or from the replay loop), so the same input sequence produces the same orders and fills.
I/O edges (WebSockets, REST, disk) live elsewhere and talk to the engine through events.

Venues: each venue has its own order manager, positions, risk engine and markouts.
  paper  — PaperExchange (shadow / replay / shadow twin of live symbols)
  real   — exchange gateway (demo / live), see orders/gateway.py
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from riverbank.analytics.markout import FillRecord, MarkoutTracker
from riverbank.core.clock import Clock, SimClock
from riverbank.core.config import Config
from riverbank.core.decimal_utils import floor_to_step
from riverbank.core.events import (
    AccountUpdate,
    AggTrade,
    BookTicker,
    BookTopN,
    DepthDiff,
    DepthSnapshot,
    MarkPrice,
    OrderAck,
    OrderUpdate,
    StreamStatus,
    Timer,
    UserStreamStatus,
)
from riverbank.core.models import BookStatus, Fill, Order, OrderStatus, RiskEvent, RiskState, Side, SymbolSpec
from riverbank.features.fair_value import FairValue
from riverbank.marketdata.market_state import MarketState
from riverbank.ops.logging import get_logger
from riverbank.orders.client_ids import ClientIdAllocator
from riverbank.orders.manager import Command, OrderManager
from riverbank.orders.positions import PositionBook
from riverbank.quoting.engine import QuoteInputs, QuoterState, compute_quotes
from riverbank.quoting.sizing import order_size
from riverbank.risk.engine import RiskEngine
from riverbank.sim.paper_fills import PaperExchange

log = get_logger("engine")


@dataclass
class Venue:
    name: str
    kind: str  # paper | real
    fill_mode: str  # shadow | demo | live
    positions: PositionBook
    orders: OrderManager
    risk: RiskEngine
    markouts: MarkoutTracker
    paper: PaperExchange | None = None
    gateway: Any = None
    symbols: set[str] = field(default_factory=set)
    winding: set[str] = field(default_factory=set)
    quoter: dict[str, QuoterState] = field(default_factory=dict)
    user_stream_ok: bool = True
    halted_at: int = 0
    flatten_done: bool = False
    last_reason: dict[str, str] = field(default_factory=dict)
    last_quote_ts: dict[str, int] = field(default_factory=dict)
    uptime: dict[str, list[int]] = field(default_factory=dict)
    fills: int = 0
    paused_until: int = 0  # e.g. waiting for reconciliation after a user-stream reconnect


class Engine:
    def __init__(
        self,
        cfg: Config,
        specs: dict[str, SymbolSpec],
        market: MarketState,
        clock: Clock,
        ids: ClientIdAllocator,
        *,
        repo: Any = None,
        alerts: Any = None,
        metrics: Any = None,
        hedge_symbols: Iterable[str] = (),
        md_handle: Callable[[Any], bool] | None = None,
        md_timer: Callable[[int], Any] | None = None,
        beta: Callable[[str], float] | None = None,
        order_limit_10s: int = 300,
    ) -> None:
        self.cfg = cfg
        self.specs = specs
        self.market = market
        self.clock = clock
        self.ids = ids
        self.repo = repo
        self.alerts = alerts
        self.metrics = metrics
        self.hedge_symbols = set(hedge_symbols)
        self.md_handle = md_handle or self._default_md_handle
        self.md_timer = md_timer or market.on_timer
        self.beta = beta or (lambda s: 1.0)
        self.order_limit_10s = order_limit_10s
        self.fv: dict[str, FairValue] = {}
        self.venues: dict[str, Venue] = {}
        self._last_timer = 0
        self._last_1s = 0
        self._last_snap = 0
        self._last_uptime_flush = 0
        self._last_rate: dict[str, float] = {}
        self._funding_applied: dict[str, int] = {}
        self.timer_hooks: list[Callable[[int], None]] = []
        self.fill_hooks: list[Callable[[Venue, Fill, Order], None]] = []
        self.errors = 0
        self._size_cache: dict[str, tuple[float, Any]] = {}  # symbol -> (reference mid, OrderSize)
        for s in list(market.symbols):
            self.ensure_symbol(s)

    # ---- setup ----

    def ensure_symbol(self, symbol: str) -> None:
        if symbol not in self.fv:
            self.fv[symbol] = FairValue(symbol, self.cfg.quoting)
        self.market.add_symbol(symbol)

    def _make_venue(self, name: str, kind: str, fill_mode: str, equity: float,
                    submit: Callable[[Command], None], **risk_kw: Any) -> Venue:
        positions = PositionBook(equity)
        holder: dict[str, OrderManager] = {}
        risk = RiskEngine(self.cfg.risk, name, positions, lambda: holder["om"].live_orders(),
                          order_limit_10s=self.order_limit_10s, leverage=self.cfg.account.leverage,
                          beta=self.beta, **risk_kw)
        om = OrderManager(name, fill_mode, self.ids, risk, submit, mid_fn=self.mid, repo=self.repo,
                          requote_ticks=self.cfg.quoting.requote_ticks,
                          min_requote_interval_ms=self.cfg.quoting.min_requote_interval_ms)
        holder["om"] = om
        v = Venue(name, kind, fill_mode, positions, om, risk, MarkoutTracker())
        risk.on_event = lambda e, v=v: self._on_risk_event(v, e)
        om.fill_listeners.append(lambda f, o, v=v: self._on_fill(v, f, o))
        v.markouts.listeners.append(lambda rec, h, val, v=v: self._on_markout(v, rec, h, val))
        self.venues[name] = v
        return v

    def add_paper_venue(self, name: str = "paper", fill_mode: str = "shadow", equity: float | None = None) -> Venue:
        holder: dict[str, PaperExchange] = {}
        v = self._make_venue(name, "paper", fill_mode, equity or self.cfg.ops.shadow_equity_usdt,
                             lambda cmd: holder["px"].submit(cmd, self.clock.now_ms()))
        px = PaperExchange(name, self.market, self._on_exec_event, latency_ms=self.cfg.ops.sim_latency_ms,
                           fee_maker_bps=self.cfg.account.fee_maker_bps, fee_taker_bps=self.cfg.account.fee_taker_bps)
        holder["px"] = px
        v.paper = px
        return v

    def add_real_venue(self, name: str, fill_mode: str, gateway: Any, equity: float, **risk_kw: Any) -> Venue:
        v = self._make_venue(name, "real", fill_mode, equity, gateway.submit, **risk_kw)
        v.gateway = gateway
        return v

    def set_symbols(self, venue: str, symbols: Iterable[str]) -> None:
        v = self.venues[venue]
        new = set(symbols)
        now = self.clock.now_ms()
        for s in new:
            self.ensure_symbol(s)
            v.quoter.setdefault(s, QuoterState())
            v.winding.discard(s)
        for s in v.symbols - new:
            if v.positions.qty(s) != 0:
                v.winding.add(s)  # keep reducing inventory until flat
            else:
                v.orders.cancel_all(now, s, "universe_removed")
        v.symbols = new
        log.info("universe_set", venue=venue, symbols=sorted(new), winding=sorted(v.winding))

    def all_symbols(self) -> list[str]:
        out = set(self.hedge_symbols)
        for v in self.venues.values():
            out |= v.symbols | v.winding | {s for s, p in v.positions.positions.items() if p.qty != 0}
        return sorted(out)

    # ---- helpers ----

    def mid(self, symbol: str) -> float:
        f = self.fv.get(symbol)
        return f.mid if f else 0.0

    def mark(self, symbol: str) -> float:
        m = self.market.get(symbol)
        if m is not None and m.mark is not None:
            return m.mark.mark
        return self.mid(symbol)

    def _default_md_handle(self, ev: Any) -> bool:
        mkt = self.market
        if isinstance(ev, BookTopN):
            return mkt.on_topn(ev)
        if isinstance(ev, BookTicker):
            return mkt.on_book_ticker(ev)
        if isinstance(ev, AggTrade):
            return mkt.on_agg_trade(ev)
        if isinstance(ev, MarkPrice):
            return mkt.on_mark(ev)
        if isinstance(ev, DepthDiff):
            mkt.on_depth(ev)
            return True
        if isinstance(ev, DepthSnapshot):
            mkt.on_snapshot(ev)
            return True
        if isinstance(ev, StreamStatus):
            mkt.on_stream_status(ev)
            return True
        return False

    # ---- event entry ----

    def on_event(self, ev: Any) -> None:
        try:
            self._dispatch(ev)
        except Exception as e:  # SPEC §11.3: any unhandled exception in order/risk path -> HALTED
            self.errors += 1
            log.exception("engine_exception", event=type(ev).__name__)
            self.halt_all(f"unhandled exception in engine: {e!r}"[:300])

    def _dispatch(self, ev: Any) -> None:
        now = ev.recv_ts
        if isinstance(self.clock, SimClock):
            self.clock.advance_to(now)
        for v in self.venues.values():
            if v.paper is not None:
                v.paper.advance(now)
        if isinstance(ev, Timer):
            self.on_timer(now)
            return
        if isinstance(ev, (OrderUpdate, OrderAck)):
            self._on_exec_event(ev)
            return
        if isinstance(ev, AccountUpdate):
            self._on_account_update(ev)
            return
        if isinstance(ev, UserStreamStatus):
            self._on_user_stream(ev)
            return
        prev_mark = None
        if isinstance(ev, MarkPrice):
            m = self.market.get(ev.symbol)
            prev_mark = m.mark if m is not None else None
        new = self.md_handle(ev)
        if isinstance(ev, BookTicker):
            if new:
                f = self.fv.get(ev.symbol)
                if f is not None:
                    f.on_bbo(ev.bid_px, ev.bid_qty, ev.ask_px, ev.ask_qty, ev.recv_ts)
                self._paper_book(ev.symbol, now)
        elif isinstance(ev, AggTrade):
            if new:
                f = self.fv.get(ev.symbol)
                if f is not None:
                    f.on_trade(ev.qty, ev.buyer_maker, ev.recv_ts)
                for v in self.venues.values():
                    if v.paper is not None:
                        v.paper.on_trade(ev)
        elif isinstance(ev, (DepthDiff, BookTopN)):
            if isinstance(ev, BookTopN):
                f = self.fv.get(ev.symbol)
                if f is not None and ev.bids and ev.asks and f.bbo_ts < ev.recv_ts - 1000:
                    # no bookTicker recently: fall back to the book's L1
                    f.on_bbo(ev.bids[0][0], ev.bids[0][1], ev.asks[0][0], ev.asks[0][1], ev.recv_ts)
            self._paper_book(ev.symbol, now)
        elif isinstance(ev, MarkPrice) and new:
            for v in self.venues.values():
                v.positions.set_mark(ev.symbol, ev.mark)
            self._maybe_funding(ev, prev_mark)

    def _on_exec_event(self, ev: OrderUpdate | OrderAck) -> None:
        """Execution reports (paper exchange calls this synchronously, without re-advancing)."""
        v = self.venues.get(ev.venue)
        if v is None:
            return
        if isinstance(ev, OrderUpdate):
            v.orders.on_update(ev)
        else:
            v.orders.on_ack(ev)

    def _paper_book(self, symbol: str, now: int) -> None:
        for v in self.venues.values():
            if v.paper is not None:
                v.paper.on_book(symbol, now)

    # ---- timer ----

    def on_timer(self, now: int) -> None:
        dt_ms = now - self._last_timer if self._last_timer else 0
        self._last_timer = now
        self.md_timer(now)
        for f in self.fv.values():
            f.sample(now)
        every_1s = now - self._last_1s >= 1000
        if every_1s:
            self._last_1s = now
            for s in self.fv:
                if self.mark(s) > 0:
                    for v in self.venues.values():
                        if s not in v.positions.marks:
                            v.positions.set_mark(s, self.mark(s))
        interval = self.cfg.quoting.quote_interval_ms
        for v in self.venues.values():
            v.markouts.resolve(now, self.mid)
            if every_1s:
                v.risk.evaluate(now)
            if v.risk.state is RiskState.HALTED:
                self._flatten(v, now)
            else:
                for sym in sorted(v.symbols | v.winding):
                    if now - v.last_quote_ts.get(sym, 0) >= interval:
                        v.last_quote_ts[sym] = now
                        self._quote(v, sym, now)
            if dt_ms > 0:
                for sym in v.symbols:
                    u = v.uptime.setdefault(sym, [0, 0])
                    u[1] += dt_ms
                    if v.orders.has_two_sided(sym):
                        u[0] += dt_ms
        for hook in self.timer_hooks:
            hook(now)
        if now - self._last_uptime_flush >= 60_000:
            self._flush_uptime(now)
            for v in self.venues.values():
                v.orders.prune()
        if now - self._last_snap >= 60_000:
            self._last_snap = now
            self._snapshot_pnl(now)
        if every_1s:
            self._update_metrics()

    # ---- quoting ----

    def _quote(self, v: Venue, sym: str, now: int) -> None:
        spec = self.specs.get(sym)
        f = self.fv.get(sym)
        m = self.market.get(sym)
        if spec is None or f is None or m is None:
            return
        feats = f.features(now, m.sync.book if m.sync.synced else None)
        pos = v.positions.qty(sym)
        mid = feats.mid if feats else 0.0
        q = self.cfg.quoting
        size = self._order_size(spec, mid)
        risk_state = v.risk.state
        if sym in v.winding:
            if pos == 0:
                v.winding.discard(sym)
                v.orders.cancel_all(now, sym, "wound_down")
                return
            if risk_state is RiskState.OK:
                risk_state = RiskState.REDUCE_ONLY
        order_qty = size.qty if size.qty > 0 else abs(pos)
        units = size.inventory_units if size.ok else 0
        if risk_state is RiskState.REDUCE_ONLY:
            units = max(units, 1)
        user_ok = v.user_stream_ok and now >= v.paused_until
        inp = QuoteInputs(spec, feats, pos, order_qty, units, risk_state, m.status, now, user_ok,
                          m.mark.next_funding_ms if m.mark else 0, m.prev_funding_ms, self.mark(sym))
        dec = compute_quotes(q, inp, v.quoter.setdefault(sym, QuoterState()))
        if v.last_reason.get(sym) != dec.reason:
            log.info("quote_state", venue=v.name, symbol=sym, reason=dec.reason, sigma=feats.sigma_bps if feats else 0)
            v.last_reason[sym] = dec.reason
        v.orders.sync_quotes(sym, dec.quotes, spec, now, self.mark(sym))

    def _order_size(self, spec: SymbolSpec, mid: float) -> Any:
        """Order size from a reference price, re-sized only after a >5 % move (avoids requote churn
        when price oscillates around a step boundary)."""
        cached = self._size_cache.get(spec.symbol)
        if cached is not None and mid > 0 and abs(mid / cached[0] - 1) <= 0.05:
            return cached[1]
        q = self.cfg.quoting
        size = order_size(spec, mid, q.order_size_usdt, q.size_buffer, self.cfg.risk.max_position_usdt,
                          q.max_inventory_units)
        if mid > 0:
            self._size_cache[spec.symbol] = (mid, size)
        return size

    # ---- fills / markouts / risk ----

    def _on_fill(self, v: Venue, fill: Fill, order: Order) -> None:
        v.positions.apply_fill(fill)
        v.markouts.on_fill(fill, v.name)
        v.fills += 1
        if self.metrics is not None:
            self.metrics.fills.labels(v.name, fill.symbol, fill.side.value).inc()
        log.info("fill", venue=v.name, symbol=fill.symbol, side=fill.side.value, price=str(fill.price),
                 qty=str(fill.qty), maker=fill.is_maker, pos=str(v.positions.qty(fill.symbol)), reason=order.reason)
        for hook in self.fill_hooks:
            hook(v, fill, order)

    def _on_markout(self, v: Venue, rec: FillRecord, h: int, value: float) -> None:
        if self.repo is not None:
            self.repo.update_markout(v.name, rec.fill.client_id, rec.fill.trade_id, h, value)
        if h == 5:
            st = v.quoter.get(rec.fill.symbol)
            if st is not None:
                st.on_markout_5s(value)

    def _on_risk_event(self, v: Venue, e: RiskEvent) -> None:
        log.warning("risk_event", venue=v.name, rule=e.rule, detail=e.detail, before=e.state_before.value,
                    after=e.state_after.value)
        if self.repo is not None:
            self.repo.insert_risk_event(e, v.name)
        if self.alerts is not None:
            self.alerts.send(e.level, f"risk_{e.rule}", f"{v.name}: {e.state_before.value}->{e.state_after.value} "
                                                         f"{e.detail}")
        if e.state_after is RiskState.HALTED and e.state_before is not RiskState.HALTED:
            v.halted_at = e.ts
            v.flatten_done = False
            n = v.orders.cancel_all(e.ts, None, "halted")
            log.error("halted_cancel_all", venue=v.name, cancels=n, reason=e.detail)

    def halt_all(self, reason: str) -> None:
        now = self.clock.now_ms()
        for v in self.venues.values():
            v.risk.halt(reason, now)

    def _flatten(self, v: Venue, now: int) -> None:
        """HALTED: cancel everything, flatten with reduce-only limit orders (taker only if allowed)."""
        for o in v.orders.live_orders():
            if o.reason != "flatten" and o.status in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED):
                v.orders.cancel(o, now, "halted")
        rc = self.cfg.risk
        all_flat = True
        for sym, p in list(v.positions.positions.items()):
            if p.qty == 0:
                continue
            all_flat = False
            spec = self.specs.get(sym)
            if spec is None:
                continue
            live = [o for o in v.orders.live_orders(sym) if o.reason == "flatten"]
            if live:
                o = live[0]
                if (o.status in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED)
                        and now - o.ts_created >= rc.flatten_requote_s * 1000):
                    v.orders.cancel(o, now, "flatten_requote")
                continue
            qty = floor_to_step(abs(p.qty), spec.step)
            if qty <= 0 or qty < spec.min_qty:
                continue  # dust below min qty cannot be closed by an order
            side = Side.SELL if p.qty > 0 else Side.BUY
            f = self.fv.get(sym)
            if f is None or f.mid <= 0:
                continue
            if rc.emergency_flatten and now - v.halted_at >= rc.flatten_taker_after_s * 1000:
                v.orders.place(sym, side, Decimal(0), qty, reduce_only=True, tif="IOC", order_type="MARKET",
                               reason="flatten", now=now, mark=self.mark(sym))
            else:
                px = Decimal(repr(f.ask if side is Side.SELL else f.bid))  # passive: join our side
                v.orders.place(sym, side, px, qty, reduce_only=True, tif="GTX", reason="flatten", now=now,
                               mark=self.mark(sym))
        if all_flat and not v.flatten_done:
            v.flatten_done = True
            log.error("flatten_complete", venue=v.name, note="HALTED: manual restart required")
            if self.alerts is not None:
                self.alerts.send("CRITICAL", "flatten_complete", f"{v.name}: flat; HALTED, manual restart required")

    def _maybe_funding(self, ev: MarkPrice, prev: MarkPrice | None) -> None:
        """Paper venues: apply funding when a funding timestamp has passed (real venues: ACCOUNT_UPDATE)."""
        m = self.market.get(ev.symbol)
        if m is None or m.prev_funding_ms <= 0 or self._funding_applied.get(ev.symbol) == m.prev_funding_ms:
            return
        self._funding_applied[ev.symbol] = m.prev_funding_ms
        rate = prev.funding_rate if prev is not None else ev.funding_rate
        for v in self.venues.values():
            if v.kind != "paper":
                continue
            q = v.positions.qty(ev.symbol)
            if q == 0:
                continue
            amount = -float(q) * ev.mark * rate  # positive rate: longs pay shorts
            v.positions.apply_funding(ev.symbol, Decimal(repr(amount)), ev.recv_ts)
            if self.repo is not None:
                self.repo.insert_funding(ev.recv_ts, v.name, ev.symbol, rate, amount)

    # ---- real venue user data (demo/live) ----

    def _on_account_update(self, ev: AccountUpdate) -> None:
        v = self.venues.get(ev.venue)
        if v is None:
            return
        for p in ev.positions:
            before = v.positions.qty(p.symbol)
            v.positions.adopt(p.symbol, p.qty, p.entry_price, ev.recv_ts)
            if before != p.qty and ev.reason not in ("ORDER", "FUNDING_FEE"):
                log.warning("position_changed_externally", venue=v.name, symbol=p.symbol, before=str(before),
                            after=str(p.qty), reason=ev.reason)

    def _on_user_stream(self, ev: UserStreamStatus) -> None:
        v = self.venues.get(ev.venue)
        if v is None:
            return
        v.user_stream_ok = ev.connected
        if not ev.connected:
            v.orders.cancel_all(ev.recv_ts, None, "user_stream_down")
            if self.alerts is not None:
                self.alerts.send("WARN", "user_stream_down", f"{v.name}: {ev.detail}")

    # ---- persistence / metrics ----

    def _flush_uptime(self, now: int) -> None:
        self._last_uptime_flush = now
        date = dt.datetime.fromtimestamp(now / 1000, dt.UTC).strftime("%Y-%m-%d")
        for v in self.venues.values():
            for sym, (q, t) in v.uptime.items():
                if t and self.repo is not None:
                    self.repo.add_uptime(date, v.name, sym, q, t)
            v.uptime = {}

    def _snapshot_pnl(self, now: int) -> None:
        if self.repo is None:
            return
        rows = []
        for v in self.venues.values():
            eq = v.positions.equity()
            for sym, p in v.positions.positions.items():
                rows.append((now, v.name, sym, float(p.qty), self.mid(sym), float(p.realized_pnl), float(p.fees),
                             float(p.funding), v.positions.unrealized(sym), eq))
            rows.append((now, v.name, "__TOTAL__", 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, eq))
        self.repo.insert_pnl_snapshots(rows)

    def final_snapshot(self, now: int) -> None:
        """Persist uptime and a PnL snapshot (shutdown / end of replay) so reports include the tail."""
        self._flush_uptime(now)
        self._snapshot_pnl(now)

    def _update_metrics(self) -> None:
        mt = self.metrics
        if mt is None:
            return
        for v in self.venues.values():
            mt.risk_state.labels(v.name).set({"OK": 0, "REDUCE_ONLY": 1, "HALTED": 2}[v.risk.state.value])
            mt.equity.labels(v.name).set(v.positions.equity())
            for sym in v.symbols | set(v.positions.positions):
                mt.inventory.labels(v.name, sym).set(v.positions.notional(sym))
                mt.open_orders.labels(v.name, sym).set(len(v.orders.live_orders(sym)))
                st = v.markouts.symbol_stats(sym, 60, last_n=200)
                if st.n:
                    mt.realized_edge.labels(v.name, sym).set(st.mean)
        for sym, m in self.market.symbols.items():
            mt.book_status.labels(sym).set(1 if m.status is BookStatus.OK else 0)

    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for v in self.venues.values():
            out[v.name] = {"risk": v.risk.state.value, "equity": round(v.positions.equity(), 4),
                           "open_orders": v.orders.open_count(), "fills": v.fills,
                           "symbols": sorted(v.symbols), "positions": {s: str(p.qty) for s, p in
                                                                      v.positions.positions.items() if p.qty}}
        return out
