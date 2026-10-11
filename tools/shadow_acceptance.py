"""M3 acceptance report for a multi-day shadow run (SPEC §12 M3: 7-day shadow run on candidates,
daily report with markout per symbol and PnL decomposition, deterministic replay).

    uv run python tools/shadow_acceptance.py --start 2026-10-12 --end 2026-10-18 \
        [--db state/riverbank.sqlite] [--data data] [--out reports/m3-acceptance]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
from pathlib import Path

from riverbank.analytics.markout import stats
from riverbank.analytics.report import build_daily_report, edge_values, fills, fmt_stats, promotion_status
from riverbank.core.config import load_config
from riverbank.exchange.specs import parse_exchange_info
from riverbank.marketdata.recorder import recorded_symbols
from riverbank.ops.logging import setup_logging
from riverbank.sim.replay import load_events, run_replay


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/config.yaml")
    ap.add_argument("--db", default="state/riverbank.sqlite")
    ap.add_argument("--data", default="data")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--venue", default="paper")
    ap.add_argument("--out", default="reports/m3-acceptance")
    ap.add_argument("--replay-symbols", type=int, default=3)
    args = ap.parse_args()
    setup_logging("WARNING")
    cfg = load_config(args.config)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    d0, d1 = dt.date.fromisoformat(args.start), dt.date.fromisoformat(args.end)
    days = [(d0 + dt.timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]
    daily = {d: build_daily_report(args.db, d, cfg.ops.reports_dir, cfg) for d in days}
    t0 = int(dt.datetime.fromisoformat(args.start).replace(tzinfo=dt.UTC).timestamp() * 1000)
    t1 = int((dt.datetime.fromisoformat(args.end).replace(tzinfo=dt.UTC) + dt.timedelta(days=1)).timestamp() * 1000)
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    vf = fills(conn, args.venue, t0, t1)
    first, last = (vf[0]["ts"], vf[-1]["ts"]) if vf else (t0, t0)
    h = cfg.universe.promotion_horizon_s
    lines = [f"# M3 acceptance — shadow run {args.start} → {args.end}", "",
             f"- Fills (venue `{args.venue}`): {len(vf)}; first {dt.datetime.fromtimestamp(first / 1000, dt.UTC)}, "
             f"last {dt.datetime.fromtimestamp(last / 1000, dt.UTC)}",
             f"- Daily reports: {len(daily)} ({', '.join(str(p.parent.name) for p in daily.values())})", "",
             f"## Per symbol over the window (edge at {h} s)", "",
             "| symbol | fills | edge | promotion |", "|---|---:|---:|---|"]
    by_sym: dict[str, list[dict]] = {}
    for f in vf:
        by_sym.setdefault(f["symbol"], []).append(f)
    readiness = {}
    for sym, sf in sorted(by_sym.items()):
        st = stats(edge_values(sf, h))
        ok, why = promotion_status(len(sf), st, cfg.universe.min_fills_for_promotion)
        readiness[sym] = ok
        lines.append(f"| {sym} | {len(sf)} | {fmt_stats(st)} | {'READY' if ok else 'not ready'}: {why} |")
    # deterministic replay over the first recorded hour of the most active symbols
    syms = [s for s, _ in sorted(by_sym.items(), key=lambda kv: -len(kv[1]))][: args.replay_symbols]
    syms = [s for s in syms if s in set(recorded_symbols(args.data))]
    determinism = None
    if syms:
        meta = sorted((Path(args.data) / "meta").glob("exchange_info-*.json"))
        if meta:
            specs = parse_exchange_info(json.loads(meta[-1].read_text())).specs
            ev = load_events(args.data, syms, t0, t0 + 3600_000) or load_events(args.data, syms, first,
                                                                                 first + 3600_000)
            if ev:
                a = run_replay(cfg, specs, ev, syms)
                b = run_replay(cfg, specs, ev, syms)
                determinism = a.digest() == b.digest()
                lines += ["", "## Deterministic replay", "",
                          f"- {len(ev)} recorded events, symbols {syms}: fills {len(a.fills)}; digests "
                          f"{a.digest()[:16]}… vs {b.digest()[:16]}… → **{'IDENTICAL' if determinism else 'DIFF'}**"]
    days_cov = (last - first) / 86_400_000 if vf else 0.0
    verdict = {"duration_ge_7d": days_cov >= 7.0 - 1e-6, "daily_reports": len(daily) >= 7,
               "deterministic_replay": bool(determinism)}
    lines += ["", "## Acceptance", ""] + [f"- {k}: {'PASS' if v else 'NOT MET'}" for k, v in verdict.items()]
    (out / "report.md").write_text("\n".join(lines) + "\n")
    (out / "summary.json").write_text(json.dumps({"verdict": verdict, "readiness": readiness,
                                                  "fills": len(vf)}, indent=1))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
