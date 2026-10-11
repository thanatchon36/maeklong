"""Async REST client for Binance USDⓈ-M futures.

Safety: signed requests to a production host are refused unless live signing was
explicitly unlocked by the live safety gate (`unlock_live_signing`), which itself
re-checks MODE=live and LIVE_CONFIRM. Shadow/replay never construct credentials.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlencode

import aiohttp
import orjson

from riverbank.core.config import LIVE_CONFIRM_PHRASE, SafetyError
from riverbank.exchange.environments import is_production
from riverbank.exchange.signing import Credentials, signed_query

UNKNOWN_503_MSG = "Unknown error, please check your request or try again later."
SERVICE_UNAVAILABLE_MSG = "Service Unavailable."
INTERNAL_ERROR_MSG = "Internal error; unable to process your request. Please try again."

_live_signing_unlocked = False


def unlock_live_signing(env: Mapping[str, str] | None = None) -> None:
    """Called only by main.py after the live gate passed. Re-validates env."""
    global _live_signing_unlocked
    env = os.environ if env is None else env
    if env.get("MODE") != "live" or env.get("LIVE_CONFIRM") != LIVE_CONFIRM_PHRASE:
        raise SafetyError("refusing to unlock live signing: MODE/LIVE_CONFIRM not set")
    _live_signing_unlocked = True


def lock_live_signing() -> None:
    global _live_signing_unlocked
    _live_signing_unlocked = False


def assert_signed_allowed(base_url: str) -> None:
    if is_production(base_url) and not _live_signing_unlocked:
        raise SafetyError(f"signed request to production host {base_url} refused (not in gated live mode)")


class BinanceError(Exception):
    def __init__(self, status: int, code: int | None, msg: str, headers: Mapping[str, str] | None = None):
        super().__init__(f"HTTP {status} code={code} msg={msg}")
        self.status = status
        self.code = code
        self.msg = msg
        self.headers = dict(headers or {})

    @property
    def retry_after_s(self) -> float | None:
        v = self.headers.get("Retry-After") or self.headers.get("retry-after")
        try:
            return float(v) if v is not None else None
        except ValueError:
            return None


class UnknownExecutionError(BinanceError):
    """HTTP 503 'Unknown error' or a client timeout: the request may have executed."""


class RetryableError(BinanceError):
    """Definite failure that may be retried with backoff (503 Service Unavailable / internal)."""


class ThrottledError(BinanceError):
    """-1008 system-level throttle: definite failure; back off and reduce concurrency."""


class RateLimitError(BinanceError):
    """HTTP 429 — must stop sending."""


class IpBanError(BinanceError):
    """HTTP 418 — IP banned."""


class ApiError(BinanceError):
    """Any other API rejection (4xx with a Binance code)."""


def classify(status: int, body: bytes, headers: Mapping[str, str]) -> BinanceError:
    code: int | None = None
    msg = ""
    try:
        data = orjson.loads(body) if body else {}
        if isinstance(data, dict):
            code = data.get("code")
            msg = str(data.get("msg", ""))
    except orjson.JSONDecodeError:
        msg = body[:200].decode(errors="replace")
    if status == 429:
        return RateLimitError(status, code, msg, headers)
    if status == 418:
        return IpBanError(status, code, msg, headers)
    if code == -1008:
        return ThrottledError(status, code, msg, headers)
    if status == 503:
        if "Unknown error" in msg:
            return UnknownExecutionError(status, code, msg, headers)
        return RetryableError(status, code, msg, headers)
    if status == 408:
        return UnknownExecutionError(status, code, msg or "timeout", headers)
    if status >= 500:
        if "unknown error" in msg.lower():
            return UnknownExecutionError(status, code, msg, headers)
        return RetryableError(status, code, msg, headers)
    return ApiError(status, code, msg, headers)


HeaderHook = Callable[[Mapping[str, str]], None]


class RestClient:
    def __init__(
        self,
        base_url: str,
        credentials: Credentials | None = None,
        *,
        session: aiohttp.ClientSession | None = None,
        timeout_s: float = 10.0,
        recv_window_ms: int = 5000,
        server_time_ms: Callable[[], int] | None = None,
        header_hook: HeaderHook | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._creds = credentials
        self._session = session
        self._own_session = session is None
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._recv_window = recv_window_ms
        self._server_time_ms = server_time_ms
        self.header_hook = header_hook
        if credentials is not None:
            # Fail fast at construction too, not only per request.
            assert_signed_allowed(self.base_url)

    @property
    def has_credentials(self) -> bool:
        return self._creds is not None

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
            self._own_session = True
        return self._session

    async def close(self) -> None:
        if self._own_session and self._session is not None and not self._session.closed:
            await self._session.close()

    async def public(self, method: str, path: str, params: Mapping[str, Any] | None = None) -> Any:
        query = urlencode([(k, v) for k, v in (params or {}).items() if v is not None])
        return await self._send(method, path, query, signed=False, api_key=False)

    async def keyed(self, method: str, path: str, params: Mapping[str, Any] | None = None) -> Any:
        """USER_STREAM endpoints: API key header, no signature."""
        if self._creds is None:
            raise SafetyError("no credentials configured")
        assert_signed_allowed(self.base_url)
        query = urlencode([(k, v) for k, v in (params or {}).items() if v is not None])
        return await self._send(method, path, query, signed=False, api_key=True)

    async def signed(self, method: str, path: str, params: Mapping[str, Any] | None = None) -> Any:
        if self._creds is None:
            raise SafetyError("no credentials configured")
        assert_signed_allowed(self.base_url)
        p: dict[str, Any] = dict(params or {})
        p["recvWindow"] = self._recv_window
        p["timestamp"] = self._server_time_ms() if self._server_time_ms else _now_ms()
        query = signed_query(p, self._creds.signer)
        return await self._send(method, path, query, signed=True, api_key=True)

    async def _send(self, method: str, path: str, query: str, *, signed: bool, api_key: bool) -> Any:
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{query}"
        headers = {}
        if api_key and self._creds is not None:
            headers["X-MBX-APIKEY"] = self._creds.api_key
        sess = await self._sess()
        try:
            async with sess.request(method, url, headers=headers, timeout=self._timeout) as resp:
                body = await resp.read()
                hdrs = {k: v for k, v in resp.headers.items()}
                if self.header_hook is not None:
                    self.header_hook(hdrs)
                if resp.status == 200:
                    return orjson.loads(body) if body else None
                raise classify(resp.status, body, hdrs)
        except TimeoutError as e:
            # A timed-out mutating request may have executed on the exchange.
            if method != "GET":
                raise UnknownExecutionError(0, None, f"client timeout: {e!r}") from e
            raise RetryableError(0, None, f"client timeout: {e!r}") from e
        except aiohttp.ClientConnectionError as e:
            if method != "GET":
                raise UnknownExecutionError(0, None, f"connection error: {e!r}") from e
            raise RetryableError(0, None, f"connection error: {e!r}") from e


def _now_ms() -> int:
    import time

    return time.time_ns() // 1_000_000
