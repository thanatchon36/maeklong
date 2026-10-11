#!/usr/bin/env bash
# Demo mode (SPEC §3): demo-fapi.binance.com with DEMO API keys — real orders, fake money.
# Needs BINANCE_API_KEY / BINANCE_API_SECRET of a *demo* account (they are different from live keys).
# M4 acceptance: run for 72 h, then:
#   uv run python tools/kill_drill.py --db state/demo/riverbank.sqlite      (stops the bot: HALT)
#   uv run python tools/demo_acceptance.py --db state/demo/riverbank.sqlite
set -euo pipefail
cd "$(dirname "$0")/.."
: "${BINANCE_API_KEY:?set the DEMO API key}" "${BINANCE_API_SECRET:?set the DEMO API secret}"
export MODE=demo
exec uv run python -m riverbank.main --config "${CONFIG:-config/demo.yaml}"
