"""M4 fault injection against an in-process fake Binance (SPEC §10):
kill websocket, 503-unknown, 429, drop user-data stream, kill process (dead-man), recon mismatch,
kill switch < 1 s, clean startup/shutdown.
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from types import SimpleNamespace

import pytest

from riverbank.core.clock import RealClock
from riverbank.core.config import Config, SafetyError
from riverbank.core.events import BookTicker, BookTopN, MarkPrice, StreamStatus, Timer
from riverbank.core.models import Mode, OrderStatus, RiskState
from riverbank.engine import Engine
from riverbank.exchange.environments import Environment
from riverbank.exchange.rate_limiter import RateLimiter
from riverbank.exchange.specs import parse_exchange_info
from riverbank.marketdata.market_state import MarketState
from riverbank.ops.alerts import Alerts
from riverbank.orders import gateway as gw_mod
from riverbank.orders.client_ids import ClientIdAllocator
from riverbank.orders.live_venue import setup_real_venue
from riverbank.risk.killswitch import halt_record, watch_kill_file
from riverbank.storage.sqlite import SqliteRepository
from tests.integration.fake_binance import FIX, UNKNOWN_503, FakeBinance

SYM = "DOGEUSDT"
BID, ASK = 0.08635, 0.08636


def now_ms() -> int:
    return time.time_ns() // 1_000_000


class Harness:
    def __init__(self, tmp_path, monkeypatch):
        self.tmp = tmp_path
        self.mp = monkeypatch
        self.fake = FakeBinance()
        self.tasks: list[asyncio.Task] = []
        self.feed = True

    async def start(self, **cfg_over):
        base = await self.fake.start()
        self.fake.set_book(SYM, str(BID), str(ASK))
        env = Environment("fake", base, base.replace("http", "ws"), True)
        raw = {"quoting": {"sigma_warmup_samples": 2, "min_requote_interval_ms": 0},
               "ops": {"deadman_heartbeat_s": 1, "deadman_countdown_s": 5, "reconcile_s": 1, "recon_grace_ms": 300,
                       "state_dir": str(self.tmp), "kill_file": str(self.tmp / "KILL")},
               "universe": {"static_symbols": [SYM]}}
        for k, v in cfg_over.items():
            raw.setdefault(k, {}).update(v)
        cfg = Config.model_validate(raw)
        import json

        info = parse_exchange_info(json.loads(FIX.read_text()))
        self.repo = SqliteRepository(self.tmp / "state.sqlite")
        limiter = RateLimiter(info.rate_limits, weight_frac=0.9, order_frac=0.9)
        self.queue: asyncio.Queue = asyncio.Queue()
        market = MarketState([SYM], stale_ms=cfg.ops.stale_ms, request_snapshot=lambda *a: None,
                             refresh_interval_ms=0)
        self.alerts = Alerts(self.tmp / "logs", enabled=False)
        self.engine = Engine(cfg, info.specs, market, RealClock(), ClientIdAllocator(self.repo.reserve_seq_block),
                             repo=self.repo, alerts=self.alerts)
        self.app = SimpleNamespace(cfg=cfg, mode=Mode.DEMO, env=env, limiter=limiter, engine=self.engine,
                                   repo=self.repo, queue=self.queue, alerts=self.alerts)
        self.mp.setenv("BINANCE_API_KEY", "testkey123456")
        self.mp.setenv("BINANCE_API_SECRET", "testsecret123456")
        self.rt = await setup_real_venue(self.app, [SYM])
        self.venue = self.engine.venues["demo"]
        self.tasks = [asyncio.create_task(c) for c in
                      [self._engine_loop(), self._timer(), self._market(), *self.rt.coroutines()]]
        return self

    async def _engine_loop(self):
        while True:
            self.engine.on_event(await self.queue.get())

    async def _timer(self):
        while True:
            await asyncio.sleep(0.1)
            self.queue.put_nowait(Timer(now_ms()))

    async def _market(self):
        u = 1
        while True:
            if self.feed:
                t = now_ms()
                u += 1
                self.queue.put_nowait(BookTopN(SYM, u, [(BID - i * 1e-5, 1000.0) for i in range(20)],
                                               [(ASK + i * 1e-5, 1000.0) for i in range(20)], t))
                self.queue.put_nowait(BookTicker(SYM, u, t, t, BID, 1000.0, ASK, 1000.0, t))
                if u % 10 == 0:
                    self.queue.put_nowait(MarkPrice(SYM, t, (BID + ASK) / 2, (BID + ASK) / 2, 0.0001,
                                                    t + 4 * 3600_000, t))
            await asyncio.sleep(0.1)

    async def wait(self, cond, timeout=10.0, what=""):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if cond():
                return True
            await asyncio.sleep(0.05)
        raise AssertionError(f"timeout waiting for {what}")

    async def stop(self, graceful=True):
        if graceful:
            await self.rt.shutdown()
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if not graceful:
            await self.rt.gateway.stop()
            await self.rt.rest.close()
        await self.fake.stop()
        self.repo.close()


@pytest.fixture
async def h(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    yield harness
    if harness.tasks:
        for t in harness.tasks:
            t.cancel()
        await asyncio.gather(*harness.tasks, return_exceptions=True)
        await harness.fake.stop()


async def test_startup_checks_cancel_stale_and_configure(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    h.fake.dual = True
    h.fake.add_order(SYM, "BUY", "0.08000", "100", "rb-DOGEUSDT-BUY-old1")
    h.fake.add_order(SYM, "BUY", "0.08000", "100", "manual-order")
    await h.start()
    try:
        assert h.fake.dual is False
        assert h.fake.margin[SYM] == "ISOLATED" and h.fake.leverage[SYM] == 2
        assert h.fake.orders["rb-DOGEUSDT-BUY-old1"]["status"] == "CANCELED"
        assert h.fake.orders["manual-order"]["status"] == "NEW"  # foreign orders are not touched
    finally:
        await h.stop()


async def test_startup_refuses_multi_assets_and_high_fee(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    h.fake.multi_assets = True
    with pytest.raises(SafetyError, match="Multi-Assets"):
        await h.start()
    await h.fake.stop()
    h2 = Harness(tmp_path / "b", monkeypatch)
    (tmp_path / "b").mkdir()
    h2.fake.maker_rate = "0.0004"  # 4 bps > configured 2 bps
    with pytest.raises(SafetyError, match="maker fee"):
        await h2.start()
    await h2.fake.stop()


async def test_quotes_fill_and_clean_shutdown(h):
    await h.start()
    await h.wait(lambda: len(h.fake.open_orders(SYM)) == 2, what="two-sided quotes on the exchange")
    bid = next(o for o in h.fake.open_orders(SYM) if o["side"] == "BUY")
    assert bid["timeInForce"] == "GTX" and Decimal(bid["price"]) < Decimal(str(ASK))
    await h.fake.fill(bid["clientOrderId"])
    await h.wait(lambda: h.venue.positions.qty(SYM) == Decimal(bid["origQty"]), what="position from user stream")
    assert h.venue.fills == 1
    await h.wait(lambda: h.fake.countdowns.get(SYM), what="dead-man heartbeat armed")
    await h.stop(graceful=True)
    assert h.fake.open_orders() == []  # SPEC §11.4: cancel all rb-* on shutdown
    assert SYM not in h.fake.countdowns  # dead-man disarmed after a clean shutdown


async def test_shadow_twin_quotes_the_same_symbols_without_touching_the_exchange(h):
    # SPEC §5.9 shadow-vs-live gap: a paper venue quotes the real venue's symbols in the same process.
    await h.start()
    twin = h.engine.venues["shadow"]
    assert twin.kind == "paper" and twin.fill_mode == "shadow"
    assert twin.symbols == h.venue.symbols == {SYM}
    await h.wait(lambda: twin.orders.has_two_sided(SYM), what="two-sided paper quotes on the twin")
    await h.wait(lambda: len(h.fake.open_orders(SYM)) == 2, what="real quotes")
    assert len(h.fake.open_orders()) == 2  # the twin's orders never reach the exchange
    twin.risk.halt("twin test halt", now_ms())
    assert h.venue.risk.state is RiskState.OK  # a twin HALT does not stop the real venue
    assert halt_record(h.repo) is None         # ... and is not persisted (it would block restarts)
    await h.stop()


async def test_503_unknown_executed_is_resolved_not_resent(h):
    h.fake.inject("POST /fapi/v1/order", 503, UNKNOWN_503, still_execute=True)
    await h.start()
    await h.wait(lambda: len(h.fake.open_orders(SYM)) == 2, what="quotes")
    # the fake books the order before it answers 503, so the client may see the 503 slightly later
    await h.wait(lambda: h.rt.gateway.stats["unknown"] == 1, what="503 unknown handled")
    posts = [r for r in h.fake.requests if r == "POST /fapi/v1/order"]
    assert len(posts) == 2  # the UNKNOWN order was NOT resent
    statuses = {o.status for o in h.venue.orders.orders.values()}
    assert OrderStatus.UNKNOWN not in statuses
    await h.stop()


async def test_503_unknown_not_executed_becomes_rejected_after_grace(h, monkeypatch):
    monkeypatch.setattr(gw_mod, "QUERY_GRACE_MS", 300)
    h.fake.inject("POST /fapi/v1/order", 503, UNKNOWN_503, still_execute=False)
    await h.start()
    await h.wait(lambda: any(o.status is OrderStatus.REJECTED and o.reject_reason == ""
                             for o in h.venue.orders.orders.values()), what="unknown -> rejected via query")
    await h.wait(lambda: len(h.fake.open_orders(SYM)) == 2, what="slot re-quoted afterwards")
    await h.stop()


async def test_429_halts_venue_and_traffic(h):
    h.fake.inject("POST /fapi/v1/order", 429, {"code": -1003, "msg": "Too many requests"})
    await h.start()
    await h.wait(lambda: h.venue.risk.state is RiskState.HALTED, what="HALTED on 429")
    assert h.app.limiter.halted and h.app.limiter.halted_until - time.time() >= 59
    assert halt_record(h.repo) is not None  # persisted: manual restart required
    await h.stop(graceful=False)


async def test_user_stream_drop_cancels_then_resumes_after_recon(h):
    await h.start()
    await h.wait(lambda: len(h.fake.open_orders(SYM)) == 2, what="quotes")
    first = {o["clientOrderId"] for o in h.fake.open_orders(SYM)}
    await h.fake.drop_user_streams()
    await h.wait(lambda: not h.fake.open_orders(SYM), what="quotes cancelled on stream loss")
    await h.wait(lambda: h.venue.user_stream_ok, what="stream reconnected")
    await h.wait(lambda: len(h.fake.open_orders(SYM)) == 2, timeout=15, what="quoting resumed after recon")
    assert first.isdisjoint({o["clientOrderId"] for o in h.fake.open_orders(SYM)})
    await h.stop()


async def test_process_kill_deadman_cancels_on_exchange(h):
    await h.start()
    await h.wait(lambda: len(h.fake.open_orders(SYM)) == 2, what="quotes")
    await h.wait(lambda: h.fake.countdowns.get(SYM), what="armed")
    # simulate kill -9: every task stops at once, nothing is cancelled by us
    for t in h.tasks:
        t.cancel()
    await asyncio.gather(*h.tasks, return_exceptions=True)
    h.tasks = []
    assert len(h.fake.open_orders(SYM)) == 2
    await asyncio.sleep(5.5)
    assert h.fake.open_orders(SYM) == [] and h.fake.countdown_cancels >= 2
    await h.rt.gateway.stop()
    await h.rt.rest.close()
    await h.fake.stop()
    h.repo.close()


async def test_recon_mismatch_cancels_adopts_and_resolves(h):
    await h.start()
    await h.wait(lambda: len(h.fake.open_orders(SYM)) == 2, what="quotes")
    h.fake.positions[SYM] = Decimal(500)  # silent position change (e.g. manual trade)
    h.fake.entry[SYM] = Decimal("0.0863")
    h.fake.add_order(SYM, "SELL", "0.09000", "100", "rb-DOGEUSDT-SELL-ghost")
    await h.wait(lambda: h.venue.recon_mismatches >= 1, what="RECON_MISMATCH")
    await h.wait(lambda: h.venue.positions.qty(SYM) == Decimal(500), what="exchange position adopted")
    await h.wait(lambda: h.fake.orders["rb-DOGEUSDT-SELL-ghost"]["status"] == "CANCELED", what="ghost cancelled")
    await h.wait(lambda: not h.venue.recon_unresolved, timeout=10, what="mismatch resolved")
    h.repo.flush()
    rows = h.repo.query("SELECT resolved FROM recon_events")
    assert rows and all(r[0] == 1 for r in rows)
    await h.stop()


async def test_market_ws_kill_pulls_quotes(h):
    await h.start()
    await h.wait(lambda: len(h.fake.open_orders(SYM)) == 2, what="quotes")
    h.feed = False
    h.queue.put_nowait(StreamStatus("public", 0, False, (SYM,), now_ms(), "killed"))
    await h.wait(lambda: not h.fake.open_orders(SYM), what="quotes pulled on market-data loss")
    await h.stop()


async def test_kill_switch_under_one_second(h):
    await h.start()
    await h.wait(lambda: len(h.fake.open_orders(SYM)) == 2, what="quotes")
    kill = h.tmp / "KILL"
    halted_at = {}

    def on_kill(reason):
        h.engine.halt_all(f"kill switch: {reason}")
        halted_at["t"] = time.monotonic()

    watcher = asyncio.create_task(watch_kill_file(kill, on_kill))
    t0 = time.monotonic()
    kill.touch()
    await h.wait(lambda: h.venue.risk.state is RiskState.HALTED, timeout=2, what="HALTED")
    latency = halted_at["t"] - t0
    assert latency < 1.0, latency
    await h.wait(lambda: not h.fake.open_orders(SYM), what="orders cancelled after kill")
    watcher.cancel()
    await h.stop(graceful=False)
