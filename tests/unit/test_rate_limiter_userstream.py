from decimal import Decimal

import pytest

from riverbank.core.events import AccountUpdate, OrderUpdate
from riverbank.exchange.rate_limiter import RateLimiter, RateLimitHalted
from riverbank.exchange.specs import RateLimit
from riverbank.exchange.user_stream import parse_user_event
from riverbank.orders.live_venue import TradingRulesMonitor

LIMITS = (RateLimit("REQUEST_WEIGHT", "MINUTE", 1, 2400), RateLimit("ORDERS", "MINUTE", 1, 1200),
          RateLimit("ORDERS", "SECOND", 10, 300))


class FakeClock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_weight_window_budget_and_reset():
    c = FakeClock(600.0)  # aligned to a minute boundary
    rl = RateLimiter(LIMITS, weight_frac=0.5, order_frac=0.5, clock=c)
    assert rl.try_acquire(1000) == 0.0
    assert rl.try_acquire(200) == 0.0
    wait = rl.try_acquire(1)  # 1200 = 50 % of 2400 used
    assert 59.0 < wait <= 60.0
    c.t += 60.0
    assert rl.try_acquire(1) == 0.0


def test_order_limits_self_imposed_fraction_and_priority():
    c = FakeClock(1000.0)
    rl = RateLimiter(LIMITS, order_frac=0.5, priority_frac=0.95, clock=c)
    for _ in range(150):  # 50 % of 300 per 10 s
        assert rl.try_acquire(0, orders=1) == 0.0
    assert rl.try_acquire(0, orders=1) > 0
    assert rl.try_acquire(0, orders=1, priority=True) == 0.0  # reduce-only/cancel headroom


def test_header_drift_correction():
    c = FakeClock(1200.0)
    rl = RateLimiter(LIMITS, weight_frac=1.0, clock=c)
    rl.try_acquire(10)
    rl.on_headers({"X-MBX-USED-WEIGHT-1M": "2000", "x-mbx-order-count-10s": "7"})
    assert rl.weight[0].used == 2000
    assert next(o for o in rl.orders if o.window_s == 10).used == 7
    rl.on_headers({"X-MBX-USED-WEIGHT-1M": "5"})  # never decreases within a window
    assert rl.weight[0].used == 2000
    rl.on_headers({"X-MBX-USED-WEIGHT-1M": "2390"}, ignore_weight=True)  # e.g. bookTicker endpoint
    assert rl.weight[0].used == 2000


def test_halt_on_429_blocks_everything_for_at_least_60s():
    c = FakeClock(1000.0)
    rl = RateLimiter(LIMITS, clock=c)
    rl.halt(7, "429")
    with pytest.raises(RateLimitHalted):
        rl.try_acquire(1, priority=True)
    c.t += 59
    assert rl.halted
    c.t += 2
    assert not rl.halted and rl.try_acquire(1) == 0.0


ORDER_TRADE_UPDATE = {
    "e": "ORDER_TRADE_UPDATE", "E": 1568879465651, "T": 1568879465650,
    "o": {"s": "DOGEUSDT", "c": "rb-DOGEUSDT-BUY-1a", "S": "BUY", "o": "LIMIT", "f": "GTX", "q": "115",
          "p": "0.08632", "ap": "0.08632", "sp": "0", "x": "TRADE", "X": "PARTIALLY_FILLED", "i": 8886774,
          "l": "15", "z": "15", "L": "0.08632", "N": "USDT", "n": "0.00025896", "T": 1568879465650, "t": 77,
          "b": "0", "a": "0", "m": True, "R": False, "wt": "CONTRACT_PRICE", "ot": "LIMIT", "ps": "BOTH",
          "cp": False, "rp": "0", "pP": False, "si": 0, "ss": 0, "V": "EXPIRE_MAKER", "pm": "NONE", "gtd": 0,
          "er": "0"}}
ACCOUNT_UPDATE = {
    "e": "ACCOUNT_UPDATE", "E": 1564745798939, "T": 1564745798938,
    "a": {"m": "ORDER", "B": [{"a": "USDT", "wb": "122624.12345678", "cw": "100.12345678", "bc": "50.12345678"}],
          "P": [{"s": "DOGEUSDT", "pa": "15", "ep": "0.08632", "bep": "0.08632", "cr": "0", "up": "-0.01",
                 "mt": "isolated", "iw": "0.43", "ps": "BOTH"},
                {"s": "XRPUSDT", "pa": "5", "ep": "1", "bep": "1", "cr": "0", "up": "0", "mt": "isolated",
                 "iw": "1", "ps": "LONG"}]}}


def test_parse_order_trade_update():
    ev = parse_user_event(ORDER_TRADE_UPDATE, 5, "demo")
    assert isinstance(ev, OrderUpdate)
    assert ev.client_id == "rb-DOGEUSDT-BUY-1a" and ev.exec_type == "TRADE" and ev.status == "PARTIALLY_FILLED"
    assert ev.last_fill_qty == Decimal("15") and ev.filled_qty == Decimal("15") and ev.trade_id == 77
    assert ev.fee == Decimal("0.00025896") and ev.is_maker and ev.venue == "demo" and ev.exchange_id == 8886774


def test_parse_account_update_one_way_only():
    ev = parse_user_event(ACCOUNT_UPDATE, 5, "demo")
    assert isinstance(ev, AccountUpdate) and ev.reason == "ORDER"
    assert ev.balances["USDT"] == Decimal("122624.12345678")
    assert [p.symbol for p in ev.positions] == ["DOGEUSDT"]  # hedge-mode legs ignored
    assert parse_user_event({"e": "listenKeyExpired", "E": 1}, 5, "demo") == "listenKeyExpired"
    assert parse_user_event({"e": "TRADE_LITE"}, 5, "demo") is None


def test_trading_rules_monitor_pauses_near_trigger():
    alerts = []

    class A:
        def send(self, level, kind, msg, **kw):
            alerts.append((level, kind))

    m = TradingRulesMonitor(None, None, A(), pause_s=60)
    m.apply({"indicators": {"DOGEUSDT": [{"isLocked": False, "plannedRecoverTime": 0, "indicator": "UFR",
                                           "value": 0.95, "triggerValue": 1.0}],
                            "XRPUSDT": [{"isLocked": False, "indicator": "IFER", "value": 0.75,
                                         "triggerValue": 1.0}]}}, 1000)
    assert m.gate("DOGEUSDT", 2000) == "trading_rules_UFR"
    assert m.gate("DOGEUSDT", 62_000) is None
    assert m.gate("XRPUSDT", 2000) is None
    assert ("CRITICAL", "trading_rules") in alerts and ("WARN", "trading_rules") in alerts


async def test_real_venue_requires_credentials(monkeypatch):
    from types import SimpleNamespace

    from riverbank.core.config import SafetyError
    from riverbank.core.models import Mode
    from riverbank.orders.live_venue import setup_real_venue

    for k in ("BINANCE_API_KEY", "BINANCE_API_SECRET", "BINANCE_PRIVATE_KEY_PATH"):
        monkeypatch.delenv(k, raising=False)
    app = SimpleNamespace(mode=Mode.DEMO, cfg=None)
    with pytest.raises(SafetyError, match="BINANCE_API_KEY"):
        await setup_real_venue(app, ["DOGEUSDT"])
