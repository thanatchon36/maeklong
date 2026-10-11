"""Synthetic recorded market data (written through the real Recorder) for replay tests."""

from __future__ import annotations

import random
from pathlib import Path

from riverbank.core.events import AggTrade, BookTicker, BookTopN, MarkPrice
from riverbank.marketdata.recorder import Recorder


async def write_dataset(root: str | Path, symbols: dict[str, float], start_ms: int, minutes: float, *,
                        seed: int = 7, tick: float = 0.0001, step_bps: float = 0.6, trade_rate: float = 4.0,
                        funding_ms: int | None = None, drift_bps: dict[str, float] | None = None) -> None:
    rng = random.Random(seed)
    rec = Recorder(root, record_depth=False)
    n_ticks = int(minutes * 600)
    for sym, p0 in symbols.items():
        mid = p0
        u = 1
        a = 1
        drift = (drift_bps or {}).get(sym, 0.0)
        for i in range(n_ticks):
            ts = start_ms + i * 100
            mid *= 1 + (rng.gauss(0, step_bps) + drift) / 1e4
            bid = round(mid / tick - 0.5) * tick
            ask = bid + tick
            bq, aq = rng.uniform(50, 300), rng.uniform(50, 300)
            u += rng.randint(1, 5)
            rec.on_event(BookTicker(sym, u, ts, ts, round(bid, 8), bq, round(ask, 8), aq, ts))
            levels_b = [(round(bid - k * tick, 8), rng.uniform(50, 500)) for k in range(20)]
            levels_a = [(round(ask + k * tick, 8), rng.uniform(50, 500)) for k in range(20)]
            levels_b[0] = (round(bid, 8), bq)
            levels_a[0] = (round(ask, 8), aq)
            rec.on_event(BookTopN(sym, u, levels_b, levels_a, ts))
            for _ in range(_poisson(rng, trade_rate / 10)):
                buyer = rng.random() < 0.5
                px = ask if buyer else bid
                if rng.random() < 0.15:  # sweep through a few ticks
                    px = px + (tick * rng.randint(1, 6) * (1 if buyer else -1))
                qty = rng.uniform(1, 400)
                a += 1
                tts = ts + rng.randint(1, 99)
                rec.on_event(AggTrade(sym, a, round(px, 8), qty, qty, tts, tts, not buyer, tts))
            if i % 10 == 0:
                nxt = funding_ms if funding_ms and ts < funding_ms else (funding_ms or start_ms) + 8 * 3600_000
                rec.on_event(MarkPrice(sym, ts, mid, mid, 0.0001, nxt, ts))
    await rec.flush()


def _poisson(rng: random.Random, lam: float) -> int:
    n, p, limit = 0, 1.0, pow(2.718281828459045, -lam)
    while True:
        p *= rng.random()
        if p <= limit:
            return n
        n += 1
