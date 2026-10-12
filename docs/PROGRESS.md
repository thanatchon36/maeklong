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
| M4 order manager, rate limiter, reconciler, dead-man, risk → demo | done | done | M4 |
| M5 live small (gated) + shadow-vs-live gap report | done (live blocked, B1) | done | M5 |
| M6 hedger + beta | done | done | M6 |
| M7 auto promotion/demotion, multi-level quoting | done (live-only rotation) | done | M7 |

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

## M4 — done

- `orders/gateway.py` (REST execution: 503-unknown → UNKNOWN + query, never resent; 503/internal → retry
  200/400/800/1600 ms; -1008 → back-off with cancels/reduce-only exempt; 429/418 → limiter halt + venue HALT;
  -5022 post-only = normal reject), `exchange/user_stream.py` (listenKey lifecycle, `/private` route, parsing),
  `orders/reconciler.py` + `Engine._on_recon` (RECON_MISMATCH → cancel symbol, adopt exchange state, alert,
  resolve; 3 consecutive → HALT), `orders/deadman.py` (per-symbol countdownCancelAll heartbeats + quoting gate),
  `orders/live_venue.py` (startup checks, trading-rules monitor, time sync, clean shutdown),
  `tools/{cancel_all,flatten_all,_exchange_cli,kill_drill,demo_acceptance}.py`, `tools/run_demo.sh`,
  `config/demo.yaml`.
- Tests (`tests/integration/fake_binance.py` = in-process fake exchange): startup checks (one-way switch,
  isolated/leverage, stale rb-* cancelled, foreign orders untouched, refuse Multi-Assets / higher maker fee);
  quoting → fill via user stream → position; clean shutdown cancels everything + disarms dead-man; 503-unknown
  executed (resolved, not resent) and not executed (→ REJECTED after query grace); 429 → HALTED + persisted +
  traffic halted ≥ 60 s; user-stream drop → cancel all → reconnect → recon → resume; **process kill → exchange
  dead-man cancels**; recon mismatch (silent position + ghost rb order) → cancel/adopt/resolve; market-data loss
  → quotes pulled; kill switch < 1 s with cancels; standalone cancel_all/flatten_all (dry run, execute, live gate);
  rate limiter windows/headers/halt; user-stream payload parsing; trading-rules monitor.
- Not run: signed calls against demo (no demo API keys in this session) — see pending table.

## Dashboard metrics fix (2026-10-12)

- Found while reading the Grafana dashboard against a running demo: five panels had no data because their
  metrics were declared but never set: `rb_markout_bps`, `rb_quote_uptime_ratio`, `rb_rate_limit_usage_ratio`,
  `rb_ws_latency_ms`, `rb_errors_total` (the "Ops" entry below overstated the dashboard). Now exported:
  markout per horizon (last 200 fills), today's two-sided uptime (UTC day), limiter usage (timer hook in
  `main.py`), WS latency = receive time on the exchange clock − event time `E`, and every ERROR/CRITICAL log
  line counted by event name (`ops/logging.add_error_hook`). Tests: `tests/unit/test_metrics_export.py`.
- Recon race (first demo run, 2026-10-12): two `RECON_MISMATCH unknown_open_order` on XRPUSDT were false
  positives: the REST snapshot was taken while our order was open and its cancel confirmed before the snapshot
  was processed. A locally terminal order now counts as "unknown" only if it was terminal before the snapshot
  was requested (`tests/unit/test_recon_race.py`). Both had self-resolved within 30 s.
- Hedger vs gross limit (first demo run): with ~87 USDT of long inventory every hedge was rejected by
  `max_gross_exposure` (worst-case gross 117 + a 49 USDT hedge > 150), i.e. the hedger failed exactly when
  needed, and logged `placed: false` every 5 s. The hedge is now also capped by the remaining gross headroom
  (`RiskEngine.gross_worst_case`), and a rejected hedge logs `hedge_rejected` with the risk reason once per
  change. Tests in `tests/unit/test_hedger.py`.
- Screener churn (first shadow run, 2026-10-12): 38 distinct symbols quoted in 45 min, 4–11 candidates changed
  every screen, so no symbol could collect the 300 fills promotion needs. Cause: without recorded history the
  spread came from one bookTicker snapshot, and thin books flip between 1 and 2 ticks across the 6 bps bar
  (two production screens 30 s apart: 11 changes). Now the screener uses the median of the last 12 snapshot
  spreads until history exists, and last screen's candidates stay down to 0.85 × the spread bar (toxicity up to
  `toxicity_max` / 0.85). Measured on production: 1–4 changes per screen after a few screens. Shadow restarted on
  it at 06:41:59 UTC (M3 window restarts). Tests in `tests/unit/test_screener.py`.
