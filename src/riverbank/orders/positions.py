"""Position book per venue: signed qty, average entry, realized PnL, fees, funding, marks.

Paper venues derive everything from simulated fills. Real venues additionally adopt the
exchange's view (ACCOUNT_UPDATE / positionRisk) and the account equity.
"""

from __future__ import annotations

from decimal import Decimal

from riverbank.core.models import Fill, Position, Side

ZERO = Decimal(0)


class PositionBook:
    def __init__(self, initial_equity: float) -> None:
        self.initial_equity = initial_equity
        self.positions: dict[str, Position] = {}
        self.marks: dict[str, float] = {}
        self._equity_override: float | None = None
        self.wallet_balance: float | None = None

    def get(self, symbol: str) -> Position:
        p = self.positions.get(symbol)
        if p is None:
            p = self.positions[symbol] = Position(symbol)
        return p

    def qty(self, symbol: str) -> Decimal:
        p = self.positions.get(symbol)
        return p.qty if p else ZERO

    def set_mark(self, symbol: str, price: float) -> None:
        if price > 0:
            self.marks[symbol] = price

    def mark(self, symbol: str) -> float:
        return self.marks.get(symbol, 0.0)

    def apply_fill(self, fill: Fill) -> Decimal:
        """Update position; returns realized PnL of this fill (before fees)."""
        p = self.get(fill.symbol)
        signed = fill.qty if fill.side is Side.BUY else -fill.qty
        realized = ZERO
        old = p.qty
        new = old + signed
        if old == 0 or (old > 0) == (signed > 0):
            # opening / adding: weighted average entry
            total = abs(old) + abs(signed)
            p.entry_price = (p.entry_price * abs(old) + fill.price * abs(signed)) / total
        else:
            closed = min(abs(old), abs(signed))
            direction = Decimal(1) if old > 0 else Decimal(-1)
            realized = (fill.price - p.entry_price) * closed * direction
            if abs(signed) > abs(old):  # flipped through zero
                p.entry_price = fill.price
            elif new == 0:
                p.entry_price = ZERO
        p.qty = new
        p.realized_pnl += realized
        p.fees += fill.fee
        p.updated_ts = fill.ts
        return realized

    def apply_funding(self, symbol: str, amount: Decimal, ts: int) -> None:
        p = self.get(symbol)
        p.funding += amount
        p.updated_ts = ts

    def adopt(self, symbol: str, qty: Decimal, entry: Decimal, ts: int) -> None:
        """Take the exchange's position as truth (real venues)."""
        p = self.get(symbol)
        p.qty = qty
        p.entry_price = entry if qty != 0 else ZERO
        p.updated_ts = ts

    def set_equity(self, equity: float | None, wallet: float | None = None) -> None:
        self._equity_override = equity
        if wallet is not None:
            self.wallet_balance = wallet

    def unrealized(self, symbol: str) -> float:
        p = self.positions.get(symbol)
        m = self.marks.get(symbol, 0.0)
        if p is None or p.qty == 0 or m <= 0:
            return 0.0
        return float(p.qty) * (m - float(p.entry_price))

    def notional(self, symbol: str) -> float:
        """Signed position value at mark (USDT)."""
        return float(self.qty(symbol)) * self.marks.get(symbol, 0.0)

    def gross_exposure(self) -> float:
        return sum(abs(self.notional(s)) for s in self.positions)

    def equity(self) -> float:
        if self._equity_override is not None:
            return self._equity_override
        if self.wallet_balance is not None:  # real venue: exchange wallet + live unrealized PnL
            return self.wallet_balance + sum(self.unrealized(s) for s in self.positions)
        tot = self.initial_equity
        for s, p in self.positions.items():
            tot += float(p.realized_pnl - p.fees + p.funding) + self.unrealized(s)
        return tot

    def totals(self) -> dict[str, float]:
        out = {"realized": 0.0, "fees": 0.0, "funding": 0.0, "unrealized": 0.0}
        for s, p in self.positions.items():
            out["realized"] += float(p.realized_pnl)
            out["fees"] += float(p.fees)
            out["funding"] += float(p.funding)
            out["unrealized"] += self.unrealized(s)
        return out
