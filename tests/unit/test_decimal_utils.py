from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from riverbank.core.decimal_utils import ceil_to_step, floor_to_step, fmt, is_multiple, round_to_step

steps = st.sampled_from([Decimal(s) for s in ("0.000001", "0.00001", "0.0001", "0.001", "0.01", "0.1",
                                              "1", "10", "0.5", "0.025", "0.000010")])
values = st.decimals(min_value=Decimal("0.0000001"), max_value=Decimal("1000000"), places=8,
                     allow_nan=False, allow_infinity=False)


@given(values, steps)
def test_floor_ceil_properties(v: Decimal, step: Decimal) -> None:
    f, c = floor_to_step(v, step), ceil_to_step(v, step)
    assert is_multiple(f, step) and is_multiple(c, step)
    assert f <= v <= c
    assert c - f in (Decimal(0), step)
    assert v - f < step and c - v < step


@given(values, steps)
def test_idempotent(v: Decimal, step: Decimal) -> None:
    f = floor_to_step(v, step)
    assert floor_to_step(f, step) == f
    assert ceil_to_step(f, step) == f
    assert round_to_step(f, step) == f


@given(values, steps)
def test_round_is_nearest(v: Decimal, step: Decimal) -> None:
    r = round_to_step(v, step)
    assert is_multiple(r, step)
    assert abs(r - v) <= step / 2


def test_float_input_uses_shortest_repr() -> None:
    assert floor_to_step(0.3, Decimal("0.1")) == Decimal("0.3")
    assert ceil_to_step(0.30000000000000004, Decimal("0.1")) == Decimal("0.4")


def test_exponent_matches_step() -> None:
    assert str(floor_to_step(Decimal("0.0863549"), Decimal("0.000010"))) == "0.086350"


def test_fmt_plain() -> None:
    assert fmt(Decimal("1E-7")) == "0.0000001"
    assert fmt(Decimal("100.000")) == "100"
    assert fmt(Decimal("0.0860")) == "0.086"
    assert fmt(Decimal("0")) == "0"


def test_bad_step() -> None:
    with pytest.raises(ValueError):
        floor_to_step(Decimal(1), Decimal(0))
