"""Replay harness (SPEC §10): quoting + paper simulator over recorded Parquet, deterministic.

Events are merged by local receive time (what the bot saw live), ties broken by a fixed
stream priority and file order; timer ticks are synthesized every `timer_ms` of event
time. Same input → same orders → same fills (verified by a digest of the fill list).
"""

from __future__ import annotations

import hashlib
import heapq
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from riverbank.core.clock import SimClock
from riverbank.core.config import Config
from riverbank.core.events import AggTrade, BookTicker, BookTopN, MarkPrice, Timer
from riverbank.core.models import Fill, Order, SymbolSpec
from riverbank.engine import Engine, Venue
from riverbank.marketdata.market_state import MarketState
from riverbank.marketdata.recorder import read_stream
from riverbank.orders.client_ids import ClientIdAllocator

PRIORITY = {"book": 0, "bookTicker": 1, "markPrice": 2, "aggTrade": 3}


def _iter_stream(root: str | Path, stream: str, symbol: str, start: int | None, end: int | None) -> Iterator[Any]:
    t = read_stream(root, stream, symbol, start, end)
    cols = {c: t.column(c).to_pylist() for c in t.column_names}
    n = t.num_rows
    if stream == "book":
        for i in range(n):
            yield BookTopN(symbol, cols["last_update_id"][i], list(zip(cols["bids_px"][i], cols["bids_qty"][i],
                                                                       strict=True)),
                           list(zip(cols["asks_px"][i], cols["asks_qty"][i], strict=True)), cols["recv_ts"][i])
    elif stream == "bookTicker":
        for i in range(n):
            yield BookTicker(symbol, cols["u"][i], cols["E"][i], cols["T"][i], cols["bid_px"][i], cols["bid_qty"][i],
                             cols["ask_px"][i], cols["ask_qty"][i], cols["recv_ts"][i])
    elif stream == "aggTrade":
        for i in range(n):
            yield AggTrade(symbol, cols["a"][i], cols["price"][i], cols["qty"][i], cols["nq"][i], cols["T"][i],
                           cols["E"][i], cols["buyer_maker"][i], cols["recv_ts"][i])
    elif stream == "markPrice":
        for i in range(n):
            yield MarkPrice(symbol, cols["E"][i], cols["mark"][i], cols["index"][i], cols["funding_rate"][i],
                            cols["next_funding_ms"][i], cols["recv_ts"][i])


def load_events(root: str | Path, symbols: Sequence[str], start: int | None = None,
                end: int | None = None) -> list[Any]:
    """All recorded events for `symbols`, merged deterministically by (recv_ts, priority, symbol, seq)."""
    keyed: list[tuple[int, int, str, int, Any]] = []
    for sym in sorted(symbols):
        for stream, prio in PRIORITY.items():
            for seq, ev in enumerate(_iter_stream(root, stream, sym, start, end)):
                keyed.append((ev.recv_ts, prio, sym, seq, ev))
    keyed.sort(key=lambda k: k[:4])
    return [k[4] for k in keyed]


def merge_sorted(*streams: Sequence[Any]) -> list[Any]:
    return [e for _, _, e in heapq.merge(*[[(e.recv_ts, i, e) for e in s] for i, s in enumerate(streams)])]


@dataclass
class ReplayResult:
    engine: Engine
    fills: list[tuple[str, Fill]] = field(default_factory=list)
    events: int = 0

    def digest(self) -> str:
        h = hashlib.sha256()
        for venue, f in self.fills:
            h.update(f"{venue}|{f.ts}|{f.symbol}|{f.side.value}|{f.price}|{f.qty}|{f.fee}|{f.client_id}\n".encode())
        return h.hexdigest()

    @property
    def venue(self) -> Venue:
        return self.engine.venues["paper"]


def run_replay(cfg: Config, specs: dict[str, SymbolSpec], events: Sequence[Any], symbols: Sequence[str], *,
               hedge_symbols: Sequence[str] = (), repo: Any = None, timer_ms: int = 100,
               equity: float | None = None, setup: Any = None) -> ReplayResult:
    if not events:
        raise ValueError("no events to replay")
    all_syms = sorted(set(symbols) | set(hedge_symbols))
    market = MarketState(all_syms, stale_ms=cfg.ops.stale_ms, request_snapshot=lambda *a: None, refresh_interval_ms=0)
    clock = SimClock(events[0].recv_ts)
    engine = Engine(cfg, specs, market, clock, ClientIdAllocator(), repo=repo, hedge_symbols=hedge_symbols)
    engine.add_paper_venue("paper", "shadow", equity)
    engine.set_symbols("paper", symbols)
    result = ReplayResult(engine)

    def on_fill(v: Venue, f: Fill, o: Order) -> None:
        result.fills.append((v.name, f))

    engine.fill_hooks.append(on_fill)
    if setup is not None:
        setup(engine)
    next_tick = (events[0].recv_ts // timer_ms + 1) * timer_ms
    for ev in events:
        while next_tick <= ev.recv_ts:
            engine.on_event(Timer(next_tick))
            next_tick += timer_ms
        engine.on_event(ev)
        result.events += 1
    # drain: let latency, markouts (≤ 300 s) and quotes settle after the last event
    end = events[-1].recv_ts + 301_000
    while next_tick <= end:
        engine.on_event(Timer(next_tick))
        next_tick += timer_ms
    engine.final_snapshot(next_tick)
    return result
