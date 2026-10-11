"""Kill switch (SPEC §5.6): `touch ./KILL`, SIGUSR1 or Telegram `/kill` → HALTED within 1 s.

HALT is persisted (OPEN_QUESTIONS B6) so an automatic restart (docker restart policy) can
never resume trading: the bot refuses to start while a HALT record or the KILL file exists.
The operator clears it with `tools/clear_halt.py` after investigating.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

HALT_KEY = "halt:record"


class KV(Protocol):
    def kv_get(self, key: str) -> str | None: ...
    def kv_set_sync(self, key: str, value: str) -> None: ...
    def kv_delete_sync(self, key: str) -> None: ...


def persist_halt(kv: KV, reason: str, mode: str) -> None:
    if kv.kv_get(HALT_KEY):
        return  # keep the first (root-cause) reason
    kv.kv_set_sync(HALT_KEY, json.dumps({"ts": int(time.time() * 1000), "mode": mode, "reason": reason}))


def halt_record(kv: KV) -> dict[str, object] | None:
    raw = kv.kv_get(HALT_KEY)
    if not raw:
        return None
    data: dict[str, object] = json.loads(raw)
    return data


def clear_halt(kv: KV) -> None:
    kv.kv_delete_sync(HALT_KEY)


def startup_blockers(kv: KV, kill_file: str | Path) -> list[str]:
    out = []
    if Path(kill_file).exists():
        out.append(f"kill file '{kill_file}' exists (delete it after investigating)")
    rec = halt_record(kv)
    if rec is not None:
        out.append(f"persisted HALT: {rec} (clear with: uv run python tools/clear_halt.py)")
    return out


async def watch_kill_file(path: str | Path, on_kill: Callable[[str], None], poll_s: float = 0.2) -> None:
    p = Path(path)
    while True:
        if p.exists():
            on_kill(f"kill file {p} detected")
            return
        await asyncio.sleep(poll_s)


def install_sigusr1(loop: asyncio.AbstractEventLoop, on_kill: Callable[[str], None]) -> bool:
    if not hasattr(signal, "SIGUSR1") or os.name == "nt":
        return False
    loop.add_signal_handler(signal.SIGUSR1, lambda: on_kill("SIGUSR1"))
    return True
