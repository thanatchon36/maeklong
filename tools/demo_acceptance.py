"""M4 acceptance report for a demo run (SPEC §12 M4): 72 h demo run, zero unresolved
RECON_MISMATCH, all fault-injection tests pass, kill switch < 1 s.

    uv run python tools/demo_acceptance.py --db state/demo/riverbank.sqlite [--venue demo] \
        [--skip-fault-tests] [--out reports/m4-acceptance]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="state/demo/riverbank.sqlite")
    ap.add_argument("--venue", default="demo")
    ap.add_argument("--hours", type=float, default=72)
    ap.add_argument("--skip-fault-tests", action="store_true")
    ap.add_argument("--out", default="reports/m4-acceptance")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(args.db)
    first, last = conn.execute("SELECT MIN(ts_created), MAX(ts_updated) FROM orders WHERE venue=?",
                               (args.venue,)).fetchone()
    hours = ((last or 0) - (first or 0)) / 3_600_000
    by_status = dict(conn.execute("SELECT status, COUNT(*) FROM orders WHERE venue=? GROUP BY status",
                                  (args.venue,)).fetchall())
    post_only = conn.execute("SELECT COUNT(*) FROM orders WHERE venue=? AND reject_reason='post_only'",
                             (args.venue,)).fetchone()[0]
    fills = conn.execute("SELECT COUNT(*) FROM fills WHERE venue=?", (args.venue,)).fetchone()[0]
    recon_total, recon_unres = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(CASE WHEN resolved=0 THEN 1 ELSE 0 END), 0) FROM recon_events WHERE venue=?",
        (args.venue,)).fetchone()
    risk = conn.execute("SELECT ts, level, rule, detail, state_after FROM risk_events WHERE venue=? ORDER BY ts",
                        (args.venue,)).fetchall()
    drill_raw = conn.execute("SELECT value FROM kv WHERE key='drill:kill'").fetchone()
    drill = json.loads(drill_raw[0]) if drill_raw else None
    conn.close()
    fault_ok = None
    fault_out = ""
    if not args.skip_fault_tests:
        p = subprocess.run([sys.executable, "-m", "pytest", "-q", "tests/integration/test_demo_fault_injection.py",
                            "tests/integration/test_tools_standalone.py"], cwd=ROOT, capture_output=True,
                           text=True)
        fault_ok = p.returncode == 0
        fault_out = (p.stdout.strip().splitlines() or [""])[-1]
    verdict = {
        "duration_ge_72h": hours >= args.hours,
        "zero_unresolved_recon_mismatch": recon_unres == 0,
        "fault_injection_tests_pass": fault_ok,
        "kill_switch_lt_1s": bool(drill and drill.get("halted") and drill.get("latency_s", 9) < 1.0),
    }
    lines = [f"# M4 acceptance — demo run (venue `{args.venue}`)", "",
             f"- Orders: first {_t(first)}, last {_t(last)} → **{hours:.1f} h**",
             f"- Order statuses: {by_status}; post-only rejects: {post_only}; fills: {fills}",
             f"- RECON_MISMATCH: {recon_total} total, **{recon_unres} unresolved**",
             f"- Fault-injection suite: {fault_out or 'skipped'}",
             f"- Kill-switch drill: {drill if drill else 'not run (tools/kill_drill.py)'}", "",
             "## Risk events", ""]
    lines += [f"- {_t(r[0])} {r[1]} {r[2]} → {r[4]}: {r[3]}" for r in risk] or ["- none"]
    lines += ["", "## Acceptance", ""]
    lines += [f"- {k}: {'PASS' if v else ('not run' if v is None else 'NOT MET')}" for k, v in verdict.items()]
    (out / "report.md").write_text("\n".join(lines) + "\n")
    (out / "summary.json").write_text(json.dumps({"verdict": verdict, "hours": hours, "recon_unresolved": recon_unres,
                                                  "drill": drill}, indent=1))
    print("\n".join(lines))


def _t(ms: int | None) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.UTC).isoformat(timespec="seconds") if ms else "n/a"


if __name__ == "__main__":
    main()
