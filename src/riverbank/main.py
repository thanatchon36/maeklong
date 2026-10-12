"""riverbank entry point: wiring, mode selection, safety gates (SPEC §9, §11).

    MODE=shadow uv run python -m riverbank.main --config config/config.yaml

Modes: shadow (real market data, simulated fills, no keys), demo (demo-fapi, real orders with
fake money), live (production, real money — requires MODE=live + LIVE_CONFIRM + startup checks).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import json
import signal
import sys
import time
from pathlib import Path
from typing import Any

from riverbank.analytics.report import build_daily_report
from riverbank.core.clock import RealClock
from riverbank.core.config import Config, SafetyError, load_config, resolve_mode
from riverbank.core.events import Timer
from riverbank.core.models import Mode
from riverbank.engine import Engine
from riverbank.exchange.environments import ENVIRONMENTS
from riverbank.exchange.rate_limiter import RateLimiter
from riverbank.exchange.rest import RestClient
from riverbank.exchange.specs import ExchangeInfo, fetch_exchange_info
from riverbank.marketdata.pipeline import MarketDataPipeline
from riverbank.marketdata.recorder import Recorder
from riverbank.ops.alerts import Alerts
from riverbank.ops.logging import get_logger, setup_logging
from riverbank.ops.metrics import Metrics
from riverbank.orders.client_ids import ClientIdAllocator
from riverbank.risk.killswitch import install_sigusr1, persist_halt, startup_blockers, watch_kill_file
from riverbank.storage.sqlite import SqliteRepository
from riverbank.universe.screener import Screener, candidates

log = get_logger("main")


def now_ms() -> int:
    return time.time_ns() // 1_000_000


class App:
    def __init__(self, cfg: Config, mode: Mode, config_path: str) -> None:
        self.cfg = cfg
        self.mode = mode
        self.config_path = config_path
        self.env = ENVIRONMENTS[mode]
        self.stop_event = asyncio.Event()
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
        self.tasks: list[asyncio.Task[Any]] = []
        self.repo: SqliteRepository | None = None
        self.engine: Engine | None = None
        self.pipeline: MarketDataPipeline | None = None
        self.recorder: Recorder | None = None
        self.rest: RestClient | None = None
        self.limiter: RateLimiter | None = None
        self.info: ExchangeInfo | None = None
        self.alerts: Alerts | None = None
        self.screener: Screener | None = None
        self.real: Any = None  # demo/live venue wiring (orders/live_venue.py)

    # ---- kill switch ----

    def kill(self, reason: str) -> None:
        """Synchronous on purpose: HALTED within one event-loop turn (well under 1 s)."""
        log.error("kill_switch", reason=reason)
        if self.repo is not None:
            persist_halt(self.repo, f"kill switch: {reason}", self.mode.value)
        if self.engine is not None:
            self.engine.halt_all(f"kill switch: {reason}")
        if self.alerts is not None:
            self.alerts.send("CRITICAL", "kill_switch", reason)

    # ---- startup ----

    async def start(self) -> None:
        cfg, ops = self.cfg, self.cfg.ops
        state = Path(ops.state_dir)
        state.mkdir(parents=True, exist_ok=True)
        self.repo = SqliteRepository(state / "riverbank.sqlite")
        blockers = startup_blockers(self.repo, ops.kill_file)
        if blockers:
            raise SafetyError("refusing to start: " + "; ".join(blockers))
        self.alerts = Alerts("logs", enabled=ops.telegram.enabled, prefix=f"riverbank-{self.mode.value}")
        self.alerts.repo_sink = self.repo.insert_alert
        self.rest = RestClient(self.env.rest, recv_window_ms=ops.recv_window_ms)
        self.info = await fetch_exchange_info(self.rest)
        self.limiter = RateLimiter(self.info.rate_limits, weight_frac=0.6, order_frac=cfg.risk.max_order_rate_frac)
        self.rest.header_hook = self.limiter.on_headers
        self._save_exchange_info()
        metrics = Metrics()
        with contextlib.suppress(OSError):
            metrics.serve(ops.metrics_port)
        self.screener = Screener(cfg, self.rest, limiter=self.limiter)
        universe = await self._initial_universe()
        hedge = list(cfg.hedge.instruments)
        self.recorder = Recorder(ops.data_dir, flush_s=ops.recorder_flush_s, record_depth=ops.record_depth_diffs,
                                 retention_days=ops.raw_retention_days)
        self.pipeline = MarketDataPipeline(ops, self.env, self.rest, self.queue.put_nowait,
                                           sorted(set(universe + hedge)), limiter=self.limiter,
                                           recorder=self.recorder)
        o10 = self.info.rate_limit("ORDERS", 10)
        ids = ClientIdAllocator(self.repo.reserve_seq_block)
        self.engine = Engine(cfg, self.info.specs, self.pipeline.state, RealClock(), ids, repo=self.repo,
                             alerts=self.alerts, metrics=metrics, hedge_symbols=hedge,
                             md_handle=self.pipeline.handle, md_timer=self.pipeline.on_timer,
                             order_limit_10s=o10.limit if o10 else 300)
        await self._setup_venues(universe)
        self.alerts.send("INFO", "process_start", f"mode={self.mode.value} symbols={len(universe)}")
        log.info("started", mode=self.mode.value, universe=universe, hedge=hedge)

    def _save_exchange_info(self) -> None:
        assert self.rest is not None
        meta = Path(self.cfg.ops.data_dir) / "meta"
        meta.mkdir(parents=True, exist_ok=True)

        async def _save() -> None:
            assert self.rest is not None
            data = await self.rest.public("GET", "/fapi/v1/exchangeInfo")
            name = dt.datetime.now(dt.UTC).strftime("exchange_info-%Y%m%d.json")
            (meta / name).write_text(json.dumps(data))

        self.tasks.append(asyncio.create_task(_save()))

    async def _initial_universe(self) -> list[str]:
        u = self.cfg.universe
        assert self.screener is not None
        rows = await self.screener.screen_once()
        if u.static_symbols:
            ok = {r.symbol for r in rows if not r.reasons or all(x.startswith(("spread", "volume", "toxic"))
                                                                  for x in r.reasons)}
            syms = [s for s in u.static_symbols if s in ok]
            dropped = sorted(set(u.static_symbols) - set(syms))
            if dropped:
                log.warning("static_symbols_excluded", symbols=dropped)
            return syms
        return candidates(rows)

    async def _setup_venues(self, universe: list[str]) -> None:
        assert self.engine is not None and self.repo is not None
        if self.mode is Mode.SHADOW:
            repo = self.repo
            self.engine.add_paper_venue("paper", "shadow",
                                        persist_halt=lambda reason: persist_halt(repo, reason, self.mode.value))
            self.engine.set_symbols("paper", universe)
            return
        from riverbank.orders.live_venue import setup_real_venue  # demo/live wiring (M4/M5)

        self.real = await setup_real_venue(self, universe)

    # ---- tasks ----

    async def _engine_loop(self) -> None:
        assert self.engine is not None
        q = self.queue
        while True:
            ev = await q.get()
            self.engine.on_event(ev)

    async def _timer(self) -> None:
        while True:
            await asyncio.sleep(0.1)
            self.queue.put_nowait(Timer(now_ms()))

    async def _screen_loop(self) -> None:
        assert self.screener is not None
        await asyncio.sleep(self.cfg.universe.screen_interval_s)

        async def on_rows(rows: list[Any]) -> None:
            if self.cfg.universe.static_symbols:
                return
            self.apply_universe(candidates(rows))

        await self.screener.run(on_rows)

    def apply_universe(self, cands: list[str]) -> None:
        assert self.engine is not None and self.pipeline is not None
        if self.mode is Mode.SHADOW:
            self.engine.set_symbols("paper", cands)
        elif self.real is not None:
            self.real.on_candidates(cands)
        self.pipeline.set_symbols(self.engine.all_symbols())

    async def _exchange_info_refresh(self) -> None:
        assert self.rest is not None and self.engine is not None
        while True:
            await asyncio.sleep(self.cfg.ops.exchange_info_refresh_s)
            try:
                info = await fetch_exchange_info(self.rest)
                self.engine.specs.update(info.specs)
            except Exception as e:  # keep the previous specs
                log.warning("exchange_info_refresh_failed", error=repr(e))

    async def _daily_reports(self) -> None:
        last = dt.datetime.now(dt.UTC).date()
        while True:
            await asyncio.sleep(60)
            today = dt.datetime.now(dt.UTC).date()
            if today != last:
                day = last.isoformat()
                last = today
                try:
                    assert self.repo is not None
                    self.repo.flush()
                    path = await asyncio.to_thread(build_daily_report, self.repo.path, day,
                                                   self.cfg.ops.reports_dir, self.cfg)
                    log.info("daily_report", path=str(path))
                    if self.alerts:
                        self.alerts.send("INFO", "daily_report", str(path))
                except Exception as e:
                    log.error("daily_report_failed", error=repr(e))

    async def run(self) -> None:
        await self.start()
        assert self.pipeline and self.recorder and self.alerts and self.engine
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stop_event.set)
        install_sigusr1(loop, self.kill)
        await self.pipeline.start()
        coros = [self._engine_loop(), self._timer(), self._screen_loop(), self._exchange_info_refresh(),
                 self._daily_reports(), self.recorder.run(now_ms), self.alerts.run_sender(),
                 watch_kill_file(self.cfg.ops.kill_file, self.kill),
                 self.alerts.poll_commands({"/kill": lambda: (self.kill("telegram /kill"), "HALTED")[1],
                                            "/status": lambda: json.dumps(self.engine.status() if self.engine
                                                                          else {})})]
        if self.real is not None:
            coros += self.real.coroutines()
        self.tasks += [asyncio.create_task(self._guard(c)) for c in coros]
        await self.stop_event.wait()
        await self.shutdown()

    async def _guard(self, coro: Any) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("task_crashed", error=repr(e))
            if self.engine is not None:
                self.engine.halt_all(f"task crashed: {e!r}"[:200])
            self.stop_event.set()

    async def shutdown(self) -> None:
        log.info("shutting_down")
        if self.real is not None:
            with contextlib.suppress(Exception):
                await self.real.shutdown()
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.engine is not None:
            self.engine.final_snapshot(now_ms())
        if self.pipeline:
            await self.pipeline.stop()
        if self.recorder:
            await self.recorder.close(now_ms())
        if self.rest:
            await self.rest.close()
        if self.repo:
            self.repo.flush()
            self.repo.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="riverbank market maker")
    ap.add_argument("--config", default="config/config.yaml")
    args = ap.parse_args()
    cfg = load_config(args.config)
    setup_logging(cfg.ops.log_level, cfg.ops.log_levels)
    try:
        mode = resolve_mode(cfg.mode)
    except SafetyError as e:
        log.error("safety_gate", error=str(e))
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2
    if mode is Mode.REPLAY:
        print("use tools/replay.py for replay", file=sys.stderr)
        return 2
    try:
        import uvloop

        uvloop.install()
    except ImportError:
        pass
    app = App(cfg, mode, args.config)
    try:
        asyncio.run(app.run())
    except SafetyError as e:
        log.error("safety_gate", error=str(e))
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
