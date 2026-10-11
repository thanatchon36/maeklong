"""Risk limit arithmetic (pure functions, SPEC §5.6)."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal

from riverbank.core.models import Order, Side


@dataclass(frozen=True, slots=True)
class Decision:
    approved: bool
    reason: str = ""


APPROVE = Decision(True, "")


def open_qty_by_side(orders: Iterable[Order], symbol: str) -> tuple[Decimal, Decimal]:
    """Remaining quantity of our open orders (incl. pending) per side for `symbol`."""
    buy = sell = Decimal(0)
    for o in orders:
        if o.symbol != symbol or not o.is_open:
            continue
        if o.side is Side.BUY:
            buy += o.remaining
        else:
            sell += o.remaining
    return buy, sell


def worst_case_qty(pos: Decimal, open_buy: Decimal, open_sell: Decimal) -> tuple[Decimal, Decimal]:
    """(max long, max short) position if every open order on that side fills."""
    return pos + open_buy, pos - open_sell


def worst_case_abs_notional(pos: Decimal, open_buy: Decimal, open_sell: Decimal, mark: float) -> float:
    hi, lo = worst_case_qty(pos, open_buy, open_sell)
    return max(abs(float(hi)), abs(float(lo))) * mark


def is_reducing(pos: Decimal, side: Side, qty: Decimal, open_reducing: Decimal) -> bool:
    """True if the order can only reduce |position| (also counting other reducing orders)."""
    if pos > 0 and side is Side.SELL:
        return qty + open_reducing <= pos
    if pos < 0 and side is Side.BUY:
        return qty + open_reducing <= -pos
    return False


def net_beta_exposure(positions: Mapping[str, Decimal], marks: Mapping[str, float],
                      beta: Callable[[str], float]) -> float:
    return sum(float(q) * marks.get(s, 0.0) * beta(s) for s, q in positions.items())


def liquidation_distance_pct(mark: float, liq_price: float) -> float:
    if mark <= 0 or liq_price <= 0:
        return 100.0
    return abs(mark - liq_price) / mark * 100.0


def approx_isolated_liq_price(qty: float, entry: float, leverage: float, mmr: float = 0.025) -> float:
    """Approximate isolated-margin liquidation price (paper venues; real venues use the exchange's value)."""
    if qty == 0 or entry <= 0:
        return 0.0
    im = 1.0 / leverage
    if qty > 0:
        return entry * (1 - im) / (1 - mmr)
    return entry * (1 + im) / (1 + mmr)
