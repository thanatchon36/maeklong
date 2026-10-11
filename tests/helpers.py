"""Shared test builders."""

from __future__ import annotations

from decimal import Decimal

from riverbank.core.config import Config
from riverbank.core.models import Features, SymbolSpec

T0 = 1791763200000  # 2026-10-12T00:00:00Z


def spec(sym: str = "AAAUSDT", tick: str = "0.0001", step: str = "1", min_notional: str = "5",
         **kw: object) -> SymbolSpec:
    base: dict[str, object] = dict(symbol=sym, tick=Decimal(tick), step=Decimal(step), min_qty=Decimal(step),
                                   min_notional=Decimal(min_notional), price_precision=4, qty_precision=0,
                                   onboard_date_ms=T0 - 100 * 86400_000, time_in_force=("GTC", "GTX", "IOC"),
                                   percent_up=Decimal("1.1"), percent_down=Decimal("0.9"))
    base.update(kw)
    return SymbolSpec(**base)  # type: ignore[arg-type]


def cfg(**sections: dict[str, object]) -> Config:
    return Config.model_validate(sections)


def feats(mid: float = 1.0, spread: float = 0.0004, sigma: float = 1.0, fair: float | None = None,
          ready: bool = True, sym: str = "AAAUSDT", ts: int = T0) -> Features:
    bid, ask = mid - spread / 2, mid + spread / 2
    return Features(sym, ts, mid, mid, 0.0, sigma, 0.0, fair if fair is not None else mid, bid, ask, ready)
