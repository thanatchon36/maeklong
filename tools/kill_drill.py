"""Kill-switch drill against a RUNNING bot: touch ./KILL and measure the time until HALTED.

The bot will HALT (cancel all, flatten) and stay halted — run this at the END of a demo run.
Result is printed and stored in the bot database (kv `drill:kill`) for demo_acceptance.py.

    uv run python tools/kill_drill.py --db state/demo/riverbank.sqlite [--kill-file KILL]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="state/demo/riverbank.sqlite")
    ap.add_argument("--kill-file", default="KILL")
    ap.add_argument("--timeout-s", type=float, default=10)
    args = ap.parse_args()
    kill = Path(args.kill_file)
    if kill.exists():
        raise SystemExit(f"{kill} already exists")
    t0 = int(time.time() * 1000)
    kill.touch()
    conn = sqlite3.connect(args.db, timeout=30)
    halted = None
    deadline = time.time() + args.timeout_s
    while time.time() < deadline and halted is None:
        row = conn.execute("SELECT ts, venue, detail FROM risk_events WHERE state_after='HALTED' AND ts>=? "
                           "ORDER BY ts LIMIT 1", (t0,)).fetchone()
        if row:
            halted = row
        else:
            time.sleep(0.05)
    if halted is None:
        print(f"FAIL: no HALTED risk event within {args.timeout_s}s")
        result = {"t0": t0, "halted": False}
    else:
        latency = (halted[0] - t0) / 1000
        print(f"HALTED after {latency:.3f}s (venue {halted[1]}: {halted[2]}) -> {'PASS' if latency < 1 else 'FAIL'}")
        result = {"t0": t0, "halted": True, "latency_s": latency, "venue": halted[1]}
    conn.execute("INSERT INTO kv(key, value) VALUES('drill:kill', ?) ON CONFLICT(key) DO UPDATE SET "
                 "value=excluded.value", (json.dumps(result),))
    conn.commit()
    conn.close()
    print(f"note: {kill} stays in place; the bot refuses to restart until you delete it and run "
          "tools/clear_halt.py")


if __name__ == "__main__":
    main()
