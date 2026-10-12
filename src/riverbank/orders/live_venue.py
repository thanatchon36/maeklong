"""Real venue wiring for demo and live (SPEC §5.5 startup, §11 safety rules).

Startup checks (refuse to start on any failure):
  * credentials present (env only); live additionally passed the MODE/LIVE_CONFIRM gate in main
  * clock offset vs exchange small enough for recvWindow
  * live only: API key has NO withdraw permission, IS IP-restricted, futures enabled
    (GET /sapi/v1/account/apiRestrictions on api.binance.com)
  * Multi-Assets mode off (isolated margin is not possible in Multi-Assets mode)
  * one-way position mode (changed if needed; refuse if it cannot be changed)
  * maker commission (GET /fapi/v1/commissionRate) not above the configured fee
  * per symbol: ISOLATED margin + configured leverage, else the symbol is not traded
  * stale `rb-*` orders cancelled; positions and wallet adopted from the exchange
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Any

from riverbank.core.config import SafetyError
from riverbank.core.models import Mode, RiskState
from riverbank.exchange import endpoints as ep
from riverbank.exchange.environments import PROD_SAPI
from riverbank.exchange.rest import ApiError, RestClient, unlock_live_signing
from riverbank.exchange.signing import Credentials, credentials_from_env
from riverbank.exchange.user_stream import UserDataStream
from riverbank.ops.logging import get_logger
from riverbank.orders.client_ids import is_ours
from riverbank.orders.deadman import DeadmanSwitch
from riverbank.orders.gateway import OrderGateway
from riverbank.orders.reconciler import Reconciler, fetch_snapshot
from riverbank.risk.killswitch import persist_halt
from riverbank.universe.rotation import (
    Decision,
    last_demotions,
    last_states,
    live_edges,
    promoted_symbols,
    read_shadow_db,
    record,
    rotate,
)

log = get_logger("live_venue")

# OPEN_QUESTIONS B1: set to True only after the operator approves the M1/M3/M4 acceptance runs.
LIVE_ACCEPTANCE_APPROVED = False
SHADOW_TWIN = "shadow"  # paper venue quoting the real venue's symbols (shadow-vs-live gap report)


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


class StartupError(SafetyError):
    pass


async def _signed(rest: RestClient, limiter: Any, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
    if limiter is not None:
        await limiter.acquire(ep.weight(method, path, params), priority=True)
    return await rest.signed(method, path, params)


async def sync_time(rest: RestClient, clock: Any) -> int:
    t0 = _now_ms()
    res = await rest.public("GET", ep.TIME)
    t1 = _now_ms()
    offset = int(res["serverTime"]) - (t0 + t1) // 2
    clock.offset_ms = offset
    return offset


async def check_api_key_permissions(creds: Credentials, recv_window_ms: int) -> dict[str, Any]:
    sapi = RestClient(PROD_SAPI, creds, recv_window_ms=recv_window_ms)
    try:
        res: dict[str, Any] = await sapi.signed("GET", ep.SAPI_API_RESTRICTIONS)
    finally:
        await sapi.close()
    problems = []
    if res.get("enableWithdrawals"):
        problems.append("API key has WITHDRAW permission")
    if not res.get("ipRestrict"):
        problems.append("API key is not IP-restricted")
    if res.get("enableFutures") is False:
        problems.append("API key has futures disabled")
    if problems:
        raise StartupError("live startup check failed: " + "; ".join(problems))
    return res


async def configure_symbol(rest: RestClient, limiter: Any, sym: str, leverage: int) -> None:
    """ISOLATED margin + configured leverage; must succeed before a symbol is quoted on a real venue."""
    try:
        await _signed(rest, limiter, "POST", ep.MARGIN_TYPE, {"symbol": sym, "marginType": "ISOLATED"})
    except ApiError as e:
        if e.code != -4046:  # -4046: already isolated
            raise
    await _signed(rest, limiter, "POST", ep.LEVERAGE, {"symbol": sym, "leverage": leverage})


async def startup_checks(app: Any, rest: RestClient, creds: Credentials, symbols: list[str]) -> dict[str, Any]:
    cfg, limiter, mode = app.cfg, app.limiter, app.mode
    out: dict[str, Any] = {"symbols": [], "excluded": {}, "canceled": 0}
    offset = await sync_time(rest, app.engine.clock)
    out["clock_offset_ms"] = offset
    if abs(offset) > cfg.ops.recv_window_ms / 2:
        raise StartupError(f"local clock off by {offset} ms (> recvWindow/2); fix NTP first")
    if mode is Mode.LIVE:
        out["api_restrictions"] = await check_api_key_permissions(creds, cfg.ops.recv_window_ms)
    multi = await _signed(rest, limiter, "GET", ep.MULTI_ASSETS)
    if multi.get("multiAssetsMargin"):
        raise StartupError("Multi-Assets mode is on; isolated margin requires single-asset mode")
    dual = await _signed(rest, limiter, "GET", ep.POSITION_MODE)
    if dual.get("dualSidePosition"):
        try:
            await _signed(rest, limiter, "POST", ep.POSITION_MODE, {"dualSidePosition": "false"})
        except ApiError as e:
            if e.code != -4059:
                raise StartupError(f"cannot switch to one-way position mode: {e.code} {e.msg}") from e
    probe = symbols[0] if symbols else "BTCUSDT"
    try:
        comm = await _signed(rest, limiter, "GET", ep.COMMISSION_RATE, {"symbol": probe})
        maker_bps = float(comm.get("makerCommissionRate", 0)) * 1e4
        out["maker_fee_bps"] = maker_bps
        if maker_bps > cfg.account.fee_maker_bps + 0.01:
            raise StartupError(f"actual maker fee {maker_bps:.2f} bps > configured {cfg.account.fee_maker_bps} bps; "
                               "update account.fee_maker_bps (and spreads) first")
    except ApiError as e:
        log.warning("commission_rate_unavailable", error=repr(e))
    for sym in symbols:
        try:
            await configure_symbol(rest, limiter, sym, cfg.account.leverage)
            out["symbols"].append(sym)
        except ApiError as e:
            out["excluded"][sym] = f"{e.code} {e.msg}"
            log.error("symbol_setup_failed", symbol=sym, code=e.code, msg=e.msg)
    # cancel stale rb-* orders from previous sessions (SPEC §11.4)
    for o in await _signed(rest, limiter, "GET", ep.OPEN_ORDERS):
        cid = str(o.get("clientOrderId", ""))
        if is_ours(cid):
            try:
                await _signed(rest, limiter, "DELETE", ep.ORDER, {"symbol": o["symbol"], "origClientOrderId": cid})
                out["canceled"] += 1
            except ApiError as e:
                log.warning("stale_cancel_failed", client_id=cid, code=e.code)
        else:
            log.warning("foreign_open_order", symbol=o.get("symbol"), client_id=cid)
    out["snapshot"] = await fetch_snapshot(app.mode.value, rest, limiter)
    return out


class TradingRulesMonitor:
    """Polls GET /fapi/v1/apiTradingStatus; pauses symbols whose indicators approach a trigger (OQ B18)."""

    def __init__(self, rest: RestClient, limiter: Any, alerts: Any, interval_s: float = 300.0,
                 pause_frac: float = 0.9, warn_frac: float = 0.7, pause_s: float = 1800.0) -> None:
        self.rest, self.limiter, self.alerts = rest, limiter, alerts
        self.interval_s, self.pause_frac, self.warn_frac, self.pause_s = interval_s, pause_frac, warn_frac, pause_s
        self.paused: dict[str, tuple[int, str]] = {}

    def gate(self, symbol: str, now: int) -> str | None:
        p = self.paused.get(symbol)
        if p and now < p[0]:
            return f"trading_rules_{p[1]}"
        return None

    def apply(self, data: dict[str, Any], now: int) -> None:
        for sym, inds in (data.get("indicators") or {}).items():
            for ind in inds:
                name = str(ind.get("indicator", "?"))
                value, trig = float(ind.get("value", 0) or 0), float(ind.get("triggerValue", 0) or 0)
                locked = bool(ind.get("isLocked"))
                ratio = value / trig if trig > 0 else 0.0
                if locked or ratio >= self.pause_frac:
                    self.paused[sym] = (now + int(self.pause_s * 1000), name)
                    if self.alerts:
                        self.alerts.send("CRITICAL", "trading_rules", f"{sym} {name} value={value} trigger={trig} "
                                                                      f"locked={locked}: symbol paused")
                elif ratio >= self.warn_frac and self.alerts:
                    self.alerts.send("WARN", "trading_rules", f"{sym} {name} at {ratio:.0%} of trigger")

    async def run(self) -> None:
        while True:
            try:
                data = await _signed(self.rest, self.limiter, "GET", ep.TRADING_STATUS)
                self.apply(data, _now_ms())
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("trading_status_failed", error=repr(e))
            await asyncio.sleep(self.interval_s)


class RealVenueRuntime:
    def __init__(self, app: Any, venue: Any, rest: RestClient, gateway: OrderGateway, stream: UserDataStream,
                 reconciler: Reconciler, deadman: DeadmanSwitch, monitor: TradingRulesMonitor) -> None:
        self.app, self.venue, self.rest = app, venue, rest
        self.gateway, self.stream, self.reconciler = gateway, stream, reconciler
        self.deadman, self.monitor = deadman, monitor
        self.universe: list[str] = sorted(venue.symbols)  # latest screened candidates (live rotation input)

    def coroutines(self) -> list[Any]:
        out = [self.stream.run(), self.reconciler.run(), self.deadman.run(), self.monitor.run(), self._time_sync()]
        if self.app.mode is Mode.LIVE:
            out.append(self._rotation_loop())  # M7; demo keeps its configured list
        return out

    async def _time_sync(self) -> None:
        while True:
            await asyncio.sleep(600)
            try:
                await sync_time(self.rest, self.app.engine.clock)
            except Exception as e:
                log.warning("time_sync_failed", error=repr(e))

    def on_candidates(self, cands: list[str]) -> None:
        """Screened candidates; live rotation picks ACTIVE symbols from them (demo ignores them)."""
        self.universe = list(cands)

    def _decide(self, now: int) -> tuple[list[str], list[Decision]]:
        cfg, repo, v = self.app.cfg, self.app.repo, self.venue
        evidence = read_shadow_db(cfg.universe, repo.path)
        rows = repo.query("SELECT symbol, to_state, ts FROM universe_decisions ORDER BY id")
        edges = live_edges(repo, v.name, cfg.universe.promotion_horizon_s)
        return rotate(sorted(v.symbols), evidence, edges, cfg.universe, self.universe, last_states(rows), now,
                      last_demotions(rows))

    async def rotate_once(self, now: int) -> None:
        """M7: demote / promote (universe/rotation.py); new ACTIVE symbols are configured on the exchange first."""
        app, v = self.app, self.venue
        active, decisions = await asyncio.to_thread(self._decide, now)
        for sym in [s for s in active if s not in v.symbols]:
            try:
                await configure_symbol(self.rest, app.limiter, sym, app.cfg.account.leverage)
            except ApiError as e:
                active.remove(sym)
                decisions.append(Decision(now, sym, "ACTIVE", "CANDIDATE", f"exchange setup failed: {e.code} {e.msg}"
                                          [:200], 0, float("nan"), float("nan")))
        record(app.repo, decisions)
        if decisions and app.alerts is not None:
            app.alerts.send("INFO", "universe", "; ".join(f"{d.symbol} {d.from_state}->{d.to_state}"
                                                          for d in decisions))
        if set(active) != v.symbols:
            app.engine.set_symbols(v.name, active)
            app.engine.set_symbols(SHADOW_TWIN, active)
            pipeline = getattr(app, "pipeline", None)
            if pipeline is not None:
                pipeline.set_symbols(app.engine.all_symbols())

    async def _rotation_loop(self) -> None:
        while True:
            await asyncio.sleep(self.app.cfg.universe.screen_interval_s)
            try:
                await self.rotate_once(_now_ms())
            except asyncio.CancelledError:
                raise
            except Exception as e:  # keep the current set; the risk engine still bounds every symbol
                log.error("rotation_failed", error=repr(e))
                if self.app.alerts is not None:
                    self.app.alerts.send("WARN", "rotation_failed", repr(e)[:300])

    async def shutdown(self) -> None:
        """SPEC §11.4: cancel all rb-* orders on shutdown; then stop dead-man timers and the stream."""

        v = self.venue
        v.paused_until = 2**62
        n = v.orders.cancel_all(_now_ms(), None, "shutdown")
        log.info("shutdown_cancel_all", venue=v.name, cancels=n)
        await self.gateway.drain(5.0)
        await asyncio.sleep(0.5)
        try:  # belt and braces: anything still open on the exchange with our prefix
            for o in await _signed(self.rest, self.app.limiter, "GET", ep.OPEN_ORDERS):
                cid = str(o.get("clientOrderId", ""))
                if is_ours(cid):
                    await _signed(self.rest, self.app.limiter, "DELETE", ep.ORDER,
                                  {"symbol": o["symbol"], "origClientOrderId": cid})
        except Exception as e:
            log.error("shutdown_cancel_check_failed", error=repr(e))
        await self.deadman.disarm()
        await self.stream.close()
        await self.gateway.stop()
        await self.rest.close()


async def setup_real_venue(app: Any, universe: list[str]) -> RealVenueRuntime:
    cfg, mode = app.cfg, app.mode
    creds = credentials_from_env()
    if creds is None:
        raise StartupError(f"{mode.value} mode needs BINANCE_API_KEY and BINANCE_API_SECRET "
                           "(or BINANCE_PRIVATE_KEY_PATH) in the environment")
    if mode is Mode.LIVE:
        unlock_live_signing()
    clock = app.engine.clock
    rest = RestClient(app.env.rest, creds, recv_window_ms=cfg.ops.recv_window_ms, server_time_ms=clock.server_ms,
                      header_hook=app.limiter.on_headers)
    try:
        symbols = await select_real_symbols(app, universe)
        checks = await startup_checks(app, rest, creds, symbols)
    except BaseException:
        await rest.close()
        raise
    snap = checks["snapshot"]
    venue_name = mode.value
    wallet = snap.wallet_balance or 0.0
    if wallet <= 0:
        raise StartupError("wallet balance is zero or unavailable")
    repo = app.repo

    def on_rate_limit(reason: str) -> None:
        v.risk.halt(reason, _now_ms())

    gateway = OrderGateway(venue_name, rest, app.limiter, app.queue.put_nowait, on_rate_limit=on_rate_limit)
    v = app.engine.add_real_venue(venue_name, mode.value, gateway, wallet,
                                  persist_halt=lambda reason: persist_halt(repo, reason, mode.value),
                                  kv_get=repo.kv_get, kv_set=repo.kv_set)
    v.positions.wallet_balance = wallet
    for sym, (qty, entry, liq) in snap.positions.items():
        if qty != Decimal(0):
            v.positions.adopt(sym, qty, entry, _now_ms())
            v.liq[sym] = float(liq)
            log.warning("startup_position", symbol=sym, qty=str(qty), entry=str(entry))
    app.engine.set_symbols(venue_name, checks["symbols"])
    # Shadow twin (SPEC §5.9 gap report): paper fills on the same symbols and market data, same DB.
    # Its HALT is not persisted: a simulated venue must never block a real restart.
    app.engine.add_paper_venue(SHADOW_TWIN, "shadow")
    app.engine.set_symbols(SHADOW_TWIN, checks["symbols"])
    stream = UserDataStream(venue_name, app.env, rest, app.queue.put_nowait, limiter=app.limiter)
    reconciler = Reconciler(venue_name, rest, app.limiter, app.queue.put_nowait, cfg.ops.reconcile_s)
    v.recon_request = reconciler.request_now
    v.user_stream_ok = False  # until the private stream is connected (then paused until a clean recon)

    def deadman_symbols() -> list[str]:
        return sorted(v.symbols | v.winding | {o.symbol for o in v.orders.live_orders()})

    deadman = DeadmanSwitch(rest, app.limiter, deadman_symbols, heartbeat_s=cfg.ops.deadman_heartbeat_s,
                            countdown_s=cfg.ops.deadman_countdown_s,
                            on_failure=lambda s, e: app.alerts.send("WARN", "deadman", f"{s}: {e}"))
    monitor = TradingRulesMonitor(rest, app.limiter, app.alerts)
    v.gates.append(lambda s, now: None if deadman.healthy(s, now) else "deadman_unhealthy")
    v.gates.append(monitor.gate)
    gateway.start()
    log.warning("real_venue_ready", venue=venue_name, symbols=checks["symbols"], excluded=checks["excluded"],
                canceled_stale=checks["canceled"], wallet=wallet, clock_offset_ms=checks["clock_offset_ms"],
                maker_fee_bps=checks.get("maker_fee_bps"), risk=cfg.risk.model_dump())
    if v.risk.state is not RiskState.OK:
        raise StartupError(f"risk engine not OK at startup: {v.risk.state}")
    return RealVenueRuntime(app, v, rest, gateway, stream, reconciler, deadman, monitor)


async def select_real_symbols(app: Any, universe: list[str]) -> list[str]:
    """Demo: the configured/initial universe. Live: ACTIVE symbols promoted from shadow (universe/rotation.py)."""
    if app.mode is Mode.LIVE:
        if not LIVE_ACCEPTANCE_APPROVED:
            raise StartupError("live is blocked until the M1/M3/M4 acceptance runs pass and the operator approves "
                               "(OPEN_QUESTIONS B1)")
        return await asyncio.to_thread(promoted_symbols, app.cfg, universe, app.repo, _now_ms())
    return list(universe)
