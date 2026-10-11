"""Cancel all riverbank (`rb-*`) open orders — standalone, works without the main process.

    uv run python tools/cancel_all.py              # demo, dry run (lists orders)
    uv run python tools/cancel_all.py --execute    # demo, cancel
    MODE=live LIVE_CONFIRM=I_UNDERSTAND_THE_RISK uv run python tools/cancel_all.py --live --execute
    ... --all   # also orders not created by riverbank (whole account)
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _exchange_cli import add_env_args, client
from riverbank.exchange import endpoints as ep
from riverbank.exchange.rest import ApiError
from riverbank.orders.client_ids import is_ours


async def run(args: argparse.Namespace) -> int:
    rest, _ = client(args)
    try:
        orders = await rest.signed("GET", ep.OPEN_ORDERS)
        targets = [o for o in orders if args.all or is_ours(str(o.get("clientOrderId", "")))]
        print(f"open orders: {len(orders)}; to cancel: {len(targets)}")
        for o in targets:
            print(f"  {o['symbol']:<14} {o['side']:<4} {o['price']:>14} x {o['origQty']:<10} {o['clientOrderId']}")
        if not args.execute:
            print("dry run (add --execute to cancel)")
            return 0
        failed = 0
        for o in targets:
            try:
                await rest.signed("DELETE", ep.ORDER, {"symbol": o["symbol"], "origClientOrderId": o["clientOrderId"]})
            except ApiError as e:
                if e.code not in (-2011, -2013):
                    failed += 1
                    print(f"  FAILED {o['clientOrderId']}: {e}")
        left = [o for o in await rest.signed("GET", ep.OPEN_ORDERS)
                if args.all or is_ours(str(o.get("clientOrderId", "")))]
        print(f"done: remaining targeted open orders = {len(left)}, failures = {failed}")
        return 0 if not left else 1
    finally:
        await rest.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    add_env_args(ap)
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--all", action="store_true", help="cancel every open order, not only rb-*")
    raise SystemExit(asyncio.run(run(ap.parse_args())))


if __name__ == "__main__":
    main()
