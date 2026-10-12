"""Structured JSON logging (structlog) with per-component levels and secret redaction."""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable, Mapping, MutableMapping
from typing import Any, TextIO

import structlog

_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
_SECRET_ENV = ("BINANCE_API_KEY", "BINANCE_API_SECRET", "BINANCE_PRIVATE_KEY_PASSWORD", "TELEGRAM_BOT_TOKEN")
_SECRET_KEYS = {"api_key", "apikey", "secret", "signature", "token", "password", "x-mbx-apikey", "listenkey"}

_component_levels: dict[str, int] = {}
_default_level = 20
_error_hooks: list[Callable[[str], None]] = []


def add_error_hook(fn: Callable[[str], None]) -> None:
    """`fn(event)` is called for every ERROR/CRITICAL log line (e.g. the rb_errors_total metric)."""
    _error_hooks.append(fn)


def remove_error_hook(fn: Callable[[str], None]) -> None:
    if fn in _error_hooks:
        _error_hooks.remove(fn)


def _count_errors(_: Any, __: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    if event_dict.get("level") in ("error", "critical"):
        for fn in list(_error_hooks):
            fn(str(event_dict.get("event", "")))
    return event_dict


def _secret_values() -> list[str]:
    return [v for k in _SECRET_ENV if (v := os.environ.get(k)) and len(v) >= 6]


def _redact_value(v: Any, secrets: list[str]) -> Any:
    if isinstance(v, str):
        for s in secrets:
            if s in v:
                v = v.replace(s, "<redacted>")
        return v
    if isinstance(v, Mapping):
        return {k: ("<redacted>" if str(k).lower() in _SECRET_KEYS else _redact_value(x, secrets))
                for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return type(v)(_redact_value(x, secrets) for x in v)
    return v


def redact(_: Any, __: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    secrets = _secret_values()
    out: dict[str, Any] = {}
    for k, v in event_dict.items():
        out[k] = "<redacted>" if k.lower() in _SECRET_KEYS else _redact_value(v, secrets)
    return out


def _level_filter(_: Any, method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    comp = event_dict.get("component", "")
    threshold = _component_levels.get(comp, _default_level)
    if _LEVELS.get(method.upper(), 20) < threshold:
        raise structlog.DropEvent
    return event_dict


def setup_logging(level: str = "INFO", component_levels: Mapping[str, str] | None = None,
                  stream: TextIO | None = None) -> None:
    global _default_level
    _default_level = _LEVELS.get(level.upper(), 20)
    _component_levels.clear()
    for k, v in (component_levels or {}).items():
        _component_levels[k] = _LEVELS.get(v.upper(), 20)
    logging.basicConfig(level=logging.WARNING, stream=stream or sys.stdout)
    structlog.configure(
        processors=[
            _level_filter,
            structlog.processors.add_log_level,
            _count_errors,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(file=stream or sys.stdout),
        cache_logger_on_first_use=False,
    )


def get_logger(component: str) -> Any:
    # Lazy proxy: picks up the configuration active at call time (module-level loggers are
    # created before setup_logging runs).
    return structlog.get_logger(component=component)
