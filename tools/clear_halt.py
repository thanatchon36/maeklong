"""Show and (after confirmation) clear a persisted HALT so the bot may start again.

    uv run python tools/clear_halt.py [--db state/riverbank.sqlite] [--yes]
"""

from __future__ import annotations

import argparse
from pathlib import Path

from riverbank.risk.killswitch import clear_halt, halt_record
from riverbank.storage.sqlite import SqliteRepository


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="state/riverbank.sqlite")
    ap.add_argument("--kill-file", default="KILL")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()
    repo = SqliteRepository(args.db)
    try:
        rec = halt_record(repo)
        if Path(args.kill_file).exists():
            print(f"NOTE: kill file '{args.kill_file}' exists — delete it yourself after investigating.")
        if rec is None:
            print("no persisted HALT")
            return
        print(f"persisted HALT: {rec}")
        if not args.yes and input("Clear it? Type 'clear' to confirm: ").strip() != "clear":
            print("not cleared")
            return
        clear_halt(repo)
        print("HALT cleared. Check positions/orders on the exchange before restarting.")
    finally:
        repo.close()


if __name__ == "__main__":
    main()
