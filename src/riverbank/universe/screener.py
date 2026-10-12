"""Universe Screener (SPEC §5.2).

Every `screen_interval_s`: exchangeInfo + ticker/24hr (all, weight 40) + bookTicker (all,
weight 5) + recorded history -> ranked CANDIDATE list with all metrics and, for every
excluded symbol, the reasons.

Ranking score (heuristic, only orders candidates; promotion is decided by measured
markout, never by this score):  score = edge_ratio * fill_opportunity / toxicity_proxy
"""

from __future__ import annotations

import asyncio
import json
import statistics
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from riverbank.core.config import Config
from riverbank.core.models import SymbolSpec
from riverbank.exchange import endpoints as ep
from riverbank.exchange.specs import ExchangeInfo, fetch_exchange_info
from riverbank.ops.logging import get_logger
from riverbank.quoting.sizing import client_id_fits, order_size
from riverbank.universe.history import HistoryMetrics

log = get_logger("screener")

DAY_MS = 86_400_000
SPREAD_SAMPLES = 12  # snapshot spreads kept per symbol: 1 h at the default 300 s screen interval
HYSTERESIS = 0.85  # a current candidate stays down to 0.85 x the spread entry bar (and toxicity_max / 0.85)


def load_blacklist(path: str | Path) -> set[str]:
    p = Path(path)
    if not p.exists():
        return set()
    data = yaml.safe_load(p.read_text()) or {}
    return {str(s).upper() for s in (data.get("symbols") or [])}


@dataclass(slots=True)
class ScreenRow:
    symbol: str
    eligible: bool = False
    reasons: list[str] = field(default_factory=list)
    spread_bps: float = float("nan")
    tick_bps: float = float("nan")
    quote_vol_musd: float = float("nan")
    range_bps: float = float("nan")
    toxicity_proxy: float = float("nan")
    fill_opportunity: float = float("nan")
    edge_ratio: float = float("nan")
    mid: float = float("nan")
    order_qty: str = ""
    order_notional: float = float("nan")
    inventory_units: int = 0
    score: float = 0.0
    source: str = "snapshot"
    history_days: int = 0
    rank: int = 0
    candidate: bool = False


def hard_exclusions(spec: SymbolSpec, cfg: Config, blacklist: set[str], now_ms: int) -> list[str]:
    u = cfg.universe
    r: list[str] = []
    if spec.symbol in u.hedge_only:
        r.append("hedge_only")
    if spec.status != "TRADING":
        r.append(f"status={spec.status}")
    if spec.contract_type != "PERPETUAL":
        r.append(f"contract={spec.contract_type}")
    if spec.quote_asset != "USDT":
        r.append(f"quote={spec.quote_asset}")
    if spec.underlying_type not in u.allowed_underlying_types:
        r.append(f"underlying={spec.underlying_type}")
    if not spec.symbol.isascii():
        r.append("non_ascii_symbol")
    if spec.position_control_side not in ("", "NONE"):
        r.append(f"position_control={spec.position_control_side}")
    if spec.onboard_date_ms and now_ms - spec.onboard_date_ms < u.min_listing_days * DAY_MS:
        r.append(f"listed<{u.min_listing_days}d")
    if spec.symbol in blacklist:
        r.append("blacklist")
    if not client_id_fits(spec.symbol):
        r.append("client_id_too_long")
    if spec.time_in_force and "GTX" not in spec.time_in_force:
        r.append("no_post_only")
    return r


def _f(x: Any) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


def evaluate(
    specs: Mapping[str, SymbolSpec],
    tickers: Mapping[str, Mapping[str, Any]],
    book_tickers: Mapping[str, Mapping[str, Any]],
    cfg: Config,
    blacklist: set[str],
    now_ms: int,
    history: Callable[[str], tuple[dict[str, float], int]] | None = None,
    incumbents: Iterable[str] = (),
) -> list[ScreenRow]:
    u, q = cfg.universe, cfg.quoting
    rt_fee = 2 * cfg.account.fee_maker_bps
    rows: list[ScreenRow] = []
    incumbent = set(incumbents)
    for sym, spec in specs.items():
        row = ScreenRow(sym)
        row.reasons = hard_exclusions(spec, cfg, blacklist, now_ms)
        rows.append(row)
        if row.reasons:
            continue
        t, b = tickers.get(sym), book_tickers.get(sym)
        if t is None or b is None:
            row.reasons.append("no_market_data")
            continue
        bid, ask = _f(b.get("bidPrice")), _f(b.get("askPrice"))
        if not (bid > 0 and ask > bid):
            row.reasons.append("bad_book")
            continue
        mid = (bid + ask) / 2
        snap = {
            "spread_bps": (ask - bid) / mid * 1e4,
            "quote_vol_musd": _f(t.get("quoteVolume")) / 1e6,
            "range_bps": (_f(t.get("highPrice")) - _f(t.get("lowPrice"))) / max(_f(t.get("lastPrice")), 1e-12) * 1e4,
            "fill_opportunity": _f(t.get("count")) / 1440.0,
        }
        hist, ndays = history(sym) if history else ({}, 0)
        used_hist = [k for k in snap if k in hist]
        m = {k: hist.get(k, v) for k, v in snap.items()}
        row.source = "history" if len(used_hist) == len(snap) else ("mixed" if used_hist else "snapshot")
        row.history_days = ndays
        row.mid = mid
        row.spread_bps = m["spread_bps"]
        row.quote_vol_musd = m["quote_vol_musd"]
        row.range_bps = m["range_bps"]
        row.fill_opportunity = m["fill_opportunity"]
        row.tick_bps = float(spec.tick) / mid * 1e4
        row.toxicity_proxy = row.range_bps / row.spread_bps if row.spread_bps > 0 else float("inf")
        row.edge_ratio = row.spread_bps / rt_fee if rt_fee > 0 else float("inf")
        size = order_size(spec, mid, q.order_size_usdt, q.size_buffer, cfg.risk.max_position_usdt,
                          q.max_inventory_units)
        row.order_qty = str(size.qty)
        row.order_notional = size.notional
        row.inventory_units = size.inventory_units
        k = HYSTERESIS if sym in incumbent else 1.0  # last screen's candidates keep their place near the line
        if not row.spread_bps >= u.edge_ratio_min * rt_fee * k:
            row.reasons.append(f"spread<{u.edge_ratio_min}x_fee")
        if not (u.vol_min_musd <= row.quote_vol_musd <= u.vol_max_musd):
            row.reasons.append("volume_out_of_range")
        if not row.toxicity_proxy <= u.toxicity_max / k:
            row.reasons.append("toxic")
        if not size.ok:
            row.reasons.append(f"size:{size.reason}")
        row.eligible = not row.reasons
        if row.eligible:
            row.score = row.edge_ratio * row.fill_opportunity / max(row.toxicity_proxy, 1.0)
    eligible = sorted((r for r in rows if r.eligible), key=lambda r: (-r.score, r.symbol))
    for i, r in enumerate(eligible, 1):
        r.rank = i
        r.candidate = i <= u.max_candidate_symbols
    others = sorted((r for r in rows if not r.eligible), key=lambda r: r.symbol)
    return eligible + others


