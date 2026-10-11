"""Quoting Engine (SPEC §5.4): Avellaneda–Stoikov inspired, perpetual (no terminal time).

  σ_h          = σ_bps(1 s) · sqrt(sigma_horizon_s)                      (OPEN_QUESTIONS B2)
  reservation  = fair · (1 − skew_k · q_norm · σ_h / 1e4),  q_norm = clip(q / max_units, −1, 1)
  half_spread  = max(min_half_spread, base_half_spread + vol_mult · σ_h) · toxicity_mult · funding_mult
  bid          = floor_to_tick(reservation · (1 − hs/1e4))
  ask          = ceil_to_tick (reservation · (1 + hs/1e4))

Guards (pull = no quotes): book not OK, user stream down, risk HALTED, σ not warmed up,
σ > vol_kill (pull for cooldown_s), toxicity ladder exhausted, funding guard (pull mode).
REDUCE_ONLY: only the inventory-reducing side, reduceOnly, qty <= |position| (OPEN_QUESTIONS B4).
`compute_quotes` is pure apart from the per-symbol `QuoterState` it updates.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal

from riverbank.core.config import QuotingCfg
from riverbank.core.decimal_utils import ceil_to_step, floor_to_step
from riverbank.core.models import BookStatus, Features, RiskState, Side, SymbolSpec


@dataclass(frozen=True, slots=True)
class DesiredQuote:
    side: Side
    level: int
    price: Decimal
    qty: Decimal
    reduce_only: bool = False


@dataclass(slots=True)
class QuoteDecision:
    quotes: list[DesiredQuote]
    reason: str
    reservation: float = 0.0
    half_spread_bps: float = 0.0
    q_norm: float = 0.0


@dataclass(slots=True)
class QuoterState:
    """Per (venue, symbol) mutable state: volatility cooldown and toxicity ladder."""

    vol_pull_until: int = 0
    tox_level: int = 0
    tox_changed_ts: int = 0
    tox_pull_until: int = 0
    markouts_5s: deque[float] = field(default_factory=lambda: deque(maxlen=200))

    def on_markout_5s(self, value_bps: float) -> None:
        self.markouts_5s.append(value_bps)

    def update_toxicity(self, cfg: QuotingCfg, now: int) -> None:
        """Escalate 1.0x -> 1.5x -> 2.0x -> pull while recent 5 s markout < toxic threshold.

        One step per `toxicity_step_s`. After a pull of `cooldown_s` we come back at the
        highest widening level with a fresh window (new evidence required).
        """
        n_levels = len(cfg.toxicity_multipliers)
        if self.tox_level >= n_levels:  # pulled
            if now >= self.tox_pull_until:
                self.tox_level = n_levels - 1
                self.tox_changed_ts = now
                self.markouts_5s.clear()
            return
        window = list(self.markouts_5s)[-cfg.toxicity_window_fills:]
        if len(window) < cfg.toxicity_min_samples:
            return
        mean = sum(window) / len(window)
        if now - self.tox_changed_ts < cfg.toxicity_step_s * 1000:
            return
        if mean < cfg.toxic_markout_bps:
            self.tox_level += 1
            self.tox_changed_ts = now
            if self.tox_level >= n_levels:
                self.tox_pull_until = now + cfg.cooldown_s * 1000
        elif self.tox_level > 0:
            self.tox_level -= 1
            self.tox_changed_ts = now

    def tox_multiplier(self, cfg: QuotingCfg) -> float | None:
        if self.tox_level >= len(cfg.toxicity_multipliers):
            return None
        return cfg.toxicity_multipliers[self.tox_level]


@dataclass(slots=True)
class QuoteInputs:
    spec: SymbolSpec
    features: Features | None
    position_qty: Decimal
    order_qty: Decimal
    inventory_units: int
    risk_state: RiskState
    book_status: BookStatus
    now_ms: int
    user_stream_ok: bool = True
    next_funding_ms: int = 0
    prev_funding_ms: int = 0
    mark_price: float = 0.0


def _pull(reason: str) -> QuoteDecision:
    return QuoteDecision([], reason)


def compute_quotes(cfg: QuotingCfg, inp: QuoteInputs, st: QuoterState) -> QuoteDecision:
    f = inp.features
    now = inp.now_ms
    if inp.risk_state is RiskState.HALTED:
        return _pull("risk_halted")
    if inp.book_status is not BookStatus.OK:
        return _pull(f"book_{inp.book_status.value.lower()}")
    if not inp.user_stream_ok:
        return _pull("user_stream_down")
    if f is None or f.mid <= 0:
        return _pull("no_features")
    if not f.sigma_ready:
        return _pull("sigma_warmup")
    if f.sigma_bps > cfg.vol_kill_bps:
        st.vol_pull_until = now + cfg.cooldown_s * 1000
        return _pull("vol_kill")
    if now < st.vol_pull_until:
        return _pull("vol_cooldown")
    st.update_toxicity(cfg, now)
    tox = st.tox_multiplier(cfg)
    if tox is None:
        return _pull("toxic")
    funding_mult = 1.0
    g = cfg.funding_guard_s * 1000
    in_guard = any(t and abs(now - t) <= g for t in (inp.next_funding_ms, inp.prev_funding_ms))
    if in_guard:
        if cfg.funding_guard_action == "pull":
            return _pull("funding_guard")
        funding_mult = cfg.funding_widen_mult
    if inp.order_qty <= 0 or inp.inventory_units <= 0:
        return _pull("no_size")

    spec = inp.spec
    tick = spec.tick
    units = inp.inventory_units
    q = float(inp.position_qty / inp.order_qty)
    q_norm = max(-1.0, min(1.0, q / units))
    sigma_h = f.sigma_bps * math.sqrt(cfg.sigma_horizon_s)
    reservation = f.fair_value * (1.0 - cfg.skew_k * q_norm * sigma_h / 1e4)
    hs = max(cfg.min_half_spread_bps, cfg.base_half_spread_bps + cfg.vol_mult * sigma_h) * tox * funding_mult

    best_bid = Decimal(repr(f.best_bid))
    best_ask = Decimal(repr(f.best_ask))
    quotes: list[DesiredQuote] = []
    reduce_only = inp.risk_state is RiskState.REDUCE_ONLY
    want_bid = q < units - 1e-9
    want_ask = q > -units + 1e-9
    qty = inp.order_qty
    if reduce_only:
        pos = inp.position_qty
        if pos == 0:
            return QuoteDecision([], "reduce_only_flat", reservation, hs, q_norm)
        want_bid, want_ask = pos < 0, pos > 0
        qty = floor_to_step(min(inp.order_qty, abs(pos)), spec.step)
        if qty <= 0 or qty < spec.min_qty:
            return QuoteDecision([], "reduce_only_too_small", reservation, hs, q_norm)

    for level in range(cfg.levels):
        off = hs + level * cfg.level_spacing_bps
        if want_bid:
            bid = floor_to_step(reservation * (1 - off / 1e4), tick)
            bid = min(bid, best_ask - tick)  # never cross (post-only would reject anyway)
            if _price_ok(spec, bid, inp.mark_price):
                quotes.append(DesiredQuote(Side.BUY, level, bid, qty, reduce_only))
        if want_ask:
            ask = ceil_to_step(reservation * (1 + off / 1e4), tick)
            ask = max(ask, best_bid + tick)
            if _price_ok(spec, ask, inp.mark_price):
                quotes.append(DesiredQuote(Side.SELL, level, ask, qty, reduce_only))
        if reduce_only:
            break  # reduce-only: a single order on the reducing side
    return QuoteDecision(quotes, "reduce_only" if reduce_only else "ok", reservation, hs, q_norm)


def _price_ok(spec: SymbolSpec, price: Decimal, mark: float) -> bool:
    if price <= 0:
        return False
    if spec.min_price > 0 and price < spec.min_price:
        return False
    if spec.max_price > 0 and price > spec.max_price:
        return False
    if mark > 0 and spec.percent_up > 0 and spec.percent_down > 0:
        m = Decimal(repr(mark))
        if price > m * spec.percent_up or price < m * spec.percent_down:
            return False
    return True
