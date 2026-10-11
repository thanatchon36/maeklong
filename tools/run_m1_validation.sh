#!/usr/bin/env bash
# M1 acceptance (SPEC §12): 24 h run on 20 symbols, public market data only (no keys).
# Report: reports/m1-validation/report.md (interim copy refreshed every 10 min).
set -euo pipefail
cd "$(dirname "$0")/.."
exec uv run python tools/validate_book.py --symbols "${SYMBOLS:-20}" --hours "${HOURS:-24}" \
  --out "${OUT:-reports/m1-validation}" --data-dir "${DATA_DIR:-data/m1-validation}" --record-depth