- Hedge leg deadlock (demo twin, 2026-10-12 07:38 UTC): a `max_position` soft breach put the twin in
  REDUCE_ONLY; the quoted symbols flattened, but the ETH hedge leg stayed at 48.7 of 50 USDT, above the 90 %
  resume bar. Nothing could shrink it (the quoter never trades a hedge instrument; the hedger ran only in OK),
  so the venue was stuck in REDUCE_ONLY holding a naked ETH long. The hedger now unwinds in REDUCE_ONLY:
  `reduceOnly` orders that shrink an existing hedge position, capped at flat. Tests in
  `tests/unit/test_hedger.py`. **Not deployed yet**: demo and shadow keep the old code until M4 ends
  (2026-10-15 05:35 UTC) so the M4 window is not reset; deploy then (`up -d --build`), before 3e.

## M5 — code done; live still blocked (OPEN_QUESTIONS B1)

- Scope agreed with the operator (2026-10-12): write and unit-test M5–M7; never run live. Live remains refused by
  `LIVE_ACCEPTANCE_APPROVED = False` in `orders/live_venue.py` until the M1/M3/M4 acceptance runs pass and the
  operator approves; flipping it is the only change needed, the `MODE=live` + `LIVE_CONFIRM` gate stays.
- `universe/rotation.py`: B10/B11 promotion (`select_active`), shadow evidence from the shadow DB opened read-only
  (`shadow_evidence`), demotion cooldown + fresh-evidence rule, decisions written to `universe_decisions`
  (`promoted_symbols`, B21). `select_real_symbols` uses it in live; demo keeps its configured list.
- Shadow twin: demo/live add a paper venue `shadow` on the real venue's symbols (B22); `analytics/gap.py`
  (shadow-vs-live gap: fill rate per quoted hour, markout 5 s, realized edge, gap ± combined SE) appended to
  the daily report and available as `python -m riverbank.analytics.gap --db ... --start ... --end ...`.
- `config/live.yaml` (own data/state/reports dirs, metrics 9110, `universe.shadow_db`, empty allow-list).
- Tests: `tests/unit/test_rotation.py`, `tests/unit/test_gap_report.py`, twin test in
  `tests/integration/test_demo_fault_injection.py`.
- Found: `test_503_unknown_executed_is_resolved_not_resent` raced (the fake exchange books the order before it
  answers 503, so the test could see two open orders before the client handled the 503). Probably the
  "unidentified flaky test"; the twin's extra engine work made it fail ~1 in 3. The test now waits for it.
- Acceptance (7 days live within limits + gap report) needs the live run: not done.

## M6 — done (code + replay acceptance; live part pending with M5)

- `hedge/beta.py` (`BetaTable`: klines → 1-min log returns → shrunk OLS beta vs each hedge instrument; refresh
  loop), `hedge/hedger.py` (`Hedger`, decisions in OPEN_QUESTIONS B23). `main.py` feeds the betas to the risk
  engine (`Engine(beta=...)`) and registers the hedger as a timer hook when `hedge.enabled`.
- Tests: `tests/unit/test_beta.py` (returns/gaps, OLS + shrink, prior on little data, klines refresh with
  paging and completed candles only), `tests/unit/test_hedger.py` (threshold, BTC → ETH fallback with ETH
  beta, nothing fits, hedge position counted, max-position cap, pull when not OK, keep/cancel resting hedge,
  taker only if allowed, eval interval).
