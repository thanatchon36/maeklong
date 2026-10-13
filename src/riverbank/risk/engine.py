"""Risk Engine (SPEC §5.6): independent, veto power, OK → REDUCE_ONLY → HALTED.

* `check(intent)` is called for every new order; strategy code cannot bypass it.
* `evaluate(now)` runs every second on the venue's positions/equity:
    soft breach → REDUCE_ONLY (position, gross, net-beta, leverage, liquidation distance,
                   daily loss ≥ 75 % of limit);  back to OK only below `resume_frac` of every limit.
                   REDUCE_ONLY admits reducing orders and hedges that shrink |net beta|.
    hard breach → HALTED (daily loss limit, max drawdown); also `halt()` from the kill
                   switch, unhandled exceptions, 429/418, ...  HALTED is sticky (manual restart).
Limits come only from config; nothing here ever raises a limit (SPEC §11.5).
"""

from __future__ import annotations

import datetime as dt
from collections import deque
from collections.abc import Callable, Iterable
from decimal import Decimal

from riverbank.core.config import RiskCfg
from riverbank.core.models import Order, QuoteIntent, RiskEvent, RiskState, Side
from riverbank.orders.positions import PositionBook
from riverbank.risk.limits import (
    APPROVE,
    Decision,
    approx_isolated_liq_price,
    is_reducing,
    liquidation_distance_pct,
    open_qty_by_side,
    worst_case_abs_notional,
)

SOFT_DAILY_LOSS_FRAC = 0.75

EventSink = Callable[[RiskEvent], None]


def _utc_day(ts_ms: int) -> str:
    return dt.datetime.fromtimestamp(ts_ms / 1000, dt.UTC).strftime("%Y-%m-%d")


