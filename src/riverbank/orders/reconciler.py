"""Reconciliation (SPEC §5.5): every `reconcile_s` compare local orders/positions with REST.

Fetching is async (this module); the comparison and the corrective actions happen in the
engine (`Engine._on_recon`) when the `ReconSnapshot` event is processed, so they are
ordered with every other event. A mismatch → RECON_MISMATCH: log, alert, cancel all our
orders on that symbol, adopt the exchange state. A symbol that is still mismatched on three
consecutive checks is unresolvable → HALT (OPEN_QUESTIONS B5).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from riverbank.core.events import ReconSnapshot
from riverbank.exchange import endpoints as ep
from riverbank.exchange.rate_limiter import RateLimiter
from riverbank.exchange.rest import RestClient
from riverbank.ops.logging import get_logger

log = get_logger("reconciler")


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


async def fetch_snapshot(venue: str, rest: RestClient, limiter: RateLimiter | None) -> ReconSnapshot:
    requested = _now_ms()

    async def get(path: str, params: dict[str, Any] | None = None) -> Any:
        if limiter is not None:
            await limiter.acquire(ep.weight("GET", path, params), priority=True)
        return await rest.signed("GET", path, params)

    orders = await get(ep.OPEN_ORDERS)
    risk = await get(ep.POSITION_RISK)
    acct = await get(ep.ACCOUNT)
    positions: dict[str, tuple[Decimal, Decimal, Decimal]] = {}
    for p in risk:
        if p.get("positionSide", "BOTH") != "BOTH":
            continue
        positions[p["symbol"]] = (Decimal(str(p.get("positionAmt", "0"))), Decimal(str(p.get("entryPrice", "0"))),
                                  Decimal(str(p.get("liquidationPrice", "0") or "0")))
    wallet = margin = None
    for a in acct.get("assets", []):
        if a.get("asset") == "USDT":
            wallet = float(a.get("walletBalance", 0))
            margin = float(a.get("marginBalance", 0))
    if wallet is None and "totalWalletBalance" in acct:
        wallet = float(acct["totalWalletBalance"])
        margin = float(acct.get("totalMarginBalance", wallet))
    return ReconSnapshot(venue, requested, _now_ms(), list(orders), positions, wallet, margin)


class Reconciler:
    def __init__(self, venue: str, rest: RestClient, limiter: RateLimiter | None, sink: Callable[[Any], None],
                 interval_s: float) -> None:
        self.venue = venue
        self.rest = rest
        self.limiter = limiter
        self.sink = sink
        self.interval_s = interval_s
        self._now = asyncio.Event()
        self.failures = 0
        self.snapshots = 0

    def request_now(self) -> None:
        self._now.set()

    async def run(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._now.wait(), timeout=self.interval_s)
            except TimeoutError:
                pass
            self._now.clear()
            try:
                snap = await fetch_snapshot(self.venue, self.rest, self.limiter)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.failures += 1
                log.error("recon_fetch_failed", error=repr(e))
                continue
            self.snapshots += 1
            self.sink(snap)
