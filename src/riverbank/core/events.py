"""Typed in-process events. Every timestamp is epoch milliseconds.

`recv_ts` is the local receive time; replay orders events by it so the engine
sees exactly what it saw live.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

Levels = list[tuple[float, float]]


@dataclass(slots=True)
class DepthDiff:
    symbol: str
    E: int
    T: int
    U: int
    u: int
    pu: int
    bids: Levels
    asks: Levels
    recv_ts: int


@dataclass(slots=True)
class DepthSnapshot:
    symbol: str
    last_update_id: int
    bids: Levels
    asks: Levels
    recv_ts: int
    purpose: str = "sync"  # sync | check | refresh


@dataclass(slots=True)
class BookTopN:
    """Recorded top-N book state (replay input)."""

    symbol: str
    last_update_id: int
    bids: Levels
    asks: Levels
    recv_ts: int


@dataclass(slots=True)
class BookTicker:
    symbol: str
    u: int
    E: int
    T: int
    bid_px: float
    bid_qty: float
    ask_px: float
    ask_qty: float
    recv_ts: int


@dataclass(slots=True)
class AggTrade:
    symbol: str
    a: int
    price: float
    qty: float
    nq: float  # qty excluding RPI fills (== qty when the field is absent)
    T: int
    E: int
    buyer_maker: bool  # True => aggressor was the seller
    recv_ts: int


@dataclass(slots=True)
class MarkPrice:
    symbol: str
    E: int
    mark: float
    index: float
    funding_rate: float
    next_funding_ms: int
    recv_ts: int


@dataclass(slots=True)
class StreamStatus:
    route: str
    shard: int
    connected: bool
    symbols: tuple[str, ...]
    recv_ts: int
    detail: str = ""


@dataclass(slots=True)
class Timer:
    recv_ts: int


# ---- user data / execution events (normalized, venue-agnostic) ----


@dataclass(slots=True)
class OrderUpdate:
    """Normalized ORDER_TRADE_UPDATE (or its paper-exchange equivalent)."""

    symbol: str
    client_id: str
    exchange_id: int | None
    side: str
    exec_type: str  # NEW | TRADE | CANCELED | EXPIRED | AMENDMENT | CALCULATED | REJECTED
    status: str  # NEW | PARTIALLY_FILLED | FILLED | CANCELED | EXPIRED | EXPIRED_IN_MATCH | REJECTED
    price: Decimal
    qty: Decimal
    filled_qty: Decimal  # cumulative
    last_fill_qty: Decimal
    last_fill_price: Decimal
    fee: Decimal  # commission of this fill, quote asset
    fee_asset: str
    is_maker: bool
    trade_id: int
    T: int
    E: int
    recv_ts: int
    venue: str
    reduce_only: bool = False
    expire_reason: str = ""
    realized_pnl: Decimal = Decimal(0)


@dataclass(slots=True)
class PositionUpdate:
    symbol: str
    qty: Decimal
    entry_price: Decimal
    unrealized: Decimal = Decimal(0)
    margin_type: str = ""


@dataclass(slots=True)
class AccountUpdate:
    reason: str
    balances: dict[str, Decimal]  # asset -> wallet balance
    positions: list[PositionUpdate]
    E: int
    T: int
    recv_ts: int
    venue: str


@dataclass(slots=True)
class UserStreamStatus:
    connected: bool
    recv_ts: int
    venue: str
    detail: str = ""


@dataclass(slots=True)
class OrderAck:
    """REST outcome hint for a place/cancel command (the stream remains the source of truth)."""

    client_id: str
    symbol: str
    action: str  # place | cancel
    outcome: str  # ok | rejected | unknown | failed | not_found
    recv_ts: int
    venue: str
    exchange_id: int | None = None
    status: str = ""
    code: int | None = None
    msg: str = ""
    raw: dict[str, object] = field(default_factory=dict)
