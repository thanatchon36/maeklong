"""In-process fake of the Binance USDⓈ-M endpoints used by the real venue (tests only).

Implements order placement/cancel/query, open orders, positions, account, countdown
cancel-all, listenKey + private user-data WebSocket, account setup endpoints, and
fault injection (scripted HTTP errors, dropped streams, silent position changes).
"""

from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "exchange_info_sample.json"
UNKNOWN_503 = {"code": -1000, "msg": "Unknown error, please check your request or try again later."}


def now_ms() -> int:
    return time.time_ns() // 1_000_000


class FakeBinance:
    def __init__(self) -> None:
        self.orders: dict[str, dict[str, Any]] = {}
        self.positions: dict[str, Decimal] = {}
        self.entry: dict[str, Decimal] = {}
        self.wallet = Decimal("1000")
        self.books: dict[str, tuple[Decimal, Decimal]] = {}
        self.countdowns: dict[str, int] = {}
        self.listen_keys: set[str] = set()
        self.ws_clients: list[web.WebSocketResponse] = []
        self.dual = False
        self.multi_assets = False
        self.margin: dict[str, str] = {}
        self.leverage: dict[str, int] = {}
        self.maker_rate = "0.0002"
        self.indicators: dict[str, Any] = {}
        self.faults: list[tuple[str, int, dict[str, Any], bool]] = []  # (route, status, body, still_execute)
        self.requests: list[str] = []
        self._oid = 1000
        self._tid = 1
        self.runner: web.AppRunner | None = None
        self.port = 0
        self._cd_task: asyncio.Task[None] | None = None
        self.countdown_cancels = 0

    # ---- lifecycle ----

    async def start(self) -> str:
        app = web.Application()
        r = app.router
        r.add_get("/fapi/v1/time", self.h_time)
        r.add_get("/fapi/v1/exchangeInfo", self.h_info)
        r.add_route("*", "/fapi/v1/order", self.h_order)
        r.add_get("/fapi/v1/openOrders", self.h_open_orders)
        r.add_delete("/fapi/v1/allOpenOrders", self.h_cancel_all)
        r.add_post("/fapi/v1/countdownCancelAll", self.h_countdown)
        r.add_get("/fapi/v3/positionRisk", self.h_position_risk)
        r.add_get("/fapi/v3/account", self.h_account)
        r.add_route("*", "/fapi/v1/listenKey", self.h_listen_key)
        r.add_route("*", "/fapi/v1/positionSide/dual", self.h_dual)
        r.add_get("/fapi/v1/multiAssetsMargin", self.h_multi)
        r.add_post("/fapi/v1/marginType", self.h_margin)
        r.add_post("/fapi/v1/leverage", self.h_leverage)
        r.add_get("/fapi/v1/commissionRate", self.h_commission)
        r.add_get("/fapi/v1/apiTradingStatus", self.h_trading_status)
        r.add_get("/fapi/v1/ticker/bookTicker", self.h_book_ticker)
        r.add_get("/private/ws/{key}", self.h_ws)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        self._cd_task = asyncio.create_task(self._countdown_loop())
        return f"http://127.0.0.1:{self.port}"

    async def stop(self) -> None:
        if self._cd_task:
            self._cd_task.cancel()
        for ws in list(self.ws_clients):
            await ws.close()
        if self.runner:
            await self.runner.cleanup()

    # ---- helpers ----

    def _j(self, req: web.Request, data: Any, status: int = 200) -> web.Response:
        return web.json_response(data, status=status, headers={"X-MBX-USED-WEIGHT-1M": "10"})

    def _err(self, code: int, msg: str, status: int = 400) -> web.Response:
        return web.json_response({"code": code, "msg": msg}, status=status)

    def inject(self, route: str, status: int, body: dict[str, Any], still_execute: bool = False) -> None:
        """Next request on `route` (e.g. 'POST /fapi/v1/order') gets this error response."""
        self.faults.append((route, status, body, still_execute))

    def _fault(self, route: str) -> tuple[int, dict[str, Any], bool] | None:
        for i, (r, st, body, ex) in enumerate(self.faults):
            if r == route:
                del self.faults[i]
                return st, body, ex
        return None

    def _params(self, req: web.Request, body: str) -> dict[str, str]:
        from urllib.parse import parse_qsl

        p = dict(parse_qsl(req.query_string))
        p.update(dict(parse_qsl(body)))
        return p

    async def _push(self, payload: dict[str, Any]) -> None:
        msg = json.dumps(payload)
        for ws in list(self.ws_clients):
            try:
                await ws.send_str(msg)
            except Exception:
                pass

    def _otu(self, o: dict[str, Any], x: str, last_qty: str = "0", last_px: str = "0", fee: str = "0",
             tid: int = 0) -> dict[str, Any]:
        t = now_ms()
        return {"e": "ORDER_TRADE_UPDATE", "E": t, "T": t, "o": {
            "s": o["symbol"], "c": o["clientOrderId"], "S": o["side"], "o": o["type"], "f": o["timeInForce"],
            "q": o["origQty"], "p": o["price"], "ap": "0", "sp": "0", "x": x, "X": o["status"], "i": o["orderId"],
            "l": last_qty, "z": o["executedQty"], "L": last_px, "N": "USDT", "n": fee, "T": t, "t": tid,
            "b": "0", "a": "0", "m": True, "R": o["reduceOnly"], "wt": "CONTRACT_PRICE", "ot": o["type"],
            "ps": "BOTH", "cp": False, "rp": "0", "pP": False, "si": 0, "ss": 0, "V": "EXPIRE_MAKER", "pm": "NONE",
            "gtd": 0, "er": "0"}}

    # ---- test controls ----

    def set_book(self, symbol: str, bid: str, ask: str) -> None:
        self.books[symbol] = (Decimal(bid), Decimal(ask))

    def open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        return [o for o in self.orders.values() if o["status"] in ("NEW", "PARTIALLY_FILLED")
                and (symbol is None or o["symbol"] == symbol)]

    async def fill(self, client_id: str, qty: str | None = None) -> None:
        o = self.orders[client_id]
        q = Decimal(qty) if qty else Decimal(o["origQty"]) - Decimal(o["executedQty"])
        o["executedQty"] = str(Decimal(o["executedQty"]) + q)
        o["status"] = "FILLED" if Decimal(o["executedQty"]) >= Decimal(o["origQty"]) else "PARTIALLY_FILLED"
        o["updateTime"] = now_ms()
        px = Decimal(o["price"])
        signed = q if o["side"] == "BUY" else -q
        old = self.positions.get(o["symbol"], Decimal(0))
        new = old + signed
        if old == 0 or (old > 0) == (signed > 0):
            self.entry[o["symbol"]] = ((self.entry.get(o["symbol"], Decimal(0)) * abs(old) + px * abs(signed))
                                       / abs(new))
        self.positions[o["symbol"]] = new
        fee = (px * q * Decimal(self.maker_rate)).quantize(Decimal("0.00000001"))
        self.wallet -= fee
        self._tid += 1
        await self._push(self._otu(o, "TRADE", str(q), str(px), str(fee), self._tid))
        t = now_ms()
        await self._push({"e": "ACCOUNT_UPDATE", "E": t, "T": t, "a": {"m": "ORDER", "B": [
            {"a": "USDT", "wb": str(self.wallet), "cw": str(self.wallet), "bc": "0"}], "P": [
            {"s": o["symbol"], "pa": str(new), "ep": str(self.entry.get(o["symbol"], 0)), "bep": "0", "cr": "0",
             "up": "0", "mt": "isolated", "iw": "0", "ps": "BOTH"}]}})

    async def drop_user_streams(self) -> None:
        for ws in list(self.ws_clients):
            await ws.close()
        self.ws_clients.clear()

    def add_order(self, symbol: str, side: str, price: str, qty: str, client_id: str) -> dict[str, Any]:
        self._oid += 1
        o = {"symbol": symbol, "side": side, "type": "LIMIT", "price": price, "origQty": qty, "executedQty": "0",
             "status": "NEW", "clientOrderId": client_id, "orderId": self._oid, "reduceOnly": False,
             "timeInForce": "GTX", "time": now_ms() - 60_000, "updateTime": now_ms() - 60_000}
        self.orders[client_id] = o
        return o

    # ---- handlers ----

    async def h_book_ticker(self, req: web.Request) -> web.Response:
        sym = req.query.get("symbol", "")
        bid, ask = self.books.get(sym, (Decimal("1"), Decimal("1.001")))
        return self._j(req, {"symbol": sym, "bidPrice": str(bid), "bidQty": "100", "askPrice": str(ask),
                             "askQty": "100"})

    async def h_time(self, req: web.Request) -> web.Response:
        return web.json_response({"serverTime": now_ms()})

    async def h_info(self, req: web.Request) -> web.Response:
        return web.json_response(json.loads(FIX.read_text()))

    async def h_multi(self, req: web.Request) -> web.Response:
        return self._j(req, {"multiAssetsMargin": self.multi_assets})

    async def h_commission(self, req: web.Request) -> web.Response:
        return self._j(req, {"symbol": req.query.get("symbol"), "makerCommissionRate": self.maker_rate,
                             "takerCommissionRate": "0.0005"})

    async def h_trading_status(self, req: web.Request) -> web.Response:
        return self._j(req, {"indicators": self.indicators, "updateTime": now_ms()})

    async def h_order(self, req: web.Request) -> web.Response:
        body = await req.text()
        p = self._params(req, body)
        route = f"{req.method} /fapi/v1/order"
        self.requests.append(route)
        if "signature" not in p or "X-MBX-APIKEY" not in req.headers:
            return self._err(-1022, "Signature for this request is not valid.")
        f = self._fault(route)
        if f is not None and not f[2]:
            st, b, _ = f
            hdr = {"Retry-After": "61"} if st == 429 else {}
            return web.json_response(b, status=st, headers=hdr)
        if req.method == "POST":
            resp = await self._place(p)
        elif req.method == "DELETE":
            resp = await self._cancel(p)
        else:
            o = self.orders.get(p.get("origClientOrderId", ""))
            resp = self._j(req, o) if o else self._err(-2013, "Order does not exist.")
        if f is not None:  # executed, but the client gets the injected error (e.g. 503 unknown)
            return web.json_response(f[1], status=f[0])
        return resp

    async def _place(self, p: dict[str, str]) -> web.Response:
        sym, side = p["symbol"], p["side"]
        cid = p.get("newClientOrderId", f"auto{self._oid}")
        if any(o["clientOrderId"] == cid and o["status"] in ("NEW", "PARTIALLY_FILLED")
               for o in self.orders.values()):
            return self._err(-4116, "ClientOrderId is duplicated.")
        px = Decimal(p.get("price", "0"))
        if p.get("timeInForce") == "GTX" and sym in self.books:
            bid, ask = self.books[sym]
            if (side == "BUY" and px >= ask) or (side == "SELL" and px <= bid):
                return self._err(-5022, "Due to the order could not be executed as maker, the Post Only order "
                                        "will be rejected.")
        self._oid += 1
        o = {"symbol": sym, "side": side, "type": p["type"], "price": p.get("price", "0"),
             "origQty": p["quantity"], "executedQty": "0", "status": "NEW", "clientOrderId": cid,
             "orderId": self._oid, "reduceOnly": p.get("reduceOnly") == "true",
             "timeInForce": p.get("timeInForce", "GTC"), "time": now_ms(), "updateTime": now_ms()}
        self.orders[cid] = o
        await self._push(self._otu(o, "NEW"))
        if p["type"] == "MARKET":
            bid, ask = self.books.get(sym, (Decimal("1"), Decimal("1")))
            o["price"] = str(ask if side == "BUY" else bid)
            await self.fill(cid)
        return web.json_response({"orderId": o["orderId"], "clientOrderId": cid, "status": "NEW", "symbol": sym},
                                 headers={"X-MBX-ORDER-COUNT-10S": "1", "X-MBX-ORDER-COUNT-1M": "1"})

    async def _cancel(self, p: dict[str, str]) -> web.Response:
        o = self.orders.get(p.get("origClientOrderId", ""))
        if o is None or o["status"] not in ("NEW", "PARTIALLY_FILLED"):
            return self._err(-2011, "Unknown order sent.")
        o["status"] = "CANCELED"
        o["updateTime"] = now_ms()
        await self._push(self._otu(o, "CANCELED"))
        return web.json_response(dict(o))

    async def h_open_orders(self, req: web.Request) -> web.Response:
        self.requests.append("GET /fapi/v1/openOrders")
        return self._j(req, self.open_orders(req.query.get("symbol")))

    async def h_cancel_all(self, req: web.Request) -> web.Response:
        p = self._params(req, await req.text())
        for o in self.open_orders(p.get("symbol")):
            o["status"] = "CANCELED"
            await self._push(self._otu(o, "CANCELED"))
        return self._j(req, {"code": 200, "msg": "The operation of cancel all open order is done."})

    async def h_countdown(self, req: web.Request) -> web.Response:
        p = self._params(req, await req.text())
        ms = int(p["countdownTime"])
        if ms == 0:
            self.countdowns.pop(p["symbol"], None)
        else:
            self.countdowns[p["symbol"]] = now_ms() + ms
        return self._j(req, {"symbol": p["symbol"], "countdownTime": str(ms)})

    async def _countdown_loop(self) -> None:
        while True:
            await asyncio.sleep(0.05)
            t = now_ms()
            for sym, exp in list(self.countdowns.items()):
                if t >= exp:
                    del self.countdowns[sym]
                    for o in self.open_orders(sym):
                        o["status"] = "CANCELED"
                        self.countdown_cancels += 1
                        await self._push(self._otu(o, "CANCELED"))

    async def h_position_risk(self, req: web.Request) -> web.Response:
        out = []
        for sym, q in self.positions.items():
            if q != 0:
                e = self.entry.get(sym, Decimal(0))
                out.append({"symbol": sym, "positionSide": "BOTH", "positionAmt": str(q), "entryPrice": str(e),
                            "liquidationPrice": str(e * Decimal("0.5") if q > 0 else e * Decimal("1.5")),
                            "marginType": "isolated"})
        return self._j(req, out)

    async def h_account(self, req: web.Request) -> web.Response:
        w = str(self.wallet)
        return self._j(req, {"totalWalletBalance": w, "totalMarginBalance": w,
                             "assets": [{"asset": "USDT", "walletBalance": w, "marginBalance": w}]})

    async def h_listen_key(self, req: web.Request) -> web.Response:
        if "X-MBX-APIKEY" not in req.headers:
            return self._err(-2015, "Invalid API-key")
        if req.method == "POST":
            key = f"key{len(self.listen_keys) + 1}"
            self.listen_keys.add(key)
            return self._j(req, {"listenKey": key})
        return self._j(req, {})

    async def h_dual(self, req: web.Request) -> web.Response:
        if req.method == "GET":
            return self._j(req, {"dualSidePosition": self.dual})
        p = self._params(req, await req.text())
        want = p.get("dualSidePosition") == "true"
        if want == self.dual:
            return self._err(-4059, "No need to change position side.")
        self.dual = want
        return self._j(req, {"code": 200, "msg": "success"})

    async def h_margin(self, req: web.Request) -> web.Response:
        p = self._params(req, await req.text())
        if self.margin.get(p["symbol"]) == p["marginType"]:
            return self._err(-4046, "No need to change margin type.")
        self.margin[p["symbol"]] = p["marginType"]
        return self._j(req, {"code": 200, "msg": "success"})

    async def h_leverage(self, req: web.Request) -> web.Response:
        p = self._params(req, await req.text())
        self.leverage[p["symbol"]] = int(p["leverage"])
        return self._j(req, {"symbol": p["symbol"], "leverage": int(p["leverage"])})

    async def h_ws(self, req: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(req)
        if req.match_info["key"] not in self.listen_keys:
            await ws.close()
            return ws
        self.ws_clients.append(ws)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.CLOSE:
                    break
        finally:
            if ws in self.ws_clients:
                self.ws_clients.remove(ws)
        return ws
