"""Screener metrics from recorded history (Parquet), computed per UTC day and cached.

Per symbol and day (only days with >= `min_coverage_h` hours of bookTicker data):
  spread_bps       median quoted spread, bookTicker sampled once per second
  range_bps        (max mid - min mid) / median mid * 1e4 over the day
  quote_vol_musd   sum(price * qty) of aggTrades / 1e6 (scaled to 24 h if coverage < 24 h)
  fill_opportunity aggTrade prints per minute (every aggTrade prints at the touch)
The screener uses the median of the daily values over the lookback window. Completed days
never change, so their results are cached in a JSON file.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.parquet as pq

from riverbank.marketdata.recorder import list_files

DayMetrics = dict[str, float]


def _read(root: Path, stream: str, symbol: str, date: str, cols: list[str]) -> dict[str, np.ndarray] | None:
    for attempt in range(3):
        files = list_files(root, stream, symbol, [date])
        if not files:
            return None
        try:
            parts = [pq.read_table(f, columns=cols) for f in files]
            break
        except FileNotFoundError:  # the recorder compacted an hour's parts after we listed them: list again
            if attempt == 2:
                raise
    out = {c: np.concatenate([p.column(c).to_numpy() for p in parts]) for c in cols}
    order = np.argsort(out["recv_ts"], kind="stable")
    return {c: v[order] for c, v in out.items()}


def day_metrics(root: str | Path, symbol: str, date: str, min_coverage_h: float = 12.0) -> DayMetrics | None:
    root = Path(root)
    bt = _read(root, "bookTicker", symbol, date, ["recv_ts", "bid_px", "ask_px"])
    if bt is None or len(bt["recv_ts"]) < 2:
        return None
    ts = bt["recv_ts"]
    coverage_h = (ts[-1] - ts[0]) / 3_600_000
    if coverage_h < min_coverage_h:
        return None
    sec = ts // 1000
    last_idx = np.flatnonzero(np.r_[sec[1:] != sec[:-1], True])  # last sample in each second
    bid, ask = bt["bid_px"][last_idx], bt["ask_px"][last_idx]
    valid = (bid > 0) & (ask > bid)
    if not valid.any():
        return None
    bid, ask = bid[valid], ask[valid]
    mid = (bid + ask) / 2
    spread_bps = float(np.median((ask - bid) / mid * 1e4))
    med_mid = float(np.median(mid))
    range_bps = float((mid.max() - mid.min()) / med_mid * 1e4)
    out: DayMetrics = {"spread_bps": spread_bps, "range_bps": range_bps, "coverage_h": coverage_h,
                       "mid": med_mid}
    tr = _read(root, "aggTrade", symbol, date, ["recv_ts", "price", "qty"])
    if tr is not None and len(tr["recv_ts"]):
        minutes = max(1.0, coverage_h * 60)
        out["fill_opportunity"] = float(len(tr["recv_ts"]) / minutes)
        out["quote_vol_musd"] = float(np.sum(tr["price"] * tr["qty"]) / 1e6 * (24.0 / max(coverage_h, 1e-9)))
    return out


class HistoryMetrics:
    def __init__(self, root: str | Path, cache_path: str | Path | None = None) -> None:
        self.root = Path(root)
        self.cache_path = Path(cache_path) if cache_path else None
        self._cache: dict[str, Any] = {}
        if self.cache_path and self.cache_path.exists():
            try:
                self._cache = json.loads(self.cache_path.read_text())
            except (OSError, ValueError):
                self._cache = {}

    def metrics(self, symbol: str, now_ms: int, lookback_days: int) -> tuple[DayMetrics, int]:
        """Median of daily metrics over the last `lookback_days` complete days (+ today if covered).

        Returns (metrics, n_days). Empty metrics when there is no usable history (bootstrap).
        """
        today = dt.datetime.fromtimestamp(now_ms / 1000, dt.UTC).date()
        days: list[DayMetrics] = []
        dirty = False
        for i in range(lookback_days, -1, -1):
            date = (today - dt.timedelta(days=i)).isoformat()
            key = f"{symbol}|{date}"
            complete = i > 0
            if complete and key in self._cache:
                m = self._cache[key]
            else:
                m = day_metrics(self.root, symbol, date)
                if complete:
                    self._cache[key] = m
                    dirty = True
            if m:
                days.append(m)
        if dirty and self.cache_path:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(self._cache))
        if not days:
            return {}, 0
        keys = set.intersection(*(set(d) for d in days))
        return {k: float(np.median([d[k] for d in days])) for k in keys}, len(days)
