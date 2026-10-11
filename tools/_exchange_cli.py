"""Shared helpers for the standalone emergency tools (cancel_all.py, flatten_all.py).

Safety: demo by default. `--live` additionally requires MODE=live and
LIVE_CONFIRM=I_UNDERSTAND_THE_RISK in the environment (same gate as the bot).
Credentials come only from the environment.
"""

from __future__ import annotations

import argparse
import sys

from riverbank.core.config import SafetyError, resolve_mode
from riverbank.core.models import Mode
from riverbank.exchange.environments import ENVIRONMENTS, Environment
from riverbank.exchange.rest import RestClient, unlock_live_signing
from riverbank.exchange.signing import credentials_from_env


def add_env_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--live", action="store_true", help="production (needs MODE=live + LIVE_CONFIRM env)")
    ap.add_argument("--base-url", help=argparse.SUPPRESS)  # tests / fake exchange


def client(args: argparse.Namespace) -> tuple[RestClient, Environment]:
    creds = credentials_from_env()
    if creds is None:
        sys.exit("BINANCE_API_KEY and BINANCE_API_SECRET (or BINANCE_PRIVATE_KEY_PATH) must be set")
    if args.base_url:
        env = Environment("custom", args.base_url, args.base_url.replace("http", "ws"), True)
    elif args.live:
        try:
            if resolve_mode(Mode.SHADOW) is not Mode.LIVE:
                raise SafetyError("MODE=live is not set")
            unlock_live_signing()
        except SafetyError as e:
            sys.exit(f"REFUSED: {e}")
        env = ENVIRONMENTS[Mode.LIVE]
    else:
        env = ENVIRONMENTS[Mode.DEMO]
    print(f"environment: {env.name} ({env.rest})")
    return RestClient(env.rest, creds), env
