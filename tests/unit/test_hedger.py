"""Hedger (SPEC §5.7, OPEN_QUESTIONS A2/B23): post-only beta hedge on the first instrument that fits."""

from decimal import Decimal

import pytest

from riverbank.core.clock import SimClock
from riverbank.core.config import Config
from riverbank.core.events import BookTicker
from riverbank.core.models import OrderStatus, RiskState, Side
from riverbank.engine import Engine
from riverbank.hedge.beta import BetaTable
from riverbank.hedge.hedger import Hedger
from riverbank.marketdata.market_state import MarketState
from riverbank.orders.client_ids import ClientIdAllocator
from tests.helpers import T0, spec

PX = {"AAAUSDT": (0.9999, 1.0001), "BTCUSDT": (99.9, 100.1), "ETHUSDT": (9.99, 10.01)}


def make(betas=None, risk=None, **hedge):
    cfg = Config.model_validate({"hedge": {"hedge_threshold_usdt": 25, "hedge_urgent_usdt": 60, **hedge},
                                 "risk": risk or {}})
    specs = {"AAAUSDT": spec("AAAUSDT"),
             "BTCUSDT": spec("BTCUSDT", tick="0.1", step="0.001", min_notional="100"),
             "ETHUSDT": spec("ETHUSDT", tick="0.01", step="0.01", min_notional="20")}
    market = MarketState(list(PX), stale_ms=60_000, request_snapshot=lambda *a: None, refresh_interval_ms=0)
    table = BetaTable("BTCUSDT")
    for (s, ref), b in (betas or {}).items():
        table.set(s, ref, b)
    eng = Engine(cfg, specs, market, SimClock(T0), ClientIdAllocator(), hedge_symbols=["BTCUSDT", "ETHUSDT"],
                 beta=table.get)
    v = eng.add_paper_venue("paper", "shadow")
    for i, (s, (b, a)) in enumerate(PX.items()):
        eng.on_event(BookTicker(s, i + 1, T0, T0, b, 10.0, a, 10.0, T0))
        v.positions.set_mark(s, (a + b) / 2)
    return eng, v, Hedger(cfg, eng, table.get)


def hold(v, sym, qty):
    v.positions.adopt(sym, Decimal(str(qty)), Decimal(str(v.positions.mark(sym))), T0)


def hedges(v):
    return [o for o in v.orders.live_orders() if o.reason == "hedge"]


def test_no_hedge_below_threshold():
    _eng, v, h = make()
    hold(v, "AAAUSDT", 20)  # 20 USDT < 25
    h.evaluate(v, T0)
    assert hedges(v) == []


def test_coarse_btc_falls_back_to_eth_with_eth_beta_post_only_at_own_touch():
    _eng, v, h = make({("AAAUSDT", "ETHUSDT"): 0.8})
    hold(v, "AAAUSDT", 40)  # 40 USDT beta-1 vs BTC: BTC would need 0.4 BTC = 40 USDT < min notional 100
    h.evaluate(v, T0)
    [o] = hedges(v)
    assert (o.symbol, o.side, o.time_in_force, o.order_type) == ("ETHUSDT", Side.SELL, "GTX", "LIMIT")
    assert o.price == Decimal("10.01")             # joins the ask: passive, never crosses
    assert o.qty == Decimal("3.2")                 # 40 · 0.8 / 10.0 rounded down to the step


def test_uses_btc_first_when_it_fits():
    _eng, v, h = make(risk={"max_position_usdt": 200, "max_gross_exposure_usdt": 1000,
                           "max_net_beta_exposure_usdt": 1000})
    hold(v, "AAAUSDT", -150)
    h.evaluate(v, T0)
    [o] = hedges(v)
    assert (o.symbol, o.side, o.price, o.qty) == ("BTCUSDT", Side.BUY, Decimal("99.9"), Decimal("1.5"))


def test_nothing_fits_means_no_hedge():
    eng, v, h = make()
    eng.specs["ETHUSDT"] = spec("ETHUSDT", tick="0.01", step="0.01", min_notional="100")
    hold(v, "AAAUSDT", 40)
    h.evaluate(v, T0)
    h.evaluate(v, T0 + 5000)
    assert hedges(v) == []


def test_hedge_position_counts_toward_exposure():
    _eng, v, h = make()
    hold(v, "AAAUSDT", 40)
    hold(v, "ETHUSDT", -4)  # already hedged: 40 − 40 = 0
    h.evaluate(v, T0)
    assert hedges(v) == []
    assert h.exposure(v, "BTCUSDT") == pytest.approx(0.0, abs=0.01)


def test_hedge_size_capped_by_max_position():
    _eng, v, h = make()
    hold(v, "AAAUSDT", 70)  # 70 USDT exposure; max_position_usdt 50 per symbol
    h.evaluate(v, T0)
    [o] = hedges(v)
    assert float(o.qty) * 10.0 <= Config().risk.max_position_usdt  # risk checks notional at the mark


def test_no_hedge_and_cancel_when_halted():
    _eng, v, h = make()
    hold(v, "AAAUSDT", 40)
    h.evaluate(v, T0)
    [o] = hedges(v)
    v.risk.state = RiskState.HALTED
    h.evaluate(v, T0 + 5000)
    assert o.status is OrderStatus.PENDING_CANCEL
    assert all(x.status is OrderStatus.PENDING_CANCEL for x in hedges(v))


