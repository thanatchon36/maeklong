"""exchangeInfo loader -> SymbolSpec + rate limits (SPEC §0.4: never hard-code filters)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from riverbank.core.models import SymbolSpec


class SpecError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RateLimit:
    limit_type: str  # REQUEST_WEIGHT | ORDERS | RAW_REQUESTS
    interval: str  # SECOND | MINUTE | DAY
    interval_num: int
    limit: int

    @property
    def window_s(self) -> int:
        unit = {"SECOND": 1, "MINUTE": 60, "HOUR": 3600, "DAY": 86400}[self.interval]
        return unit * self.interval_num

    @property
    def header_suffix(self) -> str:
        """Suffix of X-MBX-USED-WEIGHT-* / X-MBX-ORDER-COUNT-* headers, e.g. '1M', '10S'."""
        return f"{self.interval_num}{self.interval[0]}"


def _filters(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {f["filterType"]: f for f in raw.get("filters", [])}


def parse_symbol(raw: dict[str, Any]) -> SymbolSpec:
    f = _filters(raw)
    try:
        pf, lot, mn = f["PRICE_FILTER"], f["LOT_SIZE"], f["MIN_NOTIONAL"]
    except KeyError as e:
        raise SpecError(f"{raw.get('symbol')}: missing filter {e}") from e
    tick = Decimal(pf["tickSize"])
    step = Decimal(lot["stepSize"])
    if tick <= 0 or step <= 0:
        raise SpecError(f"{raw['symbol']}: non-positive tick/step")
    pp = f.get("PERCENT_PRICE", {})
    return SymbolSpec(
        symbol=raw["symbol"],
        tick=tick,
        step=step,
        min_qty=Decimal(lot["minQty"]),
        # Futures MIN_NOTIONAL uses the field `notional` (spot uses `minNotional`).
        min_notional=Decimal(mn.get("notional", mn.get("minNotional", "0"))),
        price_precision=int(raw.get("pricePrecision", 0)),
        qty_precision=int(raw.get("quantityPrecision", 0)),
        max_qty=Decimal(lot.get("maxQty", "0")),
        min_price=Decimal(pf.get("minPrice", "0")),
        max_price=Decimal(pf.get("maxPrice", "0")),
        status=raw.get("status", ""),
        contract_type=raw.get("contractType", ""),
        quote_asset=raw.get("quoteAsset", ""),
        base_asset=raw.get("baseAsset", ""),
        underlying_type=raw.get("underlyingType", ""),
        onboard_date_ms=int(raw.get("onboardDate", 0)),
        max_num_orders=int(f.get("MAX_NUM_ORDERS", {}).get("limit", 0)),
        percent_up=Decimal(pp.get("multiplierUp", "0")),
        percent_down=Decimal(pp.get("multiplierDown", "0")),
        position_control_side=f.get("POSITION_RISK_CONTROL", {}).get("positionControlSide", "NONE"),
        time_in_force=tuple(raw.get("timeInForce", ())),
    )


@dataclass(frozen=True, slots=True)
class ExchangeInfo:
    server_time: int
    rate_limits: tuple[RateLimit, ...]
    specs: dict[str, SymbolSpec]
    skipped: tuple[str, ...]  # symbols whose filters could not be parsed

    def rate_limit(self, limit_type: str, window_s: int) -> RateLimit | None:
        for rl in self.rate_limits:
            if rl.limit_type == limit_type and rl.window_s == window_s:
                return rl
        return None


def parse_exchange_info(data: dict[str, Any]) -> ExchangeInfo:
    rls = tuple(
        RateLimit(r["rateLimitType"], r["interval"], int(r["intervalNum"]), int(r["limit"]))
        for r in data.get("rateLimits", [])
    )
    specs: dict[str, SymbolSpec] = {}
    skipped: list[str] = []
    for raw in data.get("symbols", []):
        try:
            s = parse_symbol(raw)
        except (SpecError, KeyError, ArithmeticError):
            skipped.append(str(raw.get("symbol")))
            continue
        specs[s.symbol] = s
    return ExchangeInfo(int(data.get("serverTime", 0)), rls, specs, tuple(skipped))


async def fetch_exchange_info(rest: Any) -> ExchangeInfo:
    """`rest` is a RestClient; GET /fapi/v1/exchangeInfo (weight 1)."""
    return parse_exchange_info(await rest.public("GET", "/fapi/v1/exchangeInfo"))
