"""tools/cancel_all.py and tools/flatten_all.py work standalone (SPEC §11.6), gated for production."""

import asyncio
import os
import sys
from decimal import Decimal
from pathlib import Path

from tests.integration.fake_binance import FakeBinance

ROOT = Path(__file__).resolve().parents[2]


async def run_tool(*args, env_extra=None):
    env = {**os.environ, "BINANCE_API_KEY": "k" * 12, "BINANCE_API_SECRET": "s" * 12}
    env.pop("MODE", None)
    env.pop("LIVE_CONFIRM", None)
    env.update(env_extra or {})
    p = await asyncio.create_subprocess_exec(sys.executable, *args, cwd=ROOT, env=env,
                                             stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await p.communicate()
    return p.returncode, out.decode()


async def test_cancel_all_dry_run_then_execute():
    fake = FakeBinance()
    base = await fake.start()
    try:
        fake.add_order("DOGEUSDT", "BUY", "0.08", "100", "rb-DOGEUSDT-BUY-1")
        fake.add_order("DOGEUSDT", "BUY", "0.08", "100", "manual-1")
        rc, out = await run_tool("tools/cancel_all.py", "--base-url", base)
        assert rc == 0 and "dry run" in out and fake.orders["rb-DOGEUSDT-BUY-1"]["status"] == "NEW"
        rc, out = await run_tool("tools/cancel_all.py", "--base-url", base, "--execute")
        assert rc == 0, out
        assert fake.orders["rb-DOGEUSDT-BUY-1"]["status"] == "CANCELED"
        assert fake.orders["manual-1"]["status"] == "NEW"
    finally:
        await fake.stop()


async def test_flatten_all_taker_flattens():
    fake = FakeBinance()
    base = await fake.start()
    try:
        fake.set_book("DOGEUSDT", "0.08635", "0.08636")
        fake.positions["DOGEUSDT"] = Decimal(300)
        fake.entry["DOGEUSDT"] = Decimal("0.08")
        rc, out = await run_tool("tools/flatten_all.py", "--base-url", base, "--execute", "--taker", "--yes",
                                 "--requote-s", "0.2", "--timeout-s", "10")
        assert rc == 0 and "FLAT" in out, out
        assert fake.positions["DOGEUSDT"] == 0
    finally:
        await fake.stop()


async def test_live_flag_refused_without_env():
    rc, out = await run_tool("tools/cancel_all.py", "--live")
    assert rc != 0 and "REFUSED" in out
