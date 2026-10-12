"""SPEC §5.11: alert on a market-data disconnect lasting > 30 s (once), and when it recovers."""

from riverbank.core.clock import RealClock
from riverbank.core.events import StreamStatus, Timer
from riverbank.engine import Engine
from riverbank.marketdata.market_state import MarketState
from riverbank.orders.client_ids import ClientIdAllocator
from tests.helpers import T0, cfg, spec

SYM = "AAAUSDT"


class RecordingAlerts:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def send(self, level: str, kind: str, message: str, **kw: object) -> None:
        self.sent.append((level, kind, message))

    def kinds(self) -> list[str]:
        return [k for _, k, _ in self.sent]


def make_engine() -> tuple[Engine, RecordingAlerts]:
    c = cfg()
    market = MarketState([SYM], stale_ms=c.ops.stale_ms, request_snapshot=lambda *a: None, refresh_interval_ms=0)
    alerts = RecordingAlerts()
    eng = Engine(c, {SYM: spec(SYM)}, market, RealClock(), ClientIdAllocator(), alerts=alerts)
    return eng, alerts


def test_alert_once_after_30s_down_then_on_recovery():
    eng, alerts = make_engine()
    eng.on_event(StreamStatus("public", 0, False, (SYM,), T0, "closed"))
    eng.on_event(Timer(T0 + 29_000))
    assert "md_disconnect" not in alerts.kinds()
    eng.on_event(Timer(T0 + 31_000))
    eng.on_event(Timer(T0 + 40_000))
    assert alerts.kinds().count("md_disconnect") == 1
    eng.on_event(StreamStatus("public", 0, True, (SYM,), T0 + 45_000, "connected"))
    assert alerts.kinds().count("md_reconnected") == 1


def test_short_blip_and_unsubscribe_do_not_alert():
    eng, alerts = make_engine()
    eng.on_event(StreamStatus("market", 0, False, (SYM,), T0, "closed"))
    eng.on_event(StreamStatus("market", 0, True, (SYM,), T0 + 5_000, "connected"))
    eng.on_event(StreamStatus("public", -1, False, (SYM,), T0 + 6_000, "unsubscribed"))
    eng.on_event(Timer(T0 + 60_000))
    assert "md_disconnect" not in alerts.kinds() and "md_reconnected" not in alerts.kinds()
