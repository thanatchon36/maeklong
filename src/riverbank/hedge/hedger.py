"""Hedger (SPEC §5.7, OPEN_QUESTIONS A2/B23).

Every `hedge.eval_interval_s`, per venue whose risk state is OK or REDUCE_ONLY (below):
  exposure_H = Σ position_usdt_i × beta(i, H)   (hedge positions included, beta(H, H) = 1)
If |exposure vs the first instrument| > `hedge_threshold_usdt`, hedge on the first instrument in
`hedge.instruments` where qty = floor(|exposure_H| / mid_H, step) meets min qty / min notional
(capped so the instrument's position stays within `risk.max_position_usdt`). The order is post-only
(GTX) at our own side of the touch; taker (IOC market) only if `allow_taker` and the exposure exceeds
`hedge_urgent_usdt`. qty is rounded down: we never over-hedge. If nothing fits, no hedge: the risk
engine's `max_net_beta_exposure_usdt` does the work. REDUCE_ONLY → the same hedge, but a shrinking hedge
position never goes past flat (reduceOnly) and a growing one stays within `resume_frac` of the position and
gross limits, so it can clear the breach; HALTED → hedges are pulled and flattening closes hedge positions
with everything else.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

from riverbank.core.config import Config
from riverbank.core.decimal_utils import floor_to_step
from riverbank.core.models import Order, OrderStatus, RiskState, Side
from riverbank.ops.logging import get_logger

log = get_logger("hedger")


def _grows(pos: float, side: Side) -> bool:
    return pos == 0 or (pos > 0) == (side is Side.BUY)


class Hedger:
    def __init__(self, cfg: Config, engine: Any, beta: Callable[[str, str], float]) -> None:
        self.cfg = cfg
        self.h = cfg.hedge
        self.engine = engine
        self.beta = beta
        self._last_eval = 0
        self._coarse: set[str] = set()  # venues where no instrument fits (logged once)
        self._rejected: dict[str, str] = {}  # venue -> last risk reject reason (logged once per change)

    def on_timer(self, now: int) -> None:
        if now - self._last_eval < self.h.eval_interval_s * 1000:
            return
        self._last_eval = now
        for v in self.engine.venues.values():
            self.evaluate(v, now)

    def exposure(self, v: Any, ref: str) -> float:
        return sum(float(p.qty) * float(v.positions.mark(s)) * self.beta(s, ref)
                   for s, p in sorted(v.positions.positions.items()) if p.qty != 0)

    def _target(self, v: Any, reduce_only: bool = False) -> tuple[str, Side, Decimal, Decimal] | None:
        room = self.cfg.risk.resume_frac if reduce_only else 1.0  # REDUCE_ONLY: never grow past the resume bar
        for inst in self.h.instruments:
            spec, f = self.engine.specs.get(inst), self.engine.fv.get(inst)
            mid = self.engine.mark(inst)
            if spec is None or f is None or mid <= 0 or f.bid <= 0 or f.ask <= 0:
                continue
            e = self.exposure(v, inst)
            side = Side.SELL if e > 0 else Side.BUY
            notional = abs(e)
            pos = float(v.positions.qty(inst))
            if _grows(pos, side):  # the hedge grows this position and gross
                notional = min(notional, room * self.cfg.risk.max_position_usdt - abs(pos) * mid,
                               room * self.cfg.risk.max_gross_exposure_usdt - v.risk.gross_worst_case())
            elif reduce_only:
                notional = min(notional, abs(pos) * mid)  # close the leftover hedge leg, never flip it
            qty = floor_to_step(Decimal(repr(max(notional, 0.0) / mid)), spec.step)
            if qty >= spec.min_qty and float(qty) * mid >= float(spec.min_notional):
                price = Decimal(repr(f.bid if side is Side.BUY else f.ask))
                return inst, side, price, qty
        return None

    def evaluate(self, v: Any, now: int) -> None:
        live = [o for o in v.orders.live_orders() if o.reason == "hedge"]
        e = self.exposure(v, self.h.instruments[0])
        target = None
        if v.risk.state is RiskState.OK and abs(e) > self.h.hedge_threshold_usdt:
            target = self._target(v)
            if target is None and v.name not in self._coarse:
                self._coarse.add(v.name)
                log.warning("hedge_too_coarse", venue=v.name, exposure=round(e, 2))
            elif target is not None:
                self._coarse.discard(v.name)
        elif v.risk.state is RiskState.REDUCE_ONLY and abs(e) > self.h.hedge_threshold_usdt:
            # Nothing else trades a hedge instrument: without this a leftover hedge leg (demo twin 2026-10-12) or
            # a net-beta breach (shadow 2026-10-13) above the resume bar keeps the venue in REDUCE_ONLY for hours.
            target = self._target(v, reduce_only=True)
        keep: Order | None = None
        for o in live:
            if o.status is OrderStatus.PENDING_CANCEL:
                continue
            if target is not None and keep is None and (o.symbol, o.side) == target[:2] \
                    and o.price == target[2] and o.qty - o.filled_qty <= target[3]:
                keep = o  # still valid and never larger than needed
                continue
            if target is not None and o.status in (OrderStatus.PENDING_NEW, OrderStatus.UNKNOWN):
                keep = keep or o  # replace only once the exchange has answered
                continue
            v.orders.cancel(o, now, "hedge_update" if target is not None else "hedge_pull")
        if target is None or keep is not None or any(o.status is OrderStatus.PENDING_CANCEL for o in live):
            return  # one hedge order at a time: wait for cancels to confirm before placing
        inst, side, price, qty = target
        mark = self.engine.mark(inst)
        # reduceOnly when shrinking a hedge leg: the exchange also refuses a flip
        unwind = v.risk.state is RiskState.REDUCE_ONLY and not _grows(float(v.positions.qty(inst)), side)
        before = dict(v.risk.rejects)
        if self.h.allow_taker and abs(e) > self.h.hedge_urgent_usdt:
            order = v.orders.place(inst, side, Decimal(0), qty, reduce_only=unwind, tif="IOC", order_type="MARKET",
                                   reason="hedge", now=now, mark=mark)
        else:
            order = v.orders.place(inst, side, price, qty, reduce_only=unwind, tif="GTX", reason="hedge", now=now,
                                   mark=mark)
        if order is not None:
            self._rejected.pop(v.name, None)
            log.info("hedge", venue=v.name, symbol=inst, side=side.value, qty=str(qty), price=str(price),
                     exposure=round(e, 2))
            return
        reason = ",".join(sorted(k for k, n in v.risk.rejects.items() if n != before.get(k, 0))) or "rejected"
        if self._rejected.get(v.name) != reason:
            self._rejected[v.name] = reason
            log.warning("hedge_rejected", venue=v.name, symbol=inst, side=side.value, qty=str(qty),
                        exposure=round(e, 2), reason=reason)