- Acceptance in replay (`tests/replay/test_replay_hedge.py`, trending synthetic market, net-beta limit 35):
  net beta stays within the limit; hedge fills are all maker; net-beta pre-trade rejects drop from 17,174 to
  3,368 (the risk engine already caps net beta pre-trade, so the hedger's effect is room to keep quoting).
- Smoke (shadow, public API only, 75 s): betas from 4,318 one-minute returns, e.g. DOGE 1.31 / XRP 1.26 vs BTC.
- Live part of the acceptance ("in replay and live") needs the live run: not done.

## M7 — done (code; live acceptance pending with M5)

- `universe/rotation.py`: `demotion_check` (B10), `rotate` (demote, cooldown release, fill free slots; no
  displacement), shared evidence readers. `orders/live_venue.py`: `configure_symbol` (shared with the startup
  checks), `RealVenueRuntime.rotate_once` + live-only `_rotation_loop` (decisions in OPEN_QUESTIONS B24).
- Multi-level quoting: kept at `levels: 1`; the real-venue path is now tested with 2 levels.
- Tests: rotation unit tests in `tests/unit/test_rotation.py`; in `tests/integration/test_demo_fault_injection.py`
  rotation demotes + promotes with exchange setup and decision rows, a failed exchange setup skips the symbol,
  and two quote levels per side reach the fake exchange.
- Acceptance ("universe rotates automatically with documented decisions in logs") needs the live run: not done.

## Ops: Docker, monitoring, runbook (2026-10-12, no keys needed)

- `docker/{Dockerfile, Dockerfile.dockerignore, docker-compose.yml, env.example}`: services `shadow`
  (default), `demo` (profile `demo`), `m1` (profile `m1`), `prometheus`, `grafana`. There is no live service.
  The repo root is bind-mounted as the working dir, so `KILL`, `state/` and `reports/` are shared with host tools.
  `init: true` forwards SIGTERM/SIGUSR1. Prometheus and Grafana are bound to 127.0.0.1 only.
- `docker/prometheus/prometheus.yml`, `docker/grafana/{provisioning,dashboards}`: provisioned datasource and a
  `riverbank` dashboard (risk state, up, recon, equity, inventory, open orders, fills/min, edge, markout, quote
  uptime, book OK, rate-limit usage, WS latency, errors) with a `bot` selector.
- `docs/RUNBOOK.md` (Thai), `docs/ACCEPTANCE_PLAN.md`.
- Smoke test (Docker Desktop, macOS, public data only): image builds; shadow healthy; Prometheus scrapes it;
  Grafana loads the datasource and dashboard; `docker compose kill -s SIGUSR1 shadow` → HALTED in 68 ms; restart
  refused while HALT is persisted (restart loop with exit 2, as designed).
- Found: reading `state/riverbank.sqlite` from the macOS host while the container had it open **corrupted the
  DB** (SQLite locks do not cross the Docker Desktop VM). The restarted bot then hung forever in
  `SqliteRepository.__init__` (writer thread died on open, `_ready` never set): fail-closed but silent.
  RUNBOOK §3.2 forbids host-side DB access on macOS. Fixed: the writer thread hands the open error back and
  the constructor raises it, so the bot exits non-zero instead of hanging (`tests/unit/test_sqlite_repository.py`).
- Also: `config/demo.yaml` → `ops.kill_file: KILL.demo`; `tools/shadow_acceptance.py` 7-day check now passes
  for a full `--start D0 --end D6` window (1 h edge tolerance; test added).

## SPEC completeness fixes (2026-10-12)

- SPEC §5.11: market-data disconnect alert. The engine tracks each (route, symbols) connection; down > 30 s
  → one `md_disconnect` WARN, recovery → `md_reconnected` INFO; planned unsubscribes are ignored
  (`tests/unit/test_md_disconnect_alert.py`). Previously only the user stream alerted.
- OPEN_QUESTIONS B6 for shadow: paper-venue HALTs (daily loss, drawdown, exceptions) are now persisted like
  real-venue HALTs, so a crash/restart cannot resume shadow quoting (`tests/unit/test_shadow_halt.py`). Replay
  does not persist.
- Known remaining gaps (by milestone): ~~ACTIVE promotion and shadow-vs-live gap report (M5)~~ done,
  ~~hedger (M6)~~ done, ~~auto-rotation (M7)~~ done. Remaining: the wall-clock acceptance runs (M1, M3, M4, then M5–M7 live).

## Pending live-time validation

| Acceptance | How to run | Report |
|---|---|---|
| M1: 24 h on 20 symbols, no undetected gaps, L1–L5 ≥ 99.9 %, Parquet readable | `tools/run_m1_validation.sh` (env: SYMBOLS, HOURS) | `reports/m1-validation/report.md` (+ `report.interim.md` every 10 min) |
| M3: 7-day shadow run on candidates, daily markout/PnL reports, deterministic replay | `tools/run_shadow.sh` for ≥ 7 days, then `uv run python tools/shadow_acceptance.py --start D0 --end D6` | `reports/YYYY-MM-DD/report.md` daily + `reports/m3-acceptance/report.md` |
| M4: 72 h demo run, zero unresolved RECON_MISMATCH, fault-injection tests pass, kill switch < 1 s | set DEMO keys, `tools/run_demo.sh` for ≥ 72 h; then `tools/kill_drill.py --db state/demo/riverbank.sqlite`; then `tools/demo_acceptance.py` | `reports/m4-acceptance/report.md` |
| M4: first signed contact with demo (never done here: no demo keys) | as above; watch the `real_venue_ready` log line and `/private` user stream connect | logs |
