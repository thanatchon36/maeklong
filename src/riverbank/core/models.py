"""Core typed data model (SPEC §7).

Prices/quantities are `Decimal` wherever they cross the exchange boundary
(orders, fills, specs). Feature values are floats.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum


class Mode(StrEnum):
    DEMO = "demo"
    SHADOW = "shadow"
    LIVE = "live"
    REPLAY = "replay"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY


class BookStatus(StrEnum):
    OK = "OK"
    STALE = "STALE"
    INVALID = "INVALID"


class OrderStatus(StrEnum):
    PENDING_NEW = "PENDING_NEW"
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    PENDING_CANCEL = "PENDING_CANCEL"
    UNKNOWN = "UNKNOWN"

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_STATUSES


TERMINAL_STATUSES = frozenset(
    {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED}
)


class RiskState(StrEnum):
    OK = "OK"
    REDUCE_ONLY = "REDUCE_ONLY"
    HALTED = "HALTED"


class UniverseState(StrEnum):
    CANDIDATE = "CANDIDATE"
    ACTIVE = "ACTIVE"
    DEMOTED = "DEMOTED"


@dataclass(frozen=True, slots=True)
class SymbolSpec:
    symbol: str
    tick: Decimal
    step: Decimal
    min_qty: Decimal
    min_notional: Decimal
    price_precision: int
    qty_precision: int
    # Extra exchangeInfo fields used by the screener / order checks.
    max_qty: Decimal = Decimal(0)
    min_price: Decimal = Decimal(0)
    max_price: Decimal = Decimal(0)
    status: str = "TRADING"
    contract_type: str = "PERPETUAL"
    quote_asset: str = "USDT"
    base_asset: str = ""
    underlying_type: str = "COIN"
    onboard_date_ms: int = 0
    max_num_orders: int = 0
    percent_up: Decimal = Decimal(0)
    percent_down: Decimal = Decimal(0)
    position_control_side: str = "NONE"
    time_in_force: tuple[str, ...] = ()


@dataclass(slots=True)
class BookState:
    symbol: str
    bids: list[tuple[float, float]]
    asks: list[tuple[float, float]]
    last_update_id: int
    ts: int
    status: BookStatus


@dataclass(frozen=True, slots=True)
class Features:
    symbol: str
    ts: int
    mid: float
    micro: float
    imbalance: float
    sigma_bps: float  # bps per sqrt(second)
    flow: float
    fair_value: float
    best_bid: float
    best_ask: float
    sigma_ready: bool = True


@dataclass(frozen=True, slots=True)
class QuoteIntent:
    symbol: str
    side: Side
    price: Decimal
    qty: Decimal
    client_id: str
    reason: str
    reduce_only: bool = False
    time_in_force: str = "GTX"
    order_type: str = "LIMIT"
    venue: str = "paper"
    level: int = 0


@dataclass(slots=True)
class Order:
    client_id: str
    exchange_id: int | None
    symbol: str
    side: Side
    price: Decimal
    qty: Decimal
    filled_qty: Decimal
    status: OrderStatus
    ts_created: int
    ts_updated: int
    reduce_only: bool = False
    time_in_force: str = "GTX"
    order_type: str = "LIMIT"
    reason: str = ""
    level: int = 0
    avg_fill_price: Decimal = Decimal(0)
    reject_reason: str = ""

    @property
    def remaining(self) -> Decimal:
        return self.qty - self.filled_qty

    @property
    def is_open(self) -> bool:
        """Could still trade (counts toward exposure)."""
        return not self.status.is_terminal and self.status is not OrderStatus.REJECTED


@dataclass(frozen=True, slots=True)
class Fill:
    ts: int
    symbol: str
    side: Side
    price: Decimal
    qty: Decimal
    fee: Decimal  # positive = paid, in quote asset
    is_maker: bool
    mode: str
    client_id: str
    mid_at_fill: float
    trade_id: str = ""
    reason: str = ""


@dataclass(frozen=True, slots=True)
class RiskEvent:
    ts: int
    level: str
    rule: str
    detail: str
    state_before: RiskState
    state_after: RiskState


@dataclass(slots=True)
class Position:
    symbol: str
    qty: Decimal = Decimal(0)  # signed, base units
    entry_price: Decimal = Decimal(0)
    realized_pnl: Decimal = Decimal(0)
    fees: Decimal = Decimal(0)
    funding: Decimal = Decimal(0)
    liquidation_price: Decimal = Decimal(0)
    updated_ts: int = 0
    extra: dict[str, str] = field(default_factory=dict)
