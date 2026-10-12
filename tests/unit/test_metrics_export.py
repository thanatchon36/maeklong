"""Every dashboard panel's metric is actually exported (markout, quote uptime, rate limits, WS latency, errors)."""

from decimal import Decimal

from riverbank.analytics.markout import FillRecord
from riverbank.core.clock import SimClock
from riverbank.core.config import Config
from riverbank.core.events import BookTicker, Timer
from riverbank.core.models import Fill, Side
from riverbank.engine import Engine
from riverbank.marketdata.market_state import MarketState
from riverbank.ops import logging as rb_logging
from riverbank.ops.metrics import Metrics
from riverbank.orders.client_ids import ClientIdAllocator
from tests.helpers import T0, spec

SYM = "AAAUSDT"


def make():
    m = Metrics()
    market = MarketState([SYM], stale_ms=60_000, request_snapshot=lambda *a: None, refresh_interval_ms=0)
    clock = SimClock(T0)
    eng = Engine(Config(), {SYM: spec(SYM)}, market, clock, ClientIdAllocator(), metrics=m)
    v = eng.add_paper_venue("paper", "shadow")
    eng.set_symbols("paper", [SYM])
    return eng, v, m, clock


def val(m, name, **labels):
    return m.registry.get_sample_value(name, labels)


def test_ws_latency_uses_exchange_event_time_and_clock_offset():
    eng, _, m, clock = make()
    clock.offset_ms = 10  # server − local
    eng.on_event(BookTicker(SYM, 1, T0 - 30, T0 - 30, 0.9999, 1.0, 1.0001, 1.0, T0))
    assert val(m, "rb_ws_latency_ms_count", stream="BookTicker") == 1
    assert val(m, "rb_ws_latency_ms_sum", stream="BookTicker") == 40  # (T0 + 10) − (T0 − 30)


def test_markout_per_horizon_exported():
    eng, v, m, _ = make()
    f = Fill(T0, SYM, Side.BUY, Decimal("1"), Decimal("10"), Decimal("0"), True, "shadow", "c1", 1.0)
    v.markouts.records.append(FillRecord(f, "paper", 0.0, 0.0, {1: 1.5, 60: -2.0}))
    eng.on_event(Timer(T0 + 1000))
    assert val(m, "rb_markout_bps", venue="paper", symbol=SYM, horizon="1") == 1.5
    assert val(m, "rb_markout_bps", venue="paper", symbol=SYM, horizon="60") == -2.0
    assert val(m, "rb_markout_bps", venue="paper", symbol=SYM, horizon="5") is None  # nothing resolved yet


def test_quote_uptime_today_ratio_and_utc_day_reset():
    eng, v, m, _ = make()
    two_sided = {"on": True}
    v.orders.has_two_sided = lambda s: two_sided["on"]
    eng.on_event(Timer(T0 + 1000))
    eng.on_event(Timer(T0 + 2000))   # 1 s quoted
    two_sided["on"] = False
    eng.on_event(Timer(T0 + 3000))   # 1 s not quoted
    eng.on_event(Timer(T0 + 4000))
    assert val(m, "rb_quote_uptime_ratio", venue="paper", symbol=SYM) == 1 / 3
    day2 = T0 + 86_400_000
    two_sided["on"] = True
    eng.on_event(Timer(day2))
    eng.on_event(Timer(day2 + 1000))
    eng.on_event(Timer(day2 + 2000))
    assert val(m, "rb_quote_uptime_ratio", venue="paper", symbol=SYM) == 1.0  # new UTC day starts fresh


def test_rate_limit_usage_exported():
    m = Metrics()
    m.set_rate_limits({"x-mbx-used-weight-1m": 0.25, "x-mbx-order-count-10s": 0.1})
    assert val(m, "rb_rate_limit_usage_ratio", limit="x-mbx-used-weight-1m") == 0.25
    assert val(m, "rb_rate_limit_usage_ratio", limit="x-mbx-order-count-10s") == 0.1


def test_error_logs_counted_by_event(capsys):
    m = Metrics()
    hook = m.count_error
    rb_logging.add_error_hook(hook)
    try:
        log = rb_logging.get_logger("test")
        log.error("RECON_MISMATCH", symbol=SYM)
        log.critical("RECON_MISMATCH")
        log.warning("not_an_error")
        log.info("fine")
    finally:
        rb_logging.remove_error_hook(hook)
    assert val(m, "rb_errors_total", kind="RECON_MISMATCH") == 2
    assert val(m, "rb_errors_total", kind="not_an_error") is None
