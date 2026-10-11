"""Recorder (SPEC §5.10): raw normalized market events -> Parquet, partitioned by date/symbol.

Layout:
  {root}/raw/date=YYYY-MM-DD/symbol=SYM/{stream}/HH.parquet          (compacted hour)
  {root}/raw/date=YYYY-MM-DD/symbol=SYM/{stream}/HH.part-NNNNNN.parquet (flushed parts)

Every flush writes *complete* Parquet files (always readable, a crash loses at most one
flush interval). When an hour is over its parts are compacted into `HH.parquet`
(write to a temp file, atomic rename, then delete parts). Readers prefer `HH.parquet`
and ignore parts of an hour that has a compacted file.

Streams: bookTicker, aggTrade, markPrice, book (top-N every 100 ms), depth (raw diffs,
optional; used to build book-sync replay fixtures).
All disk I/O runs in a worker thread (`asyncio.to_thread`) — never on the event loop.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import re
import shutil
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from riverbank.core.events import AggTrade, BookTicker, BookTopN, DepthDiff, MarkPrice
from riverbank.ops.logging import get_logger

log = get_logger("recorder")

F64, I64 = pa.float64(), pa.int64()
LF64 = pa.list_(pa.float64())

SCHEMAS: dict[str, pa.Schema] = {
    "bookTicker": pa.schema([("recv_ts", I64), ("E", I64), ("T", I64), ("u", I64), ("bid_px", F64),
                             ("bid_qty", F64), ("ask_px", F64), ("ask_qty", F64)]),
    "aggTrade": pa.schema([("recv_ts", I64), ("E", I64), ("T", I64), ("a", I64), ("price", F64), ("qty", F64),
                           ("nq", F64), ("buyer_maker", pa.bool_())]),
    "markPrice": pa.schema([("recv_ts", I64), ("E", I64), ("mark", F64), ("index", F64), ("funding_rate", F64),
                            ("next_funding_ms", I64)]),
    "book": pa.schema([("recv_ts", I64), ("last_update_id", I64), ("bids_px", LF64), ("bids_qty", LF64),
                       ("asks_px", LF64), ("asks_qty", LF64)]),
    "depth": pa.schema([("recv_ts", I64), ("E", I64), ("T", I64), ("U", I64), ("u", I64), ("pu", I64),
                        ("bids_px", LF64), ("bids_qty", LF64), ("asks_px", LF64), ("asks_qty", LF64)]),
}

_PART_RE = re.compile(r"^(\d\d)\.part-(\d+)\.parquet$")
_HOUR_RE = re.compile(r"^(\d\d)\.parquet$")


def _utc(ts_ms: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(ts_ms / 1000, tz=dt.UTC)


def _row(ev: Any) -> tuple[str, tuple[Any, ...]] | None:
    if isinstance(ev, BookTicker):
        return "bookTicker", (ev.recv_ts, ev.E, ev.T, ev.u, ev.bid_px, ev.bid_qty, ev.ask_px, ev.ask_qty)
    if isinstance(ev, AggTrade):
        return "aggTrade", (ev.recv_ts, ev.E, ev.T, ev.a, ev.price, ev.qty, ev.nq, ev.buyer_maker)
    if isinstance(ev, MarkPrice):
        return "markPrice", (ev.recv_ts, ev.E, ev.mark, ev.index, ev.funding_rate, ev.next_funding_ms)
    if isinstance(ev, BookTopN):
        return "book", (ev.recv_ts, ev.last_update_id, [p for p, _ in ev.bids], [q for _, q in ev.bids],
                        [p for p, _ in ev.asks], [q for _, q in ev.asks])
    if isinstance(ev, DepthDiff):
        return "depth", (ev.recv_ts, ev.E, ev.T, ev.U, ev.u, ev.pu, [p for p, _ in ev.bids],
                         [q for _, q in ev.bids], [p for p, _ in ev.asks], [q for _, q in ev.asks])
    return None


BufKey = tuple[str, str, str, str]  # (date, hour, symbol, stream)


class Recorder:
    def __init__(self, root: str | Path, *, flush_s: float = 60.0, record_depth: bool = False,
                 retention_days: int = 30) -> None:
        self.raw = Path(root) / "raw"
        self.flush_s = flush_s
        self.record_depth = record_depth
        self.retention_days = retention_days
        self._buf: dict[BufKey, list[tuple[Any, ...]]] = defaultdict(list)
        self._part_seq = 0
        self.rows_written = 0
        self.files_written = 0
        self.write_errors = 0
        self._lock = asyncio.Lock()

    def on_event(self, ev: Any) -> None:
        if isinstance(ev, DepthDiff) and not self.record_depth:
            return
        r = _row(ev)
        if r is None:
            return
        stream, row = r
        t = _utc(row[0])
        self._buf[(t.strftime("%Y-%m-%d"), t.strftime("%H"), ev.symbol, stream)].append(row)

    def pending_rows(self) -> int:
        return sum(len(v) for v in self._buf.values())

    # ---- writing ----

    async def flush(self) -> None:
        async with self._lock:
            buf, self._buf = self._buf, defaultdict(list)
            if not buf:
                return
            self._part_seq += 1
            seq = self._part_seq
            try:
                n_rows, n_files = await asyncio.to_thread(self._write_parts, buf, seq)
                self.rows_written += n_rows
                self.files_written += n_files
            except Exception as e:  # never crash the bot because of disk problems
                self.write_errors += 1
                log.error("recorder_write_failed", error=repr(e))

    def _write_parts(self, buf: dict[BufKey, list[tuple[Any, ...]]], seq: int) -> tuple[int, int]:
        n_rows = n_files = 0
        for (date, hour, symbol, stream), rows in buf.items():
            if not rows:
                continue
            d = self.raw / f"date={date}" / f"symbol={symbol}" / stream
            d.mkdir(parents=True, exist_ok=True)
            schema = SCHEMAS[stream]
            cols = list(zip(*rows, strict=True))
            table = pa.table({f.name: pa.array(c, type=f.type) for f, c in zip(schema, cols, strict=True)},
                             schema=schema)
            # If an hour was already compacted (late rows), keep writing parts; compaction merges them.
            tmp = d / f".{hour}.part-{seq:06d}.tmp"
            pq.write_table(table, tmp)
            tmp.rename(d / f"{hour}.part-{seq:06d}.parquet")
            n_rows += len(rows)
            n_files += 1
        return n_rows, n_files

    async def compact(self, before_ms: int) -> int:
        """Compact all hours that ended before `before_ms`."""
        try:
            async with self._lock:
                return await asyncio.to_thread(self._compact_sync, before_ms)
        except Exception as e:
            self.write_errors += 1
            log.error("recorder_compact_failed", error=repr(e))
            return 0

    def _compact_sync(self, before_ms: int) -> int:
        if not self.raw.exists():
            return 0
        cutoff = _utc(before_ms).replace(minute=0, second=0, microsecond=0)
        done = 0
        for stream_dir in self.raw.glob("date=*/symbol=*/*"):
            date = stream_dir.parent.parent.name.removeprefix("date=")
            parts: dict[str, list[Path]] = defaultdict(list)
            for f in stream_dir.iterdir():
                m = _PART_RE.match(f.name)
                if m:
                    parts[m.group(1)].append(f)
            for hour, files in parts.items():
                hour_start = dt.datetime.fromisoformat(f"{date}T{hour}:00:00+00:00")
                if hour_start + dt.timedelta(hours=1) > cutoff:
                    continue
                target = stream_dir / f"{hour}.parquet"
                tables = [pq.read_table(f) for f in sorted(files)]
                if target.exists():
                    tables.insert(0, pq.read_table(target))
                merged = pa.concat_tables(tables).sort_by("recv_ts")
                tmp = stream_dir / f".{hour}.compact.tmp"
                pq.write_table(merged, tmp)
                tmp.rename(target)
                for f in files:
                    f.unlink()
                done += 1
        return done

    async def enforce_retention(self, now_ms: int) -> int:
        cutoff = (_utc(now_ms) - dt.timedelta(days=self.retention_days)).strftime("%Y-%m-%d")

        def _run() -> int:
            n = 0
            if not self.raw.exists():
                return 0
            for d in self.raw.glob("date=*"):
                if d.name.removeprefix("date=") < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
                    n += 1
            return n

        return await asyncio.to_thread(_run)

    async def run(self, now_ms: Any) -> None:
        """Background loop: flush every `flush_s`, compact finished hours, daily retention."""
        last_retention = 0
        while True:
            await asyncio.sleep(self.flush_s)
            await self.flush()
            now = now_ms()
            await self.compact(now)
            if now - last_retention > 3600_000:
                await self.enforce_retention(now)
                last_retention = now

    async def close(self, now_ms: int) -> None:
        await self.flush()
        await self.compact(now_ms + 3600_000 * 2)  # compact everything, including the current hour


# ---- reading ----


def list_files(root: str | Path, stream: str, symbol: str | None = None,
               dates: Iterable[str] | None = None) -> list[Path]:
    raw = Path(root) / "raw"
    date_dirs = ([raw / f"date={d}" for d in dates] if dates is not None else sorted(raw.glob("date=*")))
    out: list[Path] = []
    for dd in date_dirs:
        sym_dirs = [dd / f"symbol={symbol}"] if symbol else sorted(dd.glob("symbol=*"))
        for sd in sym_dirs:
            sdir = sd / stream
            if not sdir.is_dir():
                continue
            hours_compacted = {m.group(1) for f in sdir.iterdir() if (m := _HOUR_RE.match(f.name))}
            for f in sorted(sdir.iterdir()):
                if _HOUR_RE.match(f.name):
                    out.append(f)
                elif (m := _PART_RE.match(f.name)) and m.group(1) not in hours_compacted:
                    out.append(f)
    return out


def read_stream(root: str | Path, stream: str, symbol: str, start_ms: int | None = None,
                end_ms: int | None = None) -> pa.Table:
    dates = None
    if start_ms is not None and end_ms is not None:
        d0, d1 = _utc(start_ms).date(), _utc(end_ms).date()
        dates = [(d0 + dt.timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]
    files = list_files(root, stream, symbol, dates)
    if not files:
        return SCHEMAS[stream].empty_table()
    table = pa.concat_tables([pq.read_table(f) for f in files])
    if start_ms is not None or end_ms is not None:
        import pyarrow.compute as pc

        mask = None
        if start_ms is not None:
            mask = pc.greater_equal(table["recv_ts"], start_ms)
        if end_ms is not None:
            m2 = pc.less(table["recv_ts"], end_ms)
            mask = m2 if mask is None else pc.and_(mask, m2)
        table = table.filter(mask)
    return table.sort_by("recv_ts")


def recorded_symbols(root: str | Path, stream: str = "bookTicker") -> list[str]:
    raw = Path(root) / "raw"
    return sorted({p.parent.name.removeprefix("symbol=") for p in raw.glob(f"date=*/symbol=*/{stream}")})
