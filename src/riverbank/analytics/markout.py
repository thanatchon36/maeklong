"""Markout analytics (SPEC §5.9, OPEN_QUESTIONS B9).

  markout_bps(h)        = side · (mid(t+h) − fill_price) / fill_price · 1e4
  spread_capture_bps    = side · (mid(t) − fill_price) / fill_price · 1e4
  realized_edge_bps(h)  = markout_bps(h) − fee_bps        (half-spread + mid move − fee)

`MarkoutTracker` resolves horizons online as time passes (deterministic in replay: it only
uses the clock passed in). Statistics always report n, mean, standard error.
"""

from __future__ import annotations

import heapq
import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

from riverbank.core.models import Fill

HORIZONS_S: tuple[int, ...] = (1, 5, 30, 60, 300)


@dataclass(slots=True)
class FillRecord:
    fill: Fill
    venue: str
    fee_bps: float
    spread_capture_bps: float
    markouts: dict[int, float] = field(default_factory=dict)
    fill_id: int = 0

    def edge(self, h: int) -> float | None:
        m = self.markouts.get(h)
        return None if m is None else m - self.fee_bps


@dataclass(frozen=True, slots=True)
class Stats:
    n: int
    mean: float
    std: float
    se: float

    @property
    def lower(self) -> float:
        return self.mean - self.se

    @property
    def upper(self) -> float:
        return self.mean + self.se


def stats(values: Iterable[float]) -> Stats:
    xs = [v for v in values if v is not None and not math.isnan(v)]
    n = len(xs)
    if n == 0:
        return Stats(0, float("nan"), float("nan"), float("nan"))
    mean = sum(xs) / n
    if n == 1:
        return Stats(1, mean, float("nan"), float("nan"))
    var = sum((x - mean) ** 2 for x in xs) / (n - 1)
    std = math.sqrt(var)
    return Stats(n, mean, std, std / math.sqrt(n))


def markout_bps(side_sign: int, fill_price: float, mid_later: float) -> float:
    return side_sign * (mid_later - fill_price) / fill_price * 1e4


def fee_bps_of(fill: Fill) -> float:
    notional = float(fill.price * fill.qty)
    return float(fill.fee) / notional * 1e4 if notional > 0 else 0.0


ResolvedCb = Callable[[FillRecord, int, float], None]


class MarkoutTracker:
    def __init__(self, horizons: Sequence[int] = HORIZONS_S, keep: int = 20_000) -> None:
        self.horizons = tuple(horizons)
        self.keep = keep
        self.records: list[FillRecord] = []
        self._heap: list[tuple[int, int, int, FillRecord]] = []  # (due_ts, seq, h, rec)
        self._seq = 0
        self.listeners: list[ResolvedCb] = []

    def on_fill(self, fill: Fill, venue: str, fill_id: int = 0) -> FillRecord:
        price = float(fill.price)
        sc = markout_bps(fill.side.sign, price, fill.mid_at_fill) if fill.mid_at_fill > 0 else float("nan")
        rec = FillRecord(fill, venue, fee_bps_of(fill), sc, fill_id=fill_id)
        self.records.append(rec)
        if len(self.records) > self.keep:
            del self.records[: len(self.records) - self.keep]
        for h in self.horizons:
            self._seq += 1
            heapq.heappush(self._heap, (fill.ts + h * 1000, self._seq, h, rec))
        return rec

    def resolve(self, now: int, mid: Callable[[str], float]) -> int:
        n = 0
        while self._heap and self._heap[0][0] <= now:
            _, _, h, rec = heapq.heappop(self._heap)
            m = mid(rec.fill.symbol)
            if m <= 0:
                continue  # no price (symbol dropped): leave unresolved
            value = markout_bps(rec.fill.side.sign, float(rec.fill.price), m)
            rec.markouts[h] = value
            n += 1
            for cb in self.listeners:
                cb(rec, h, value)
        return n

    def pending(self) -> int:
        return len(self._heap)

    def symbol_stats(self, symbol: str, h: int, last_n: int | None = None, edge: bool = True) -> Stats:
        recs = [r for r in self.records if r.fill.symbol == symbol and h in r.markouts]
        if last_n is not None:
            recs = recs[-last_n:]
        return stats((r.markouts[h] - r.fee_bps) if edge else r.markouts[h] for r in recs)
