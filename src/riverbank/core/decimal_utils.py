"""Exact tick/step rounding at the exchange boundary.

All prices and quantities sent to Binance go through these helpers. Inputs are
`Decimal` (or anything `Decimal(str(x))` accepts); floats are converted via their
shortest repr so `0.1` becomes `Decimal("0.1")`, not the binary expansion.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal

ZERO = Decimal(0)


def to_decimal(x: Decimal | str | int | float) -> Decimal:
    if isinstance(x, Decimal):
        return x
    if isinstance(x, float):
        return Decimal(repr(x))
    return Decimal(x)


def _check_step(step: Decimal) -> None:
    if step <= ZERO:
        raise ValueError(f"step must be positive, got {step}")


def floor_to_step(value: Decimal | float | str, step: Decimal) -> Decimal:
    """Largest multiple of `step` that is <= value."""
    _check_step(step)
    v = to_decimal(value)
    n = (v / step).to_integral_value(rounding=ROUND_FLOOR)
    return (n * step).quantize(step)


def ceil_to_step(value: Decimal | float | str, step: Decimal) -> Decimal:
    """Smallest multiple of `step` that is >= value."""
    _check_step(step)
    v = to_decimal(value)
    n = (v / step).to_integral_value(rounding=ROUND_CEILING)
    return (n * step).quantize(step)


def round_to_step(value: Decimal | float | str, step: Decimal) -> Decimal:
    """Nearest multiple of `step` (banker's rounding on exact ties)."""
    _check_step(step)
    v = to_decimal(value)
    n = (v / step).to_integral_value(rounding=ROUND_HALF_EVEN)
    return (n * step).quantize(step)


def is_multiple(value: Decimal, step: Decimal) -> bool:
    _check_step(step)
    return (value % step) == ZERO


def fmt(value: Decimal) -> str:
    """Plain decimal string for API parameters (never scientific notation)."""
    s = format(value, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"
