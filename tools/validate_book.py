"""M1 acceptance run: market data gateway + local books + recorder on N symbols.

Public production market data only (no keys). Measures:
  * detected gaps / resyncs per symbol
  * L1–L5 exact match between the long-running book and a fresh REST-snapshot book at the
    same update id (OPEN_QUESTIONS B15) — a mismatch = an *undetected* gap/bug
  * stale periods, reconnects, parse errors
  * readability of every Parquet file written
Writes an interim report every 10 minutes and a final report:
  {out}/report.md, {out}/summary.json

    uv run python tools/validate_book.py --symbols 20 --hours 24 --out reports/m1-validation
    uv run python tools/validate_book.py --symbols DOGEUSDT XRPUSDT --minutes 3   # smoke test
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import json
import time
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from riverbank.core.config import OpsCfg
from riverbank.core.events import Timer
from riverbank.core.models import Mode
from riverbank.exchange import endpoints as ep
from riverbank.exchange.environments import ENVIRONMENTS
from riverbank.exchange.rate_limiter import RateLimiter
from riverbank.exchange.rest import RestClient
from riverbank.exchange.specs import fetch_exchange_info
from riverbank.marketdata.pipeline import MarketDataPipeline
from riverbank.marketdata.recorder import Recorder, list_files
from riverbank.ops.logging import get_logger, setup_logging

log = get_logger("validate_book")


def now_ms() -> int:
    return time.time_ns() // 1_000_000


async def pick_symbols(rest: RestClient, n: int, exclude: set[str]) -> list[str]:
    info = await fetch_exchange_info(rest)
    tickers = await rest.public("GET", ep.TICKER_24HR)
    ok = {s for s, sp in info.specs.items()
          if sp.status == "TRADING" and sp.contract_type == "PERPETUAL" and sp.quote_asset == "USDT"
          and sp.underlying_type == "COIN" and s.isascii() and s not in exclude}
    ranked = sorted((t for t in tickers if t["symbol"] in ok), key=lambda t: -float(t["quoteVolume"]))
    return [t["symbol"] for t in ranked[:n]]


def build_report(pipe: MarketDataPipeline, started: int, ended: int, recorder: Recorder, data_dir: Path,
                 check_parquet: bool) -> tuple[str, dict[str, Any]]:
    rows = []
    tot_checks = tot_match = tot_mismatch = tot_gaps = 0
    stale_events: dict[str, int] = {}
    for _, sym, _old, new in pipe.status_changes:
        if str(new) == "STALE":
            stale_events[sym] = stale_events.get(sym, 0) + 1
    for sym, m in sorted(pipe.state.symbols.items()):
        s = m.sync
        tot_checks += m.checks
        tot_match += m.check_matches
        tot_mismatch += m.check_mismatches
        tot_gaps += s.gaps
        pct = 100.0 * m.check_matches / m.checks if m.checks else float("nan")
        rows.append({"symbol": sym, "checks": m.checks, "matches": m.check_matches, "match_pct": pct,
                     "undetected_mismatches": m.check_mismatches, "detected_gaps": s.gaps,
                     "stale_snapshots": s.stale_snapshots, "resyncs": max(0, s.syncs - 1),
                     "stale_events": stale_events.get(sym, 0), "synced_now": s.synced,
                     "mismatch_samples": m.mismatch_details[:3]})
    pq_files = pq_bad = 0
    bad_files: list[str] = []
    if check_parquet:
        for stream in ("bookTicker", "aggTrade", "markPrice", "book", "depth"):
            for f in list_files(data_dir, stream):
                pq_files += 1
                try:
                    pq.read_table(f)
                except Exception as e:
                    pq_bad += 1
                    bad_files.append(f"{f}: {e!r}")
    conns = pipe.gateway._conns.values()
    reconnects = sum(ws.disconnects for ws, _ in conns)
    rolls = sum(ws.rolls for ws, _ in conns)
    overall = 100.0 * tot_match / tot_checks if tot_checks else float("nan")
    hours = (ended - started) / 3600_000
    verdict = {
        "l1_l5_match_ge_99_9": tot_checks > 0 and overall >= 99.9,
        "no_undetected_gaps": tot_checks > 0 and tot_mismatch == 0,
        "parquet_readable": pq_files > 0 and pq_bad == 0 if check_parquet else None,
        "duration_ok_24h": hours >= 24.0,
    }
    summary = {"started": started, "ended": ended, "hours": hours, "symbols": len(rows), "checks": tot_checks,
               "matches": tot_match, "match_pct": overall, "undetected_mismatches": tot_mismatch,
               "detected_gaps": tot_gaps, "reconnects": reconnects, "rolls": rolls,
               "parse_errors": pipe.gateway.parse_errors, "snapshots": pipe.gateway.snapshots,
               "snapshot_errors": pipe.gateway.snapshot_errors, "parquet_files": pq_files,
               "parquet_unreadable": pq_bad, "rows_written": recorder.rows_written,
               "recorder_write_errors": recorder.write_errors, "verdict": verdict, "per_symbol": rows,
               "bad_files": bad_files[:20]}
    t0 = dt.datetime.fromtimestamp(started / 1000, dt.UTC).isoformat(timespec="seconds")
    t1 = dt.datetime.fromtimestamp(ended / 1000, dt.UTC).isoformat(timespec="seconds")
    lines = [
        "# M1 validation report — market data, local books, recorder", "",
        f"- Window: {t0} → {t1} ({hours:.2f} h), {len(rows)} symbols",
        f"- L1–L5 exact match vs fresh REST-snapshot book: **{overall:.3f} %** ({tot_match}/{tot_checks})",
        f"- Undetected mismatches: **{tot_mismatch}**; detected stream gaps (resynced): {tot_gaps}; "
        f"REST snapshots older than the stream (refetched): {sum(r['stale_snapshots'] for r in rows)}",
        f"- WS reconnects: {reconnects}, planned rolls: {rolls}, parse errors: {pipe.gateway.parse_errors}",
        f"- REST snapshots: {pipe.gateway.snapshots} (errors {pipe.gateway.snapshot_errors})",
        f"- Parquet files: {pq_files}, unreadable: {pq_bad}; rows written: {recorder.rows_written}",
        "", "## Acceptance (SPEC §12 M1)", "",
        f"- 24 h duration: {'PASS' if verdict['duration_ok_24h'] else 'NOT MET (run was shorter)'}",
        f"- No undetected gaps: {'PASS' if verdict['no_undetected_gaps'] else 'FAIL'}",
        f"- L1–L5 match ≥ 99.9 %: {'PASS' if verdict['l1_l5_match_ge_99_9'] else 'FAIL'}",
        f"- Parquet readable: {'PASS' if verdict['parquet_readable'] else 'FAIL'}", "",
        "## Per symbol", "",
        "| symbol | checks | match % | undetected | detected gaps | resyncs | stale events | synced |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for r in rows:
        lines.append(f"| {r['symbol']} | {r['checks']} | {r['match_pct']:.2f} | {r['undetected_mismatches']} | "
                     f"{r['detected_gaps']} | {r['resyncs']} | {r['stale_events']} | {r['synced_now']} |")
    if any(r["mismatch_samples"] for r in rows):
        lines += ["", "## Mismatch samples", ""]
        for r in rows:
            for d in r["mismatch_samples"]:
                lines.append(f"- {r['symbol']}: `{d[:300]}`")
    return "\n".join(lines) + "\n", summary


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=["20"], help="N (top-N by volume) or explicit symbols")
    ap.add_argument("--hours", type=float, default=0.0)
    ap.add_argument("--minutes", type=float, default=0.0)
    ap.add_argument("--check-interval-s", type=float, default=60.0)
    ap.add_argument("--out", default="reports/m1-validation")
    ap.add_argument("--data-dir", default="data/m1-validation")
    ap.add_argument("--record-depth", action="store_true", help="also record raw depth diffs")
    args = ap.parse_args()
    duration_s = args.hours * 3600 + args.minutes * 60 or 24 * 3600
    setup_logging("INFO", {"ws": "WARNING", "marketdata": "WARNING"})
    env = ENVIRONMENTS[Mode.SHADOW]
    rest = RestClient(env.rest)
    info = await fetch_exchange_info(rest)
    limiter = RateLimiter(info.rate_limits, weight_frac=0.5)
    rest.header_hook = limiter.on_headers
    if len(args.symbols) == 1 and args.symbols[0].isdigit():
        symbols = await pick_symbols(rest, int(args.symbols[0]), {"BTCUSDT", "ETHUSDT"})
    else:
        symbols = [s.upper() for s in args.symbols]
    ops = OpsCfg(record_depth_diffs=args.record_depth)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    data_dir = Path(args.data_dir)
    recorder = Recorder(data_dir, flush_s=ops.recorder_flush_s, record_depth=args.record_depth)
    queue: asyncio.Queue[Any] = asyncio.Queue()
    pipe = MarketDataPipeline(ops, env, rest, queue.put_nowait, symbols, limiter=limiter, recorder=recorder)
    started = now_ms()
    print(f"validating {len(symbols)} symbols for {duration_s / 3600:.2f} h: {', '.join(symbols)}")

    async def timer() -> None:
        while True:
            await asyncio.sleep(0.1)
            queue.put_nowait(Timer(now_ms()))

    async def scheduler() -> None:
        i = 0
        while True:
            await asyncio.sleep(args.check_interval_s / max(1, len(symbols)))
            sym = symbols[i % len(symbols)]
            i += 1
            queue.put_nowait(("check", sym))

    async def interim() -> None:
        while True:
            await asyncio.sleep(600)
            md, summary = build_report(pipe, started, now_ms(), recorder, data_dir, check_parquet=False)
            (out / "report.interim.md").write_text(md)
            (out / "summary.interim.json").write_text(json.dumps(summary, indent=1, default=str))

    await pipe.start()
    tasks = [asyncio.create_task(c()) for c in (timer, scheduler, interim)]
    tasks.append(asyncio.create_task(recorder.run(now_ms)))
    deadline = time.monotonic() + duration_s
    try:
        while time.monotonic() < deadline:
            try:
                ev = await asyncio.wait_for(queue.get(), timeout=1.0)
            except TimeoutError:
                continue
            if isinstance(ev, Timer):
                pipe.on_timer(ev.recv_ts)
            elif isinstance(ev, tuple) and ev[0] == "check":
                pipe.state.start_pending(ev[1], "check", now_ms())
            else:
                pipe.handle(ev)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await pipe.stop()
        await recorder.close(now_ms())
        await rest.close()
        # Final report is written even if the run is interrupted (Ctrl-C / SIGTERM).
        md, summary = build_report(pipe, started, now_ms(), recorder, data_dir, check_parquet=True)
        (out / "report.md").write_text(md)
        (out / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
        print(md)


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