def test_resting_hedge_kept_when_still_valid_and_cancelled_when_too_big_or_wrong_side():
    _eng, v, h = make()
    hold(v, "AAAUSDT", 40)
    h.evaluate(v, T0)
    [o] = hedges(v)
    v.paper.advance(T0 + 1000)  # acknowledged: resting
    h.evaluate(v, T0 + 5000)
    assert hedges(v) == [o] and o.status is not OrderStatus.PENDING_CANCEL  # unchanged: kept
    hold(v, "AAAUSDT", 30)  # exposure shrank: a 4 ETH sell would now over-hedge
    h.evaluate(v, T0 + 10_000)
    assert o.status is OrderStatus.PENDING_CANCEL


def test_taker_only_if_allowed_and_urgent():
    _eng, v, h = make()
    hold(v, "AAAUSDT", 70)  # > urgent 60
    h.evaluate(v, T0)
    assert all(o.order_type == "LIMIT" and o.time_in_force == "GTX" for o in hedges(v))  # A2: post-only
    _eng2, v2, h2 = make(allow_taker=True)
    hold(v2, "AAAUSDT", 70)
    h2.evaluate(v2, T0)
    [o] = hedges(v2)
    assert (o.order_type, o.time_in_force) == ("MARKET", "IOC")


def test_on_timer_respects_eval_interval():
    _eng, v, h = make(eval_interval_s=5)
    hold(v, "AAAUSDT", 40)
    calls = []
    h.evaluate = lambda venue, now: calls.append(now)
    for t in range(0, 12_000, 100):
        h.on_timer(T0 + t)
    assert calls == [T0, T0 + 5000, T0 + 10_000]


def test_hedge_size_capped_by_gross_headroom():
    # Seen on demo: a full-size hedge pushed worst-case gross over max_gross_exposure_usdt and the risk engine
    # rejected every hedge exactly when inventory was large. The hedge must fit the remaining gross headroom.
    _eng, v, h = make(risk={"max_gross_exposure_usdt": 70})
    hold(v, "AAAUSDT", 40)  # gross 40 → 30 USDT of headroom
    h.evaluate(v, T0)
    [o] = hedges(v)
    assert (o.symbol, o.qty) == ("ETHUSDT", Decimal("3"))  # 30 USDT at mid 10, not the 40 the exposure asks for


def test_no_hedge_when_gross_headroom_below_min_notional():
    _eng, v, h = make(risk={"max_gross_exposure_usdt": 55})
    hold(v, "AAAUSDT", 40)  # 15 USDT headroom < ETH min notional 20
    h.evaluate(v, T0)
    assert hedges(v) == []
    assert v.risk.rejects == {}  # nothing was even sent to the risk check


def test_reduce_only_unwinds_a_leftover_hedge_leg():
    # Demo twin 2026-10-12: REDUCE_ONLY flattened the quoted symbols but the ETH hedge leg (48.7 of 50 USDT,
    # above the 90 % resume bar) could never shrink: the quoter never trades ETH and the hedger only ran in OK.
    # Stuck in REDUCE_ONLY forever, holding a naked ETH long. The hedger now unwinds, never past flat.
    _eng, v, h = make()
    hold(v, "ETHUSDT", 4.8)  # 48 USDT long, nothing left to hedge
    v.risk.state = RiskState.REDUCE_ONLY
    h.evaluate(v, T0)
    [o] = hedges(v)
    assert (o.symbol, o.side, o.time_in_force, o.reduce_only) == ("ETHUSDT", Side.SELL, "GTX", True)
    assert Decimal(0) < o.qty <= Decimal("4.8")


def test_reduce_only_unwind_never_flips_the_hedge():
    _eng, v, h = make()
    hold(v, "AAAUSDT", -60)  # wants a 60 USDT BUY hedge ...
    hold(v, "ETHUSDT", -2.5)  # ... but only 25 USDT of short hedge exists to close
    v.risk.state = RiskState.REDUCE_ONLY
    h.evaluate(v, T0 + 5000)
    [o] = hedges(v)
    assert (o.symbol, o.side, o.qty) == ("ETHUSDT", Side.BUY, Decimal("2.5"))


def test_reduce_only_grows_the_hedge_up_to_the_resume_bar():
    # Shadow 2026-10-13: REDUCE_ONLY on net beta (0.93 of the limit) with no hedge allowed to grow: stuck for hours.
    # The hedge now grows in REDUCE_ONLY, capped at resume_frac (0.9) of max_position so it frees the venue
    # instead of trading one breach for another.
    _eng, v, h = make()
    hold(v, "AAAUSDT", 48)  # 48 USDT to hedge; ETH may grow to 0.9 * 50 = 45 USDT
    v.risk.state = RiskState.REDUCE_ONLY
    h.evaluate(v, T0)
    [o] = hedges(v)
    assert (o.symbol, o.side, o.qty, o.reduce_only) == ("ETHUSDT", Side.SELL, Decimal("4.5"), False)
