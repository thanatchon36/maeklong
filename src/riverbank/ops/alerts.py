"""Alerts (SPEC §5.11, OPEN_QUESTIONS A4): Telegram + an always-on local alerts log.

Every alert is (1) written to the JSON log, (2) appended to `logs/alerts.log`, and (3) sent
to Telegram if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set. Telegram failures never
block or crash the bot. Incoming `/kill` (and `/status`) commands are accepted only from
the configured chat id.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import aiohttp

from riverbank.ops.logging import get_logger

log = get_logger("alerts")

TELEGRAM_API = "https://api.telegram.org"


class Alerts:
    def __init__(self, log_dir: str | Path = "logs", *, token: str | None = None, chat_id: str | None = None,
                 enabled: bool = True, prefix: str = "riverbank", min_interval_s: float = 1.0) -> None:
        self.path = Path(log_dir) / "alerts.log"
        self.token = token if token is not None else os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id if chat_id is not None else os.environ.get("TELEGRAM_CHAT_ID", "")
        self.telegram = bool(enabled and self.token and self.chat_id)
        self.prefix = prefix
        self.min_interval_s = min_interval_s
        self._q: asyncio.Queue[str] | None = None
        self._last: dict[str, float] = {}
        self.sent = 0
        self.failed = 0
        self.history: list[tuple[str, str, str]] = []
        self.repo_sink: Callable[[int, str, str, str], None] | None = None

    def send(self, level: str, kind: str, message: str, *, dedupe_key: str | None = None) -> None:
        now = time.time()
        key = dedupe_key or f"{kind}:{message}"
        if now - self._last.get(key, 0.0) < self.min_interval_s:
            return
        self._last[key] = now
        self.history.append((level, kind, message))
        if len(self.history) > 1000:
            del self.history[:500]
        log.warning("alert", level=level, kind=kind, message=message)
        ts = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as f:
                f.write(f"{ts} [{level}] {kind}: {message}\n")
        except OSError as e:
            log.error("alert_file_failed", error=repr(e))
        if self.repo_sink is not None:
            self.repo_sink(int(now * 1000), level, kind, message)
        if self.telegram and self._q is not None:
            self._q.put_nowait(f"[{self.prefix}] {level} {kind}: {message}")

    async def run_sender(self, session: aiohttp.ClientSession | None = None) -> None:
        self._q = asyncio.Queue()
        if not self.telegram:
            return
        own = session is None
        sess = session or aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        try:
            while True:
                text = await self._q.get()
                try:
                    async with sess.post(f"{TELEGRAM_API}/bot{self.token}/sendMessage",
                                         json={"chat_id": self.chat_id, "text": text[:4000]}) as r:
                        if r.status == 200:
                            self.sent += 1
                        else:
                            self.failed += 1
                except (aiohttp.ClientError, TimeoutError):
                    self.failed += 1
        finally:
            if own:
                await sess.close()

    async def poll_commands(self, handlers: dict[str, Callable[[], Awaitable[str] | str]],
                            session: aiohttp.ClientSession | None = None) -> None:
        """Long-poll Telegram getUpdates; run handlers for commands from the configured chat only."""
        if not self.telegram:
            return
        own = session is None
        sess = session or aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40))
        offset = 0
        try:
            while True:
                try:
                    async with sess.get(f"{TELEGRAM_API}/bot{self.token}/getUpdates",
                                        params={"timeout": 25, "offset": offset}) as r:
                        data: dict[str, Any] = await r.json()
                except (aiohttp.ClientError, TimeoutError, ValueError):
                    await asyncio.sleep(5)
                    continue
                for upd in data.get("result", []):
                    offset = max(offset, int(upd.get("update_id", 0)) + 1)
                    msg = upd.get("message") or {}
                    if str((msg.get("chat") or {}).get("id")) != str(self.chat_id):
                        continue  # ignore anyone else
                    cmd = (msg.get("text") or "").strip().split("@")[0].lower()
                    h = handlers.get(cmd)
                    if h is None:
                        continue
                    res = h()
                    reply = await res if asyncio.iscoroutine(res) else res
                    if reply:
                        self.send("INFO", "command", f"{cmd}: {reply}", dedupe_key=f"cmd:{time.time()}")
        finally:
            if own:
                await sess.close()
