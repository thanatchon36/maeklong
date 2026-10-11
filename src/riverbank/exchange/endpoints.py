"""Endpoint paths and request weights (developers.binance.com, verified 2026-10-12; DISCREPANCIES #6)."""

from __future__ import annotations

TIME = "/fapi/v1/time"
EXCHANGE_INFO = "/fapi/v1/exchangeInfo"
DEPTH = "/fapi/v1/depth"
TICKER_24HR = "/fapi/v1/ticker/24hr"
BOOK_TICKER = "/fapi/v1/ticker/bookTicker"
PREMIUM_INDEX = "/fapi/v1/premiumIndex"
KLINES = "/fapi/v1/klines"
ORDER = "/fapi/v1/order"
ALL_OPEN_ORDERS = "/fapi/v1/allOpenOrders"
OPEN_ORDERS = "/fapi/v1/openOrders"
COUNTDOWN_CANCEL_ALL = "/fapi/v1/countdownCancelAll"
POSITION_RISK = "/fapi/v3/positionRisk"
ACCOUNT = "/fapi/v3/account"
BALANCE = "/fapi/v3/balance"
COMMISSION_RATE = "/fapi/v1/commissionRate"
LEVERAGE = "/fapi/v1/leverage"
MARGIN_TYPE = "/fapi/v1/marginType"
POSITION_MODE = "/fapi/v1/positionSide/dual"
LISTEN_KEY = "/fapi/v1/listenKey"
SAPI_API_RESTRICTIONS = "/sapi/v1/account/apiRestrictions"  # on api.binance.com


def depth_weight(limit: int) -> int:
    if limit <= 50:
        return 2
    if limit <= 100:
        return 5
    if limit <= 500:
        return 10
    return 20


def klines_weight(limit: int) -> int:
    if limit < 100:
        return 1
    if limit < 500:
        return 2
    if limit <= 1000:
        return 5
    return 10


# (method, path) -> IP weight; per-symbol variants handled by `weight()`.
_FIXED: dict[tuple[str, str], int] = {
    ("GET", TIME): 1,
    ("GET", EXCHANGE_INFO): 1,
    ("POST", ORDER): 0,  # orders count on ORDERS limits, 0 on IP weight
    ("PUT", ORDER): 0,
    ("DELETE", ORDER): 1,
    ("GET", ORDER): 1,
    ("DELETE", ALL_OPEN_ORDERS): 1,
    ("POST", COUNTDOWN_CANCEL_ALL): 10,
    ("GET", POSITION_RISK): 5,
    ("GET", ACCOUNT): 5,
    ("GET", BALANCE): 5,
    ("GET", COMMISSION_RATE): 20,
    ("POST", LEVERAGE): 1,
    ("POST", MARGIN_TYPE): 1,
    ("GET", POSITION_MODE): 30,
    ("POST", POSITION_MODE): 1,
    ("POST", LISTEN_KEY): 1,
    ("PUT", LISTEN_KEY): 1,
    ("DELETE", LISTEN_KEY): 1,
}

ORDER_COUNTING = frozenset({("POST", ORDER), ("PUT", ORDER)})


def weight(method: str, path: str, params: dict[str, object] | None = None) -> int:
    p = params or {}
    has_symbol = "symbol" in p
    if path == DEPTH:
        return depth_weight(int(p.get("limit", 500)))  # type: ignore[call-overload]
    if path == KLINES:
        return klines_weight(int(p.get("limit", 500)))  # type: ignore[call-overload]
    if path == TICKER_24HR:
        return 1 if has_symbol else 40
    if path == BOOK_TICKER:
        return 2 if has_symbol else 5
    if path == PREMIUM_INDEX:
        return 1 if has_symbol else 10
    if path == OPEN_ORDERS:
        return 1 if has_symbol else 40
    return _FIXED.get((method, path), 1)
