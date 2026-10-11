"""ReconnectingWs against a local aiohttp WebSocket server: rolls and reconnects."""

import asyncio
import random

from aiohttp import WSMsgType, web

from riverbank.exchange.ws import ReconnectingWs, backoff_delay


class Broadcaster:
    """Every connected client receives the same global sequence (like one Binance stream)."""

    def __init__(self):
        self.clients: set[web.WebSocketResponse] = set()
        self.seq = 0
        self.kill_after: int | None = None

    async def handler(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.clients.add(ws)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.CLOSE:
                    break
        finally:
            self.clients.discard(ws)
        return ws

    async def pump(self):
        while True:
            await asyncio.sleep(0.005)
            self.seq += 1
            for c in list(self.clients):
                if self.kill_after is not None and self.seq == self.kill_after:
                    await c.close()
                    continue
                try:
                    await c.send_str(str(self.seq))
                except Exception:
                    pass


async def start_server(b):
    app = web.Application()
    app.router.add_get("/ws", b.handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}/ws"


def test_backoff_bounds():
    rng = random.Random(1)
    for attempt in range(20):
        d = backoff_delay(attempt, base_s=0.5, cap_s=30, rng=rng)
        assert 0 <= d <= min(30, 0.5 * 2 ** attempt)


async def test_roll_is_gapless_and_reconnect_reports_status():
    b = Broadcaster()
    runner, url = await start_server(b)
    pump = asyncio.create_task(b.pump())
    got: list[int] = []
    status: list[bool] = []
    ws = ReconnectingWs(url, lambda t, ts: got.append(int(t)), lambda c, d: status.append(c),
                        roll_after_s=0.4, overlap_s=0.1, heartbeat_s=5, idle_timeout_s=2)
    task = asyncio.create_task(ws.run())
    await asyncio.sleep(1.5)
    assert ws.rolls >= 2
    # Consumer-side dedupe (like update-id dedupe): the deduped sequence must have no gaps.
    dedup, last = [], 0
    for x in got:
        if x > last:
            dedup.append(x)
            last = x
    assert dedup == list(range(dedup[0], dedup[-1] + 1)), "gap during roll"
    # Server kills the connection -> status False, then reconnect -> True.
    b.kill_after = b.seq + 5
    await asyncio.sleep(2.0)
    assert False in status and status[-1] is True and ws.disconnects >= 1
    ws.stop()
    task.cancel()
    pump.cancel()
    await asyncio.gather(task, pump, return_exceptions=True)
    await runner.cleanup()