def candidates(rows: Iterable[ScreenRow]) -> list[str]:
    return [r.symbol for r in rows if r.candidate]


class Screener:
    def __init__(self, cfg: Config, rest: Any, *, data_dir: str | Path | None = None,
                 state_dir: str | Path | None = None, limiter: Any = None) -> None:
        self.cfg = cfg
        self.rest = rest
        self.limiter = limiter
        state = Path(state_dir or cfg.ops.state_dir)
        self.history = HistoryMetrics(data_dir or cfg.ops.data_dir, state / "screener_cache.json")
        self.latest_path = state / "screen_latest.json"
        self.info: ExchangeInfo | None = None
        self.last_rows: list[ScreenRow] = []
        self._spreads: dict[str, deque[float]] = {}  # recent snapshot spreads (bps), used until history exists

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if self.limiter is not None:
            await self.limiter.acquire(ep.weight("GET", path, params))
        return await self.rest.public("GET", path, params)

    async def screen_once(self, now_ms: int | None = None) -> list[ScreenRow]:
        now = now_ms or time.time_ns() // 1_000_000
        if self.limiter is not None:
            await self.limiter.acquire(ep.weight("GET", ep.EXCHANGE_INFO))
        self.info = await fetch_exchange_info(self.rest)
        tickers = {t["symbol"]: t for t in await self._get(ep.TICKER_24HR)}
        books = {b["symbol"]: b for b in await self._get(ep.BOOK_TICKER)}
        blacklist = load_blacklist(self.cfg.universe.blacklist_path)
        lookback = self.cfg.universe.screen_lookback_days
        for sym, b in books.items():
            bid, ask = _f(b.get("bidPrice")), _f(b.get("askPrice"))
            if bid > 0 and ask > bid:
                spread_bps = (ask - bid) / ((bid + ask) / 2) * 1e4
                self._spreads.setdefault(sym, deque(maxlen=SPREAD_SAMPLES)).append(spread_bps)

        def hist(sym: str) -> tuple[dict[str, float], int]:
            m, ndays = self.history.metrics(sym, now, lookback)
            if "spread_bps" not in m and self._spreads.get(sym):
                # A thin book flips between 1 and 2 ticks; one snapshot made candidates churn every screen.
                m = {**m, "spread_bps": statistics.median(self._spreads[sym])}
            return m, ndays

        rows = await asyncio.to_thread(evaluate, self.info.specs, tickers, books, self.cfg, blacklist, now, hist,
                                       candidates(self.last_rows))
        self.last_rows = rows
        self._persist(rows, now)
        log.info("screen_done", eligible=sum(r.eligible for r in rows), candidates=candidates(rows))
        return rows

    def _persist(self, rows: list[ScreenRow], now: int) -> None:
        try:
            self.latest_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"ts": now, "rows": [asdict(r) for r in rows if r.eligible or r.candidate]}
            self.latest_path.write_text(json.dumps(payload, default=str))
        except OSError as e:
            log.warning("screen_persist_failed", error=repr(e))

    async def run(self, on_result: Callable[[list[ScreenRow]], Awaitable[None] | None]) -> None:
        while True:
            try:
                rows = await self.screen_once()
                res = on_result(rows)
                if asyncio.iscoroutine(res):
                    await res
            except asyncio.CancelledError:
                raise
            except Exception as e:  # screening failure must not stop the bot; keep last universe
                log.error("screen_failed", error=repr(e))
            await asyncio.sleep(self.cfg.universe.screen_interval_s)
