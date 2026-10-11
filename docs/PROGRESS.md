# PROGRESS

Resume guide: read this file, then `SPEC.md`, `docs/OPEN_QUESTIONS.md`, `docs/DISCREPANCIES.md`.
Environment: `uv sync` then `uv run pytest -q`. Each milestone is one git commit `M<n>: ...`.

Safety constraints in force for this build: never run `live`; never place real orders; only public market-data
endpoints are called on production; signed calls only against demo (no demo keys were available in this
session, so no signed call was made at all).

## Status

| Milestone | Code | Tests | Commit |
|---|---|---|---|
| M0 scaffold, config, logging, exchangeInfo, SymbolSpec | done | done | M0 |
| M1 market data gateway, order book, recorder | pending | | |
| M2 universe screener | pending | | |
| M3 fair value, quoting, paper fills, analytics, shadow | pending | | |
| M4 order manager, rate limiter, reconciler, dead-man, risk → demo | pending | | |
| M5 live small (gated) + shadow-vs-live gap report | pending | | |
| M6 hedger + beta | pending | | |
| M7 auto promotion/demotion, multi-level quoting | pending | | |

## M0 — done

- `src/riverbank/core/{models,config,clock,decimal_utils}.py`, `exchange/{environments,signing,rest,specs}.py`,
  `ops/logging.py`, `config/{config.yaml,blacklist.yaml}`, `tools/show_specs.py`.
- Tests: `tests/unit/test_decimal_utils.py` (hypothesis property tests for tick/step rounding),
  `tests/unit/test_specs_config_signing.py` (exchangeInfo parsing from a live-captured fixture, config validation,
  live-mode gate, HMAC vs. the Binance doc example, RSA/Ed25519 round trip, production signed-request refusal,
  HTTP error classification incl. the 503 variants, log secret redaction).
- Acceptance: `uv run python tools/show_specs.py` printed specs for DOGEUSDT, XRPUSDT, SOLUSDT, ADAUSDT, LINKUSDT
  from the live public API (2026-10-12). `ruff` clean, `mypy --strict` clean on core/.

## Pending live-time validation

(filled in as milestones add run scripts)
