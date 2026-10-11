"""Order sizing (SPEC §5.4 "Order size", OPEN_QUESTIONS B14). Shared by screener and quoting."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from riverbank.core.decimal_utils import floor_to_step, to_decimal
from riverbank.core.models import SymbolSpec

CLIENT_ID_MAX = 36
SEQ_MAX_CHARS = 7  # base36 sequence (see orders/client_ids.py)
NOTIONAL_SAFETY = Decimal("1.05")  # rounded order must clear min_notional by 5 % (price drift)


@dataclass(frozen=True, slots=True)
class OrderSize:
    qty: Decimal
    notional: float
    ok: bool
    reason: str = ""
    inventory_units: int = 0  # effective max inventory in units of this order size


def client_id_fits(symbol: str) -> bool:
    return len(f"rb-{symbol}-SELL-") + SEQ_MAX_CHARS <= CLIENT_ID_MAX


def order_size(spec: SymbolSpec, price: float, order_size_usdt: float, size_buffer: float,
               max_position_usdt: float, max_inventory_units: int) -> OrderSize:
    """qty = floor_to_step(max(order_size_usdt, min_notional * size_buffer) / price)."""
    if price <= 0:
        return OrderSize(Decimal(0), 0.0, False, "no_price")
    target = max(Decimal(str(order_size_usdt)), spec.min_notional * to_decimal(size_buffer))
    qty = floor_to_step(target / to_decimal(price), spec.step)
    notional = float(qty) * price
    if qty <= 0 or qty < spec.min_qty:
        return OrderSize(qty, notional, False, "below_min_qty")
    if spec.max_qty > 0 and qty > spec.max_qty:
        return OrderSize(qty, notional, False, "above_max_qty")
    if Decimal(str(notional)) < spec.min_notional * NOTIONAL_SAFETY:
        return OrderSize(qty, notional, False, "below_min_notional")
    if notional > max_position_usdt:
        return OrderSize(qty, notional, False, "order_exceeds_max_position")
    units = min(max_inventory_units, int(max_position_usdt // notional))
    return OrderSize(qty, notional, True, "", units)
