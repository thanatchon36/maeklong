"""Shadow-vs-live gap (SPEC §5.9): the real venue (demo/live) and its shadow twin quote the same
symbols in the same process (orders/live_venue.py), so their fill rate and markout can be compared
on the same time window. Gap = real − shadow; its SE combines both SEs (independent samples).

    uv run python -m riverbank.analytics.gap --db state/live/riverbank.sqlite --start 2026-10-12 --end 2026-10-18
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import sqlite3
from typing import Any

from riverbank.analytics.markout import stats
from riverbank.analytics.report import DAY_MS, day_bounds, edge_values, fills, fmt_stats

TWIN = "shadow"


def _quoted_ms(conn: sqlite3.Connection, venue: str, symbol: str, t0: int, t1: int) -> int:
    dates = [dt.datetime.fromtimestamp(t / 1000, dt.UTC).strftime("%Y-%m-%d") for t in range(t0, t1, DAY_MS)]
    q = f"SELECT COALESCE(SUM(quoted_ms), 0) FROM quote_uptime WHERE venue=? AND symbol=? " \
        f"AND date IN ({','.join('?' * len(dates))})"
    return int(conn.execute(q, (venue, symbol, *dates)).fetchone()[0])


def gap_rows(conn: sqlite3.Connection, real: str, twin: str, t0: int, t1: int, horizon: int) -> list[dict[str, Any]]:
    if not conn.execute("SELECT 1 FROM fills WHERE venue=? AND ts>=? AND ts<? LIMIT 1", (twin, t0, t1)).fetchone():
        return []
    out = []
    for sym in sorted({f["symbol"] for f in fills(conn, real, t0, t1)}):
        r: dict[str, Any] = {"symbol": sym}
        for key, venue in (("real", real), ("shadow", twin)):
            vf = fills(conn, venue, t0, t1, sym)
            q_ms = _quoted_ms(conn, venue, sym, t0, t1)
            r[f"{key}_fills"] = len(vf)
            r[f"{key}_fph"] = len(vf) / (q_ms / 3_600_000) if q_ms else float("nan")
            r[f"{key}_mo5"] = stats(f["mo_5"] for f in vf if f["mo_5"] is not None)
            r[f"{key}_edge"] = stats(edge_values(vf, horizon))
        r["edge_gap"] = r["real_edge"].mean - r["shadow_edge"].mean
        r["edge_gap_se"] = math.sqrt(r["real_edge"].se ** 2 + r["shadow_edge"].se ** 2)
        out.append(r)
    return out


def gap_lines(conn: sqlite3.Connection, real: str, twin: str, t0: int, t1: int, horizon: int) -> list[str]:
    rows = gap_rows(conn, real, twin, t0, t1, horizon)
    if not rows:
        return []
    lines = [f"### Shadow-vs-live gap (`{real}` vs twin `{twin}`)", "",
             "Same symbols, same market data, same window. Gap = real − shadow (bps), ± combined SE.", "",
             f"| symbol | fills real | fills shadow | fills/quoted h real | fills/quoted h shadow | mo 5s real | "
             f"mo 5s shadow | edge {horizon}s real | edge {horizon}s shadow | edge gap |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in rows:
        gap = "–" if math.isnan(r["edge_gap"]) else f"{r['edge_gap']:+.2f} ± {r['edge_gap_se']:.2f}"
        lines.append(f"| {r['symbol']} | {r['real_fills']} | {r['shadow_fills']} | {_num(r['real_fph'])} | "
                     f"{_num(r['shadow_fph'])} | {fmt_stats(r['real_mo5'])} | {fmt_stats(r['shadow_mo5'])} | "
                     f"{fmt_stats(r['real_edge'])} | {fmt_stats(r['shadow_edge'])} | {gap} |")
    return [*lines, ""]


def _num(x: float) -> str:
    return "–" if math.isnan(x) else f"{x:.1f}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--start", required=True, help="first UTC date, YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="last UTC date (inclusive)")
    ap.add_argument("--real", default="live")
    ap.add_argument("--horizon", type=int, default=60)
    a = ap.parse_args()
    conn = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    lines = gap_lines(conn, a.real, TWIN, day_bounds(a.start)[0], day_bounds(a.end)[1], a.horizon)
    print("\n".join(lines) if lines else f"no fills for both `{a.real}` and its twin `{TWIN}` in the window")


if __name__ == "__main__":
    main()
