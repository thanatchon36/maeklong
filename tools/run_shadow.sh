#!/usr/bin/env bash
# Shadow mode (SPEC §3): real public market data, simulated fills, NO keys, NO orders.
# M3 acceptance: run for 7 days, then:  uv run python tools/shadow_acceptance.py --start <d0> --end <d6>
# Daily reports are written automatically to reports/YYYY-MM-DD/ after each UTC midnight.
set -euo pipefail
cd "$(dirname "$0")/.."
unset BINANCE_API_KEY BINANCE_API_SECRET BINANCE_PRIVATE_KEY_PATH   # shadow never needs keys
export MODE=shadow
exec uv run python -m riverbank.main --config "${CONFIG:-config/config.yaml}"
