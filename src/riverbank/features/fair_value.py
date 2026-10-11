"""Fair Value Engine (SPEC §5.3). Floats only (features, not exchange boundary).

  mid       = (bid + ask) / 2
  micro     = (ask * bid_qty + bid * ask_qty) / (bid_qty + ask_qty)
  imbalance = (Σbid_qty - Σask_qty) / (Σbid_qty + Σask_qty) over top N depth levels
  σ         = EWMA of 1 s mid log-returns (half-life `sigma_half_life_s`), exposed in bps/√s
  flow      = signed aggTrade volume EWMA (half-life `flow_half_life_s`); + = buyer aggressor
  fair      = micro + α_imb·imbalance·half_spread + α_flow·flow_norm·half_spread  (α = 0 in v1)
"""

from __future__ import annotations

import math

from riverbank.core.config import QuotingCfg
from riverbank.core.models import Features
from riverbank.marketdata.orderbook import OrderBook


class FairValue:
    __slots__ = ("_abs_flow", "_flow", "_flow_ts", "_lam", "_last_sample_mid", "_last_sample_s", "_var", "ask",
                 "ask_qty", "bbo_ts", "bid", "bid_qty", "cfg", "n_samples", "symbol")

    def __init__(self, symbol: str, cfg: QuotingCfg) -> None:
        self.symbol = symbol
        self.cfg = cfg
        self.bid = self.ask = self.bid_qty = self.ask_qty = 0.0
        self.bbo_ts = 0
        self._lam = 0.5 ** (1.0 / cfg.sigma_half_life_s)  # per 1 s sample
        self._var = 0.0
        self.n_samples = 0
        self._last_sample_s = -1
        self._last_sample_mid = 0.0
        self._flow = 0.0
        self._abs_flow = 0.0
        self._flow_ts = 0

    # ---- inputs ----

    def on_bbo(self, bid: float, bid_qty: float, ask: float, ask_qty: float, ts: int) -> None:
        if bid > 0 and ask > bid:
            self.bid, self.bid_qty, self.ask, self.ask_qty, self.bbo_ts = bid, bid_qty, ask, ask_qty, ts

    def on_trade(self, qty: float, buyer_maker: bool, ts: int) -> None:
        self._decay_flow(ts)
        signed = -qty if buyer_maker else qty
        self._flow += signed
        self._abs_flow += abs(signed)

    def _decay_flow(self, ts: int) -> None:
        if self._flow_ts and ts > self._flow_ts:
            k = 0.5 ** ((ts - self._flow_ts) / 1000.0 / self.cfg.flow_half_life_s)
            self._flow *= k
            self._abs_flow *= k
        self._flow_ts = max(self._flow_ts, ts)

    def sample(self, ts: int) -> None:
        """Called on timer ticks; samples mid once per whole second."""
        sec = ts // 1000
        mid = self.mid
        if mid <= 0 or sec == self._last_sample_s:
            return
        if self._last_sample_s < 0 or self._last_sample_mid <= 0:
            self._last_sample_s, self._last_sample_mid = sec, mid
            return
        k = sec - self._last_sample_s  # seconds elapsed (>= 1)
        r = math.log(mid / self._last_sample_mid)
        lam_k = self._lam ** k
        self._var = lam_k * self._var + (1.0 - lam_k) * (r * r / k)
        self.n_samples += k
        self._last_sample_s, self._last_sample_mid = sec, mid

    # ---- outputs ----

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2 if self.ask > 0 else 0.0

    @property
    def micro(self) -> float:
        q = self.bid_qty + self.ask_qty
        if q <= 0:
            return self.mid
        return (self.ask * self.bid_qty + self.bid * self.ask_qty) / q

    @property
    def sigma_bps(self) -> float:
        return math.sqrt(self._var) * 1e4

    @property
    def sigma_ready(self) -> bool:
        return self.n_samples >= self.cfg.sigma_warmup_samples

    def flow(self, ts: int) -> float:
        self._decay_flow(ts)
        return self._flow

    def imbalance(self, book: OrderBook | None) -> float:
        n = self.cfg.imbalance_levels
        if book is not None:
            bids, asks = book.top(n)
            bq, aq = sum(q for _, q in bids), sum(q for _, q in asks)
        else:
            bq, aq = self.bid_qty, self.ask_qty
        tot = bq + aq
        return (bq - aq) / tot if tot > 0 else 0.0

    def features(self, ts: int, book: OrderBook | None = None) -> Features | None:
        if self.mid <= 0:
            return None
        imb = self.imbalance(book)
        flow = self.flow(ts)
        micro = self.micro
        fair = micro
        c = self.cfg
        if c.alpha_imb or c.alpha_flow:
            half = (self.ask - self.bid) / 2
            flow_norm = max(-1.0, min(1.0, flow / (self._abs_flow + 1e-12)))
            fair = micro + c.alpha_imb * imb * half + c.alpha_flow * flow_norm * half
        return Features(self.symbol, ts, self.mid, micro, imb, self.sigma_bps, flow, fair, self.bid, self.ask,
                        self.sigma_ready)
