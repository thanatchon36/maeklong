"""Rolling beta of each symbol vs the hedge instruments (SPEC §5.7, OPEN_QUESTIONS B23).

beta = cov(r_sym, r_ref) / var(r_ref) on 1-min log returns over `hedge.beta_window_days`, then shrunk:
beta = (1 − shrink)·beta_ols + shrink·1.0. Too little data (< `min_obs` overlapping returns) → prior 1.0.
Closes come from public REST klines (completed candles only), refreshed every `hedge.beta_refresh_s`.
"""

from __future__ import annotations

import asyncio
import itertools
import math
import time
from collections.abc import Iterable, Sequence
from typing import Any

from riverbank.exchange import endpoints as ep
from riverbank.ops.logging import get_logger

log = get_logger("beta")

MIN_MS = 60_000
KLINES_LIMIT = 1500


def log_returns(closes: Sequence[tuple[int, float]]) -> dict[int, float]:
    """{open time of the later candle: log return}; only consecutive candles (gaps are skipped)."""
    out: dict[int, float] = {}
    for (t0, p0), (t1, p1) in itertools.pairwise(closes):
        if t1 - t0 == MIN_MS and p0 > 0 and p1 > 0:
            out[t1] = math.log(p1 / p0)
    return out


def estimate_beta(sym: dict[int, float], ref: dict[int, float], shrink: float,
                  min_obs: int = 360) -> tuple[float, int]:
    keys = sorted(sym.keys() & ref.keys())
    n = len(keys)
    if n < min_obs:
        return 1.0, n
    xs, ys = [ref[k] for k in keys], [sym[k] for k in keys]
    mx, my = sum(xs) / n, sum(ys) / n
    var = sum((x - mx) ** 2 for x in xs)
    if var <= 0:
        return 1.0, n
    raw = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / var
    return (1 - shrink) * raw + shrink * 1.0, n


class BetaTable:
    def __init__(self, default_ref: str, shrink: float = 0.3, min_obs: int = 360) -> None:
        self.default_ref = default_ref
        self.shrink = shrink
        self.min_obs = min_obs
        self.betas: dict[tuple[str, str], float] = {}

    def set(self, symbol: str, ref: str, beta: float) -> None:
        self.betas[(symbol, ref)] = beta

    def get(self, symbol: str, ref: str | None = None) -> float:
        ref = ref or self.default_ref
        if symbol == ref:
            return 1.0
        return self.betas.get((symbol, ref), 1.0)

    async def _closes(self, rest: Any, symbol: str, start: int, now_ms: int, limiter: Any) -> list[tuple[int, float]]:
        out: list[tuple[int, float]] = []
        while start < now_ms:
            params = {"symbol": symbol, "interval": "1m", "startTime": start, "limit": KLINES_LIMIT}
            if limiter is not None:
                await limiter.acquire(ep.weight("GET", ep.KLINES, params))
            rows = await rest.public("GET", ep.KLINES, params)
            if not rows:
                break
            out += [(int(r[0]), float(r[4])) for r in rows if int(r[6]) < now_ms]  # completed candles only
            start = int(rows[-1][0]) + MIN_MS
        return out

    async def refresh(self, rest: Any, symbols: Iterable[str], refs: Sequence[str], window_days: float,
                      now_ms: int, limiter: Any = None) -> None:
        start = now_ms - int(window_days * 86_400_000)
        rets = {}
        for s in sorted(set(symbols) | set(refs)):
            rets[s] = log_returns(await self._closes(rest, s, start, now_ms, limiter))
        for s in sorted(rets):
            for ref in refs:
                if s != ref:
                    beta, n = estimate_beta(rets[s], rets[ref], self.shrink, self.min_obs)
                    self.set(s, ref, beta)
                    log.info("beta", symbol=s, ref=ref, beta=round(beta, 4), n=n)

    async def run(self, rest: Any, symbols: Any, refs: Sequence[str], window_days: float, every_s: float,
                  limiter: Any = None) -> None:
        """Refresh forever; `symbols()` is called each time (the universe changes). Failures keep old betas."""
        while True:
            try:
                await self.refresh(rest, symbols(), refs, window_days, time.time_ns() // 1_000_000, limiter)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("beta_refresh_failed", error=repr(e))
            await asyncio.sleep(every_s)
