"""Reconciliation race (seen on demo 2026-10-12): the REST snapshot is taken while our order is open, the
cancel confirms before the snapshot is processed → must not be reported as an unknown open order."""

from decimal import Decimal

from riverbank.core.clock import SimClock
from riverbank.core.config import Config
from riverbank.core.events import ReconSnapshot
from riverbank.core.models import OrderStatus, Side
from riverbank.engine import Engine
from riverbank.marketdata.market_state import MarketState
from riverbank.orders.client_ids import ClientIdAllocator
from tests.helpers import T0, spec

SYM = "XRPUSDT"


def setup(terminal_at: int):
    market = MarketState([SYM], stale_ms=60_000, request_snapshot=lambda *a: None, refresh_interval_ms=0)
    eng = Engine(Config(), {SYM: spec(SYM)}, market, SimClock(T0), ClientIdAllocator())
    v = eng.add_paper_venue("demo", "demo")
    v.positions.set_mark(SYM, 1.0)
    o = v.orders.place(SYM, Side.BUY, Decimal("0.9"), Decimal("7"), now=T0, mark=1.0)
    v.orders.resolve(o.client_id, OrderStatus.CANCELED, Decimal(0), terminal_at)
    return eng, v, o


def snapshot(o, requested_ts):
    raw = {"clientOrderId": o.client_id, "symbol": SYM, "status": "NEW", "updateTime": T0}
    return ReconSnapshot("demo", requested_ts, requested_ts + 1500, [raw], {})


def test_order_cancelled_after_the_snapshot_is_not_a_mismatch():
    requested = T0 + 4000                      # snapshot taken while the order was still open ...
    eng, v, o = setup(terminal_at=requested + 900)  # ... cancel confirmed before the snapshot is processed
    eng.on_event(snapshot(o, requested))
    assert v.recon_mismatches == 0 and v.recon_unresolved == set()


def test_order_already_terminal_when_snapshot_taken_is_still_a_mismatch():
    requested = T0 + 4000
    eng, v, o = setup(terminal_at=requested - 500)  # we believed it gone, the exchange still has it open
    eng.on_event(snapshot(o, requested))
    assert v.recon_mismatches == 1 and v.recon_unresolved == {SYM}
