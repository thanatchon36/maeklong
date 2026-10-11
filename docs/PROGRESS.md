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
| M1 market data gateway, order book, recorder | done | done | M1 |
| M2 universe screener | done | done | M2 |
| M3 fair value, quoting, paper fills, analytics, shadow | done | done | M3 |
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

## M1 — done

- `exchange/ws.py` (reconnect + full-jitter backoff, make-before-break 24 h roll with ordered replay),
  `exchange/endpoints.py` (paths/weights), `exchange/rate_limiter.py` (windows from exchangeInfo, header drift
  correction, 429 halt), `marketdata/{orderbook,market_state,gateway,recorder,pipeline}.py`,
  `tools/validate_book.py`, `tools/run_m1_validation.sh`, `tools/capture_depth_fixture.py`.
- Routed WS endpoints `/public` (depth, bookTicker) and `/market` (aggTrade, markPrice@1s) — DISCREPANCIES #1.
- Book self-check/refresh: a fresh snapshot book is compared with the live book at the same update id; mismatch
  or periodic refresh swaps books without an INVALID window.
- Tests: book sync unit tests + hypothesis property test (random drops/duplicates/snapshots at arbitrary ids →
  synced book always equals ground truth); market state (dedupe, staleness, disconnect, check/self-heal);
  parsing of real captured messages; routed URLs/sharding; recorder round trip/compaction/retention;
  ReconnectingWs against a local server (gapless roll, reconnect status); replay of a real recorded diff stream
  (tests/fixtures/depth_capture.jsonl.gz) with injected gaps → detected, resynced, never silently wrong.
- Smoke runs against the live public API (2026-10-12): 20 symbols × 3 min → 190/190 L1–L5 exact matches,
  0 undetected mismatches, 100 Parquet files all readable; 3 symbols × 1.2 min → 20/20.

## M2 — done

- `universe/screener.py` (hard exclusions, metrics, eligibility, ranking, scheduled `Screener.run`),
  `universe/history.py` (daily medians from recorded Parquet, cached per completed day; snapshot fallback during
  bootstrap), `quoting/sizing.py` (order size rules, effective inventory units, client-id length check),
  `tools/screen_once.py`.
- Tests: every hard-exclusion rule (incl. hedge-only, blacklist file, listing age, TRADIFI/USDC/delivery, non-ASCII,
  POSITION_RISK_CONTROL), metrics/eligibility/ranking/candidate cap, sizing edge cases, history-over-snapshot
  preference with a recorded dataset, scheduled screener survives a failed cycle.
- Acceptance: `uv run python tools/screen_once.py` against the live public API (2026-10-12) produced a ranked
  list of 20 CANDIDATEs with all metrics; BTCUSDT/ETHUSDT reported as excluded (hedge_only).

## M3 — done

- `features/fair_value.py`, `quoting/engine.py` (A-S reservation/half-spread, guards, toxicity ladder, funding
  guard, REDUCE_ONLY quoting, multi-level ready), `sim/paper_fills.py` (conservative fill model),
  `orders/{client_ids,state_machine,manager,positions}.py` (venue-agnostic order manager shared with M4),
  `risk/{limits,engine,killswitch}.py` (full pre-trade checks + periodic evaluation; HALT persistence),
  `analytics/{markout,pnl,report}.py`, `storage/sqlite.py`, `engine.py`, `sim/replay.py`, `main.py` (shadow),
  `ops/{alerts,metrics}.py`, tools `replay.py`, `run_shadow.sh`, `shadow_acceptance.py`, `clear_halt.py`.
- Note: the order manager, state machine and risk engine were needed for shadow (every intent passes risk.check),
  so their venue-agnostic parts landed in M3; M4 adds the real exchange gateway, reconciler, dead-man, user stream.
- Tests: fair value math (EWMA σ closed form, micro, imbalance, flow decay); quoting (A-S math, floor/ceil, skew,
  inventory caps, never-cross, every guard, vol kill/cooldown, toxicity ladder up/down, funding pull/widen,
  reduce-only, multi-level, PERCENT_PRICE); paper fills (latency, queue, nq/RPI, trade-through, unexplained
  decrease, improving price, post-only reject at arrival, book-cross fill, cancel/after-fill, taker fee); state
  machine table; order manager (cancel-then-new requote, stream-before-ack, UNKNOWN path, fill during cancel,
  duplicate fills, post-only reject, risk veto); risk (one breach test per limit, daily loss soft/hard + 50% alert,
  drawdown, hysteresis, liquidation distance, sticky HALT); markout/stats/PnL by hand; replay determinism, latency
  sensitivity, report generation, paper funding, kill switch → cancel + flatten (synthetic recorded dataset).
- Live shadow smoke runs (public data only, 2026-10-12): 8 candidates, fills simulated, daily report generated;
  `touch KILL` → HALTED in **181 ms** with 13 cancels; restart refused while KILL/HALT present; `clear_halt.py`.
  Replay of the recorded live data: identical digest on two runs (`tools/replay.py --check-determinism`).
- Findings fixed during the smoke runs: SPEC leverage 3 + 30 % min liquidation distance puts every short in
  REDUCE_ONLY immediately → leverage setting 2 (OPEN_QUESTIONS B19); requote churn reduced by an asymmetric
  requote throttle (B18); order-size reference price (B20); O(n) open-order scan → index (5× faster replay).

## Pending live-time validation

| Acceptance | How to run | Report |
|---|---|---|
| M1: 24 h on 20 symbols, no undetected gaps, L1–L5 ≥ 99.9 %, Parquet readable | `tools/run_m1_validation.sh` (env: SYMBOLS, HOURS) | `reports/m1-validation/report.md` (+ `report.interim.md` every 10 min) |
| M3: 7-day shadow run on candidates, daily markout/PnL reports, deterministic replay | `tools/run_shadow.sh` for ≥ 7 days, then `uv run python tools/shadow_acceptance.py --start D0 --end D6` | `reports/YYYY-MM-DD/report.md` daily + `reports/m3-acceptance/report.md` |
