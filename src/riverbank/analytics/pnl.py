"""PnL decomposition (SPEC §5.9) per symbol and total.

From cumulative position-book snapshots at the window start/end plus the fills inside:
  trading       = Δrealized + Δunrealized               (before fees/funding)
  spread        = Σ side · (mid_at_fill − price) · qty   (captured half-spread at fill time)
  inventory     = trading − spread                       (mark-to-market of carried inventory)
  fees          = Δfees,  funding = Δfunding
  net           = trading − fees + funding
For hedge instruments the whole trading result is reported as `hedge`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(slots=True)
class PnlRow:
    symbol: str
    spread: float = 0.0
    inventory: float = 0.0
    hedge: float = 0.0
    fees: float = 0.0
    funding: float = 0.0
    trading: float = 0.0
    net: float = 0.0
    n_fills: int = 0
    volume_usdt: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


_ZERO_SNAP = {"realized": 0.0, "unrealized": 0.0, "fees": 0.0, "funding": 0.0}


def decompose(fills: Iterable[Mapping[str, Any]], start: Mapping[str, Mapping[str, float]],
              end: Mapping[str, Mapping[str, float]], hedge_symbols: Iterable[str] = ()) -> dict[str, PnlRow]:
    hedge = set(hedge_symbols)
    rows: dict[str, PnlRow] = {}
    for f in fills:
        sym = f["symbol"]
        r = rows.setdefault(sym, PnlRow(sym))
        price, qty = float(f["price"]), float(f["qty"])
        sign = 1 if f["side"] == "BUY" else -1
        mid = float(f.get("mid_at_fill") or 0.0)
        if mid > 0:
            r.spread += sign * (mid - price) * qty
        r.n_fills += 1
        r.volume_usdt += price * qty
    for sym in set(start) | set(end):
        rows.setdefault(sym, PnlRow(sym))
    for sym, r in rows.items():
        s, e = start.get(sym, _ZERO_SNAP), end.get(sym, _ZERO_SNAP)
        r.trading = (e["realized"] - s["realized"]) + (e["unrealized"] - s["unrealized"])
        r.fees = e["fees"] - s["fees"]
        r.funding = e["funding"] - s["funding"]
        if sym in hedge:
            r.hedge, r.spread, r.inventory = r.trading, 0.0, 0.0
        else:
            r.inventory = r.trading - r.spread
        r.net = r.trading - r.fees + r.funding
    return rows


def total(rows: Mapping[str, PnlRow]) -> PnlRow:
    t = PnlRow("TOTAL")
    for r in rows.values():
        t.spread += r.spread
        t.inventory += r.inventory
        t.hedge += r.hedge
        t.fees += r.fees
        t.funding += r.funding
        t.trading += r.trading
        t.net += r.net
        t.n_fills += r.n_fills
        t.volume_usdt += r.volume_usdt
    return t
