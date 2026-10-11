"""Order state machine (SPEC §5.5).

PENDING_NEW → NEW → PARTIALLY_FILLED → FILLED | CANCELED | REJECTED | EXPIRED,
plus PENDING_CANCEL and UNKNOWN. The user data stream is the source of truth; REST
responses are hints and may arrive after (or before) stream updates, so transitions that
would move an order backwards are ignored instead of applied.
"""

from __future__ import annotations

from riverbank.core.models import OrderStatus as S

ALLOWED: dict[S, frozenset[S]] = {
    S.PENDING_NEW: frozenset({S.NEW, S.PARTIALLY_FILLED, S.FILLED, S.CANCELED, S.REJECTED, S.EXPIRED, S.UNKNOWN,
                              S.PENDING_CANCEL}),
    S.NEW: frozenset({S.PARTIALLY_FILLED, S.FILLED, S.CANCELED, S.EXPIRED, S.PENDING_CANCEL, S.UNKNOWN}),
    S.PARTIALLY_FILLED: frozenset({S.PARTIALLY_FILLED, S.FILLED, S.CANCELED, S.EXPIRED, S.PENDING_CANCEL,
                                   S.UNKNOWN}),
    # A fill can race a cancel; a rejected cancel returns the order to its working state.
    S.PENDING_CANCEL: frozenset({S.CANCELED, S.FILLED, S.EXPIRED, S.UNKNOWN, S.NEW, S.PARTIALLY_FILLED}),
    S.UNKNOWN: frozenset({S.NEW, S.PARTIALLY_FILLED, S.FILLED, S.CANCELED, S.REJECTED, S.EXPIRED,
                          S.PENDING_CANCEL}),
    S.FILLED: frozenset(),
    S.CANCELED: frozenset(),
    S.REJECTED: frozenset(),
    S.EXPIRED: frozenset(),
}


def can_transition(old: S, new: S) -> bool:
    return (old == new and old in (S.PARTIALLY_FILLED, S.PENDING_CANCEL)) or new in ALLOWED[old]


def exchange_status(raw: str) -> S:
    """Map Binance order status strings to our states."""
    if raw == "EXPIRED_IN_MATCH":
        return S.EXPIRED
    return S(raw)
