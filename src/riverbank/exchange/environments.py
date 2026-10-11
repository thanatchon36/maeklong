"""Base URLs per mode (verified against developers.binance.com, see docs/DISCREPANCIES.md).

Since 2026-04-23 the futures WebSocket is split into routed endpoints:
  /public  -> depth, bookTicker            (high-frequency public data)
  /market  -> aggTrade, markPrice, kline   (regular market data)
  /private -> user data (listenKey)
Unrouted legacy URLs no longer deliver /market or /private streams.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from riverbank.core.models import Mode


@dataclass(frozen=True, slots=True)
class Environment:
    name: str
    rest: str
    ws_root: str
    sends_orders: bool

    @property
    def ws_public(self) -> str:
        return f"{self.ws_root}/public"

    @property
    def ws_market(self) -> str:
        return f"{self.ws_root}/market"

    @property
    def ws_private(self) -> str:
        return f"{self.ws_root}/private"


PROD_REST = "https://fapi.binance.com"
PROD_WS = "wss://fstream.binance.com"
DEMO_REST = "https://demo-fapi.binance.com"
DEMO_WS = "wss://demo-fstream.binance.com"
# Spot/SAPI host, only used for the API-key permission check in live mode.
PROD_SAPI = "https://api.binance.com"

ENVIRONMENTS: dict[Mode, Environment] = {
    Mode.DEMO: Environment("demo", DEMO_REST, DEMO_WS, sends_orders=True),
    Mode.SHADOW: Environment("shadow", PROD_REST, PROD_WS, sends_orders=False),
    Mode.LIVE: Environment("live", PROD_REST, PROD_WS, sends_orders=True),
    Mode.REPLAY: Environment("replay", PROD_REST, PROD_WS, sends_orders=False),
}

PRODUCTION_HOSTS = frozenset({"fapi.binance.com", "api.binance.com", "fstream.binance.com"})
DEMO_HOSTS = frozenset({"demo-fapi.binance.com", "demo-fstream.binance.com"})


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def is_production(url: str) -> bool:
    h = host_of(url)
    return h in PRODUCTION_HOSTS or (h.endswith(".binance.com") and h not in DEMO_HOSTS)
