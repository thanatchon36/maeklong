"""Capture a raw diff-depth stream + REST snapshots from the PUBLIC production API.

Produces a gzip JSONL file used by tests/replay/test_book_replay.py (book-sync replay
with injected gaps). Public endpoints only, no keys.

    uv run python tools/capture_depth_fixture.py --symbols DOGEUSDT XRPUSDT --seconds 90 \
        --out tests/fixtures/depth_capture.jsonl.gz
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import time

import aiohttp
import orjson

from riverbank.core.models import Mode
from riverbank.exchange.environments import ENVIRONMENTS


def now_ms() -> int:
    return time.time_ns() // 1_000_000


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=["DOGEUSDT", "XRPUSDT"])
    ap.add_argument("--seconds", type=float, default=90)
    ap.add_argument("--snapshot-every", type=float, default=15)
    ap.add_argument("--out", default="tests/fixtures/depth_capture.jsonl.gz")
    args = ap.parse_args()
    env = ENVIRONMENTS[Mode.SHADOW]
    streams = "/".join(f"{s.lower()}@depth@100ms" for s in args.symbols)
    url = f"{env.ws_public}/stream?streams={streams}"
    lines: list[bytes] = []
    async with aiohttp.ClientSession() as sess:

        async def snap(sym: str, limit: int) -> None:
            async with sess.get(f"{env.rest}/fapi/v1/depth", params={"symbol": sym, "limit": limit}) as r:
                body = await r.json()
            lines.append(orjson.dumps({"t": "snap", "recv": now_ms(), "symbol": sym, "limit": limit, "data": body}))

        async with sess.ws_connect(url, max_msg_size=16 * 1024 * 1024) as ws:
            t0 = time.monotonic()
            next_snap = t0 + 2.0
            first = True
            while time.monotonic() - t0 < args.seconds:
                try:
                    msg = await ws.receive(timeout=1.0)
                except TimeoutError:
                    msg = None
                if msg is not None and msg.type is aiohttp.WSMsgType.TEXT:
                    lines.append(orjson.dumps({"t": "ws", "recv": now_ms(), "text": msg.data}))
                if time.monotonic() >= next_snap:
                    for s in args.symbols:
                        await snap(s, 500 if first else 100)
                    first = False
                    next_snap = time.monotonic() + args.snapshot_every
    with gzip.open(args.out, "wb") as f:
        f.write(b"\n".join(lines) + b"\n")
    n_ws = sum(1 for x in lines if x.startswith(b'{"t":"ws"'))
    print(f"wrote {args.out}: {n_ws} ws messages, {len(lines) - n_ws} snapshots")


if __name__ == "__main__":
    asyncio.run(main())