class RiskEngine:
    def __init__(
        self,
        cfg: RiskCfg,
        venue: str,
        positions: PositionBook,
        orders: Callable[[], Iterable[Order]],
        *,
        order_limit_10s: int = 300,
        leverage: float = 3.0,
        beta: Callable[[str], float] | None = None,
        on_event: EventSink | None = None,
        persist_halt: Callable[[str], None] | None = None,
        kv_get: Callable[[str], str | None] | None = None,
        kv_set: Callable[[str, str], None] | None = None,
        liq_price: Callable[[str], float] | None = None,
    ) -> None:
        self.cfg = cfg
        self.venue = venue
        self.positions = positions
        self.orders = orders
        self.order_cap_10s = max(1, int(order_limit_10s * cfg.max_order_rate_frac))
        self.leverage = leverage
        self.beta = beta or (lambda s: 1.0)  # before M6 betas: everything moves with BTC
        self.on_event = on_event
        self.persist_halt = persist_halt
        self.kv_get = kv_get
        self.kv_set = kv_set
        self.liq_price = liq_price
        self.state = RiskState.OK
        self.halt_reason = ""
        self.breaches: list[str] = []
        self._sent: deque[int] = deque()
        self.day = ""
        self.day_start_equity = 0.0
        self.peak_equity = 0.0
        self._alerted_daily_50 = ""
        self.metrics: dict[str, float] = {}
        self.rejects: dict[str, int] = {}
        if kv_get is not None:
            self.peak_equity = float(kv_get(f"risk:{venue}:peak_equity") or 0.0)
            self.day = kv_get(f"risk:{venue}:day") or ""
            self.day_start_equity = float(kv_get(f"risk:{venue}:day_start_equity") or 0.0)

    # ---- state ----

    def _transition(self, new: RiskState, rule: str, detail: str, now: int, level: str = "WARN") -> None:
        if new is self.state:
            return
        if self.state is RiskState.HALTED:
            return  # sticky: only a manual restart leaves HALTED
        old = self.state
        self.state = new
        if self.on_event is not None:
            self.on_event(RiskEvent(now, level, rule, detail, old, new))

    def halt(self, reason: str, now: int) -> None:
        if self.state is RiskState.HALTED:
            return
        self.halt_reason = reason
        if self.persist_halt is not None:
            self.persist_halt(reason)
        self._transition(RiskState.HALTED, "halt", reason, now, "CRITICAL")

    def on_exception(self, exc: BaseException, now: int) -> None:
        self.halt(f"unhandled exception: {exc!r}"[:300], now)

    def _event(self, now: int, level: str, rule: str, detail: str) -> None:
        if self.on_event is not None:
            self.on_event(RiskEvent(now, level, rule, detail, self.state, self.state))

    # ---- pre-trade check ----

    def _reject(self, reason: str) -> Decision:
        self.rejects[reason] = self.rejects.get(reason, 0) + 1
        return Decision(False, reason)

    def check(self, intent: QuoteIntent, mark: float, now: int) -> Decision:
        orders = [o for o in self.orders() if o.is_open]
        sym = intent.symbol
        pos = self.positions.qty(sym)
        open_buy, open_sell = open_qty_by_side(orders, sym)
        open_reducing = open_sell if pos > 0 else open_buy
        reducing = intent.reduce_only and is_reducing(pos, intent.side, intent.qty, open_reducing)
        if self.state is RiskState.HALTED and not (reducing and intent.reason == "flatten"):
            return self._reject("halted")
        # A hedge that shrinks |net beta| may grow its own position in REDUCE_ONLY (limits below still apply):
        # otherwise a net-beta breach above the resume bar can only clear when the quoted positions fill out.
        hedge_down = intent.reason == "hedge" and not intent.reduce_only and self._reduces_net_beta(intent, mark)
        if self.state is RiskState.REDUCE_ONLY and not (reducing or hedge_down):
            return self._reject("reduce_only_state")
        if len(orders) + 1 > self.cfg.max_open_orders:
            return self._reject("max_open_orders")
        while self._sent and now - self._sent[0] > 10_000:
            self._sent.popleft()
        if len(self._sent) >= self.order_cap_10s:
            return self._reject("order_rate")
        if mark <= 0:
            return self._reject("no_mark")
        if not reducing:
            nb = open_buy + (intent.qty if intent.side is Side.BUY else 0)
            ns = open_sell + (intent.qty if intent.side is Side.SELL else 0)
            if worst_case_abs_notional(pos, nb, ns, mark) > self.cfg.max_position_usdt:
                return self._reject("max_position")
            gross = self._gross_worst_case(orders, intent, mark)
            if gross > self.cfg.max_gross_exposure_usdt:
                return self._reject("max_gross_exposure")
            equity = self.positions.equity()
            if equity <= 0 or gross / equity > self.cfg.max_leverage:
                return self._reject("max_leverage")
            if not self._reduces_net_beta(intent, mark):
                lo, hi = self._net_beta_worst_case(orders, intent, mark)
                if max(abs(lo), abs(hi)) > self.cfg.max_net_beta_exposure_usdt:
                    return self._reject("max_net_beta_exposure")
        self._sent.append(now)
        return APPROVE

    def _symbols(self, orders: list[Order], extra: str) -> list[str]:
        # sorted: float sums must not depend on hash order (deterministic replay)
        return sorted(set(self.positions.positions) | {o.symbol for o in orders} | {extra})

    def _mark(self, sym: str, intent: QuoteIntent, mark: float) -> float:
        return mark if sym == intent.symbol else self.positions.mark(sym)

    def _gross_worst_case(self, orders: list[Order], intent: QuoteIntent, mark: float) -> float:
        total = 0.0
        for s in self._symbols(orders, intent.symbol):
            b, sl = open_qty_by_side(orders, s)
            if s == intent.symbol:
                b += intent.qty if intent.side is Side.BUY else 0
                sl += intent.qty if intent.side is Side.SELL else 0
            total += worst_case_abs_notional(self.positions.qty(s), b, sl, self._mark(s, intent, mark))
        return total

    def gross_worst_case(self) -> float:
        """Worst-case gross exposure (positions + all open orders filled), as the pre-trade check sees it."""
        orders = [o for o in self.orders() if o.is_open]
        total = 0.0
        for s in sorted(set(self.positions.positions) | {o.symbol for o in orders}):
            b, sl = open_qty_by_side(orders, s)
            total += worst_case_abs_notional(self.positions.qty(s), b, sl, self.positions.mark(s))
        return total

    def _net_beta_worst_case(self, orders: list[Order], intent: QuoteIntent, mark: float) -> tuple[float, float]:
        lo = hi = 0.0
        for s in self._symbols(orders, intent.symbol):
            b, sl = open_qty_by_side(orders, s)
            if s == intent.symbol:
                b += intent.qty if intent.side is Side.BUY else 0
                sl += intent.qty if intent.side is Side.SELL else 0
            k = self._mark(s, intent, mark) * self.beta(s)
            pos = self.positions.qty(s)
            hi += float(pos + b) * k
            lo += float(pos - sl) * k
        return lo, hi

    def _reduces_net_beta(self, intent: QuoteIntent, mark: float) -> bool:
        net = self.net_beta()
        delta = float(intent.qty) * mark * self.beta(intent.symbol) * (1 if intent.side is Side.BUY else -1)
        return abs(net + delta) < abs(net) and abs(delta) <= 2 * abs(net)

    def net_beta(self) -> float:
        return sum(float(p.qty) * self.positions.mark(s) * self.beta(s) for s, p in self.positions.positions.items())

    # ---- periodic evaluation ----

    def evaluate(self, now: int) -> dict[str, float]:
        c = self.cfg
        equity = self.positions.equity()
        day = _utc_day(now)
        if day != self.day or self.day_start_equity <= 0:
            self.day = day
            self.day_start_equity = equity
            self._kv("day", day)
            self._kv("day_start_equity", f"{equity:.8f}")
        if equity > self.peak_equity:
            self.peak_equity = equity
            self._kv("peak_equity", f"{equity:.8f}")
        daily_loss_pct = max(0.0, (self.day_start_equity - equity) / self.day_start_equity * 100) \
            if self.day_start_equity > 0 else 0.0
        dd_pct = max(0.0, (self.peak_equity - equity) / self.peak_equity * 100) if self.peak_equity > 0 else 0.0
        max_pos = 0.0
        min_liq = 100.0
        for s, p in self.positions.positions.items():
            if p.qty == 0:
                continue
            m = self.positions.mark(s)
            max_pos = max(max_pos, abs(float(p.qty)) * m)
            liq = self.liq_price(s) if self.liq_price else approx_isolated_liq_price(
                float(p.qty), float(p.entry_price), self.leverage)
            min_liq = min(min_liq, liquidation_distance_pct(m, liq))
        gross = self.positions.gross_exposure()
        net = self.net_beta()
        lev = gross / equity if equity > 0 else float("inf")
        self.metrics = {"equity": equity, "daily_loss_pct": daily_loss_pct, "drawdown_pct": dd_pct,
                        "max_position_usdt": max_pos, "gross_usdt": gross, "net_beta_usdt": net, "leverage": lev,
                        "min_liq_distance_pct": min_liq, "peak_equity": self.peak_equity}
        if self.state is RiskState.HALTED:
            return self.metrics
        # hard
        if daily_loss_pct >= c.daily_loss_limit_pct:
            self.halt(f"daily loss {daily_loss_pct:.2f}% >= {c.daily_loss_limit_pct}%", now)
            return self.metrics
        if dd_pct >= c.max_drawdown_pct:
            self.halt(f"drawdown {dd_pct:.2f}% >= {c.max_drawdown_pct}%", now)
            return self.metrics
        if daily_loss_pct >= c.daily_loss_alert_frac * c.daily_loss_limit_pct and self._alerted_daily_50 != day:
            self._alerted_daily_50 = day
            self._event(now, "WARN", "daily_loss_alert",
                        f"daily loss {daily_loss_pct:.2f}% >= {c.daily_loss_alert_frac:.0%} of limit")
        # soft (ratio of each metric to its limit; liq distance inverted: lower is worse)
        ratios = {
            "max_position": max_pos / c.max_position_usdt,
            "gross_exposure": gross / c.max_gross_exposure_usdt,
            "net_beta_exposure": abs(net) / c.max_net_beta_exposure_usdt,
            "leverage": lev / c.max_leverage,
            "daily_loss": daily_loss_pct / (SOFT_DAILY_LOSS_FRAC * c.daily_loss_limit_pct),
            "liq_distance": (c.min_liq_distance_pct / min_liq) if min_liq > 0 else float("inf"),
        }
        breached = [k for k, r in ratios.items() if r > 1.0]
        self.breaches = breached
        if breached and self.state is RiskState.OK:
            self._transition(RiskState.REDUCE_ONLY, "soft_breach", ",".join(breached), now)
        elif self.state is RiskState.REDUCE_ONLY and all(r <= c.resume_frac for r in ratios.values()):
            self._transition(RiskState.OK, "recovered", "all metrics below resume threshold", now, "INFO")
        return self.metrics

    def _kv(self, key: str, value: str) -> None:
        if self.kv_set is not None:
            self.kv_set(f"risk:{self.venue}:{key}", value)

    def worst_open_position(self, symbol: str, orders: Iterable[Order]) -> tuple[Decimal, Decimal]:
        b, s = open_qty_by_side(orders, symbol)
        p = self.positions.qty(symbol)
        return p + b, p - s
