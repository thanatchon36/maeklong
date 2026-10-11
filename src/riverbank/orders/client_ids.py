"""Deterministic client order ids: rb-{SYMBOL}-{BUY|SELL}-{seq base36}.

`seq` never repeats across restarts: the allocator reserves blocks of sequence numbers
through `reserve_block(size) -> start`, which the owner persists (SQLite kv) before use.
Binance rule: ^[.A-Z:/a-z0-9_-]{1,36}$ (DISCREPANCIES #12).
"""

from __future__ import annotations

import re
from collections.abc import Callable

from riverbank.core.models import Side

PREFIX = "rb-"
_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"
_VALID = re.compile(r"^[.A-Z:/a-z0-9_-]{1,36}$")


def b36(n: int) -> str:
    if n < 0:
        raise ValueError("negative")
    out = ""
    while True:
        n, r = divmod(n, 36)
        out = _ALPHABET[r] + out
        if n == 0:
            return out


def is_ours(client_id: str) -> bool:
    return client_id.startswith(PREFIX)


def parse(client_id: str) -> tuple[str, Side, int] | None:
    if not is_ours(client_id):
        return None
    body = client_id[len(PREFIX):]
    try:
        symbol, side, seq = body.rsplit("-", 2)
        return symbol, Side(side), int(seq, 36)
    except ValueError:
        return None


class ClientIdAllocator:
    def __init__(self, reserve_block: Callable[[int], int] | None = None, block: int = 1_000_000,
                 start: int = 0) -> None:
        self._reserve = reserve_block
        self._block = block
        self._next = start
        self._end = start if reserve_block else 2**62
        if reserve_block is None and start == 0:
            self._next = 1

    def next(self, symbol: str, side: Side) -> str:
        if self._next >= self._end:
            assert self._reserve is not None
            self._next = self._reserve(self._block)
            self._end = self._next + self._block
        seq = self._next
        self._next += 1
        cid = f"{PREFIX}{symbol}-{side.value}-{b36(seq)}"
        if not _VALID.match(cid):
            raise ValueError(f"client id invalid/too long: {cid}")
        return cid
