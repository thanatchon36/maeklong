"""Flatten all positions with reduce-only orders — standalone, works without the main process.

Default is passive: reduce-only post-only (GTX) limit orders at the touch, re-placed every few
seconds until flat or `--timeout-s`. `--taker` uses reduce-only MARKET orders (crosses the
spread, pays taker fee) and asks for confirmation unless `--yes`.

    uv run python tools/flatten_all.py                      # demo, dry run (shows positions)
    uv run python tools/flatten_all.py --execute            # demo, passive flatten
    MODE=live LIVE_CONFIRM=I_UNDERSTAND_THE_RISK uv run python tools/flatten_all.py --live --execute [--taker]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _exchange_cli import add_env_args, client
from riverbank.core.decimal_utils import floor_to_step, fmt
from riverbank.exchange import endpoints as ep
from riverbank.exchange.rest import ApiError
from riverbank.exchange.specs import fetch_exchange_info
from riverbank.orders.client_ids import is_ours


async def positions(rest) -> dict[str, Decimal]:  # type: ignore[no-untyped-def]
    out = {}
    for p in await rest.signed("GET", ep.POSITION_RISK):
        q = Decimal(str(p.get("positionAmt", "0")))
        if q != 0 and p.get("positionSide", "BOTH") == "BOTH":
            out[p["symbol"]] = q
    return out


async def run(args: argparse.Namespace) -> int:
    rest, _ = client(args)
    try:
        info = await fetch_exchange_info(rest)
        pos = await positions(rest)
        print(f"open positions: {len(pos)}")
        for s, q in pos.items():
            print(f"  {s:<14} {q}")
        if not pos:
            return 0
        if not args.execute:
            print("dry run (add --execute to flatten)")
            return 0
        if args.taker and not args.yes and input("Use MARKET (taker) orders? type 'yes': ").strip() != "yes":
            print("aborted")
            return 1
        # cancel our resting orders on those symbols first (reduce-only orders can conflict, -2022)
        for o in await rest.signed("GET", ep.OPEN_ORDERS):
            if o["symbol"] in pos and is_ours(str(o.get("clientOrderId", ""))):
                try:
                    await rest.signed("DELETE", ep.ORDER, {"symbol": o["symbol"],
                                                           "origClientOrderId": o["clientOrderId"]})
                except ApiError:
                    pass
        deadline = time.monotonic() + args.timeout_s
        seq = int(time.time())
        while pos and time.monotonic() < deadline:
            for sym, q in pos.items():
                spec = info.specs[sym]
                qty = floor_to_step(abs(q), spec.step)
                if qty < spec.min_qty:
                    print(f"  {sym}: dust {q} below min qty, skipped")
                    continue
                side = "SELL" if q > 0 else "BUY"
                seq += 1
                cid = f"rb-{sym}-{side}-f{seq % 10**6}"
                params = {"symbol": sym, "side": side, "quantity": fmt(qty), "reduceOnly": "true",
                          "newClientOrderId": cid}
                if args.taker:
                    params["type"] = "MARKET"
                else:
                    bt = await rest.public("GET", ep.BOOK_TICKER, {"symbol": sym})
                    px = Decimal(bt["askPrice"] if side == "SELL" else bt["bidPrice"])
                    params.update(type="LIMIT", timeInForce="GTX", price=fmt(px))
                try:
                    await rest.signed("POST", ep.ORDER, params)
                    print(f"  {sym}: {side} {qty} {params.get('price', 'MARKET')}")
                except ApiError as e:
                    print(f"  {sym}: order rejected {e.code} {e.msg}")
            await asyncio.sleep(args.requote_s)
            for o in await rest.signed("GET", ep.OPEN_ORDERS):  # re-place at the new touch
                if is_ours(str(o.get("clientOrderId", ""))) and o["symbol"] in pos:
                    try:
                        await rest.signed("DELETE", ep.ORDER, {"symbol": o["symbol"],
                                                               "origClientOrderId": o["clientOrderId"]})
                    except ApiError:
                        pass
            pos = await positions(rest)
        print("FLAT" if not pos else f"NOT FLAT after timeout: {pos}")
        return 0 if not pos else 1
    finally:
        await rest.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    add_env_args(ap)
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--taker", action="store_true")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--timeout-s", type=float, default=300)
    ap.add_argument("--requote-s", type=float, default=5)
    raise SystemExit(asyncio.run(run(ap.parse_args())))


if __name__ == "__main__":
    main()
