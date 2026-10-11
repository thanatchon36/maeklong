# SPEC.md — Multi-Symbol Quantitative Market Maker for Binance USDⓈ-M Futures

> **Codename:** `riverbank`
> **Target venue:** Binance USDⓈ-M Perpetual Futures
> **Operator:** single retail/prop operator, small capital (start: 200–500 USDT)
> **Spec version:** 1.0

---

## 0. Instructions for Claude Code (read first)

1. **Build milestone by milestone** (§12). After each milestone, stop, run the acceptance tests, and summarize results. Do **not** start the next milestone without operator approval.
2. **Never place real orders** unless `MODE=live` **and** `LIVE_CONFIRM=I_UNDERSTAND_THE_RISK` are both set in the environment. Default mode is `shadow`.
3. **Verify every Binance endpoint, stream name, field name, filter and rate limit** against the current official docs at `https://developers.binance.com` (USDⓈ-M Futures section) before implementing. Binance changes APIs; this spec may be out of date. If something in this spec conflicts with the docs, the docs win — note the discrepancy in `docs/DISCREPANCIES.md`.
4. Never read min notional, tick size, step size or rate limits from constants. Always load them from `GET /fapi/v1/exchangeInfo` at startup and refresh periodically.
5. API keys come only from environment variables / secret files. Never log them. Never commit them.
6. Write tests alongside code. Hot-path logic (book building, quoting math, risk checks, order state machine) must have unit tests before integration.
7. When unsure about a design decision not covered here, add it to `docs/OPEN_QUESTIONS.md` and pick the most conservative option.

---

## 1. Mission

Operate as a **quantitative liquidity provider** on Binance USDⓈ-M perpetuals: continuously post two-sided limit quotes on a dynamically selected set of contracts, earn the bid-ask spread and maker flow, and keep risk small by managing inventory, hedging common market exposure, and pulling quotes when flow becomes toxic.

The system makes money **only** when:

```
Realized edge per fill = captured half-spread + markout(h) − maker fee  > 0
```

measured **per symbol**, after costs. Every component exists to make this number positive and keep losses bounded when it isn't.

### Design principles

- **Measure, don't guess.** Symbol selection, spread width and sizing are driven by measured markout, not intuition.
- **Risk is a separate system.** The risk engine can veto any order and cannot be bypassed by strategy code.
- **Fail safe.** On any doubt (stale data, disconnect, unknown order state, limit breach), cancel quotes first, investigate second.
- **Small and correct beats large and fast.** We do not compete on latency with professional HFT firms.

## 2. Non-goals

- No latency arms race (no co-location, no FPGA, no kernel bypass).
- **Do not quote BTCUSDT / ETHUSDT** (spread ≈ 1 tick, below our fee; dominated by professional MMs). They may be used **only as hedge instruments**.
- No directional prediction strategies, no MEV, no cross-exchange arbitrage (v1).
- No spot market (v1). No COIN-M (v1).
- No withdrawals or wallet transfers — the API key must not have these permissions.

---

## 3. Environments

| Mode | REST base | WS base | Sends orders | Purpose |
|---|---|---|---|---|
| `demo` | `https://demo-fapi.binance.com` | `wss://demo-fstream.binance.com` | yes (fake money) | Test plumbing: order lifecycle, errors, reconnects |
| `shadow` | `https://fapi.binance.com` | `wss://fstream.binance.com` | **no** (simulated fills) | Test strategy on real market data |
| `live` | `https://fapi.binance.com` | `wss://fstream.binance.com` | yes (real money) | Measure real edge, small size |

- Demo and live API keys are **different** and not interchangeable.
- Demo profitability is **meaningless** for MM (liquidity and counterparties differ). Use demo only for correctness.
- `shadow` results are an **upper bound**; live will be worse. Track the shadow-vs-live gap explicitly.

---

## 4. System architecture

```
                  ┌──────────────────────────────────────────────────┐
                  │                  Binance USDⓈ-M                  │
                  └───────▲───────────────────┬──────────────────────┘
                          │ orders            │ market data + user data
              ┌───────────┴─────┐   ┌─────────▼──────────┐
              │  Order Gateway  │   │ Market Data Gateway│
              │ (REST/WS-API,   │   │ (depth, trades,    │
              │  rate limiter)  │   │  bookTicker, mark) │
              └───────▲─────────┘   └─────────┬──────────┘
                      │                       │
              ┌───────┴─────────┐   ┌─────────▼──────────┐       ┌──────────────┐
              │  Order Manager  │◄──┤   Local Order Book │──────►│   Recorder   │
              │ (state machine, │   │   + Fair Value     │       │ (Parquet)    │
              │  reconciliation)│   └─────────┬──────────┘       └──────────────┘
              └───────▲─────────┘             │
                      │ approved intents      │ features
              ┌───────┴─────────┐   ┌─────────▼──────────┐
              │   RISK ENGINE   │◄──┤   Quoting Engine   │
              │ (veto, limits,  │   │ (per-symbol A-S    │
              │  kill switch)   │   │  + skew + filters) │
              └───────▲─────────┘   └─────────▲──────────┘
                      │                       │ active universe
              ┌───────┴─────────┐   ┌─────────┴──────────┐
              │     Hedger      │   │ Universe Screener  │
              │ (beta hedge on  │   │ (+ markout-based   │
              │  BTC/ETH perp)  │   │  auto-rotation)    │
              └─────────────────┘   └─────────▲──────────┘
                                              │
                                    ┌─────────┴──────────┐
                                    │ Analytics (markout,│
                                    │ PnL decomposition) │
                                    └────────────────────┘
                     Monitoring: Prometheus metrics → Grafana; alerts → Telegram
```

- Single process, `asyncio` event loop, one task per component. Components communicate via in-process async queues / typed events.
- In `shadow` mode, the **Paper Fill Simulator** replaces the Order Gateway and consumes the same order intents.

---

## 5. Components

### 5.1 Market Data Gateway

- Use **combined streams** to multiplex symbols. Per quoted symbol subscribe to:
  - diff depth stream (100 ms) for local order book
  - `bookTicker` (best bid/ask, fastest top-of-book)
  - `aggTrade` (trade prints; needed for fill simulation, toxicity, markout)
  - `markPrice` (mark price + funding rate + next funding time)
- Also subscribe to the same for hedge instruments (BTCUSDT, ETHUSDT).
- **Local order book** must follow Binance's official snapshot + diff synchronization procedure (snapshot from REST depth, buffer diffs, validate update-id continuity incl. `pu`). On any gap → mark book `INVALID`, cancel that symbol's quotes, resync.
- Reconnect with exponential backoff + jitter. Respect the 24 h connection lifetime — proactively roll connections before forced disconnect.
- **Staleness watchdog:** if no update for a symbol for > `stale_ms` (config, default 3000 ms) → book `STALE` → quotes pulled.
- Respect stream/subscription limits per connection; shard symbols across multiple connections if needed.

### 5.2 Universe Screener

Runs every `screen_interval` (default 5 min). Uses `exchangeInfo`, `ticker/24hr`, `bookTicker`, plus recorded history.

**Hard exclusions**
- BTCUSDT, ETHUSDT (hedge-only)
- status ≠ `TRADING`, contractType ≠ `PERPETUAL`, quote asset ≠ USDT
- listed < `min_listing_days` (default 30)
- symbols on an operator-maintained `blacklist.yaml` (e.g. announced delisting)

**Metrics (rolling medians over `screen_lookback`, default 7 days of recorded data; fall back to snapshot only during bootstrap)**
- `spread_bps` — median quoted spread
- `tick_bps` — tick size / mid
- `quote_vol_musd` — 24 h quote volume
- `range_bps` — realized volatility proxy
- `toxicity_proxy` = `range_bps / spread_bps`
- `fill_opportunity` — trade prints per minute at/through best

**Eligibility (configurable)**
- `spread_bps ≥ edge_ratio_min × round_trip_fee_bps` (default 1.5×)
- `quote_vol_musd` within `[vol_min, vol_max]` (default 5–300)
- `toxicity_proxy ≤ toxicity_max`
- min notional allows our configured order size

**Two-stage universe**
- `CANDIDATE` → quoted in **shadow only** to collect markout.
- `ACTIVE` → quoted live, only if shadow markout (≥ `min_fills_for_promotion`, default 300 simulated fills) shows positive realized edge.
- `DEMOTED` → if live realized edge over the last N fills (default 500) is negative at the 1-σ level, stop quoting; return to `CANDIDATE` after cooldown.
- Cap: `max_active_symbols` (default 3 at start).

### 5.3 Fair Value Engine

Per symbol, compute on every book update:

- `mid = (bid + ask) / 2`
- **Microprice** (L1): `micro = (ask × bid_qty + bid × ask_qty) / (bid_qty + ask_qty)`
- **Book imbalance** over top N levels (default 5)
- **Realized volatility** σ: EWMA of mid log-returns sampled every 1 s, half-life configurable (default 60 s); expose σ in bps per √second.
- **Short-term trade flow**: signed aggTrade volume EWMA (half-life ~5 s)
- `fair_value = micro + α_imb × imbalance_adj + α_flow × flow_adj` (α default 0 in v1; enable only after analytics show predictive value)
- **v2 (optional):** lead-lag signal from BTCUSDT returns (beta-scaled) to shift fair value.

### 5.4 Quoting Engine

Inspired by Avellaneda–Stoikov, adapted for perpetuals (no terminal time).

Inputs per symbol: `fair_value`, σ, inventory `q` (in units of `order_size`), `tick`, `fee_bps`, toxicity state.

```
reservation = fair_value × (1 − skew_k × q_norm × σ_bps / 1e4)
              where q_norm = q / max_inventory_units  ∈ [−1, 1]

half_spread_bps = max(
    min_half_spread_bps,                          # floor, ≥ maker fee + safety
    base_half_spread_bps + vol_mult × σ_bps_per_horizon
) × toxicity_multiplier

bid = floor_to_tick(reservation × (1 − half_spread_bps/1e4))
ask = ceil_to_tick (reservation × (1 + half_spread_bps/1e4))
```

Rules:
- Never cross: `bid < best_ask` and `ask > best_bid`. All quotes are **post-only** (`timeInForce = GTX`). A post-only rejection is normal, not an error.
- **Inventory caps:** if `q ≥ max_inventory_units` → do not quote bid; if `q ≤ −max_inventory_units` → do not quote ask.
- **Order size:** `max(order_size_usdt, min_notional × size_buffer)` rounded to step size; `size_buffer` default 2.0.
- **Requote threshold:** only replace an order if desired price differs from live price by ≥ `requote_ticks` (default 1) or size changes — saves rate limit and queue position.
- **Volatility circuit breaker:** if σ > `vol_kill_bps` → pull quotes for `cooldown_s`.
- **Toxicity response:** if recent markout(5s) on this symbol < `toxic_markout_bps` → `toxicity_multiplier` increases (e.g. 1.5×, 2×, then pull).
- **Funding guard:** within `funding_guard_s` (default 60 s) before/after funding timestamp → widen or pull quotes (configurable).
- **Event guard:** pull quotes when book is `STALE`/`INVALID`, on user-data-stream disconnect, or when risk engine is not `OK`.
- v1 quotes **one level per side**. Multi-level layering is v2.

### 5.5 Order Manager

- Each order has a deterministic `newClientOrderId` = `rb-{symbol}-{side}-{seq}` so state survives ambiguous responses.
- **State machine:** `PENDING_NEW → NEW → PARTIALLY_FILLED → FILLED | CANCELED | REJECTED | EXPIRED`, plus `PENDING_CANCEL` and `UNKNOWN`.
- **Source of truth:** user data stream (`ORDER_TRADE_UPDATE`, `ACCOUNT_UPDATE`). REST responses are hints.
- **HTTP 503 handling** (per Binance docs):
  - "Unknown error…" → status **UNKNOWN**; do **not** resend. Query by `origClientOrderId` and wait for stream update.
  - "Service Unavailable." → definite failure; retry with exponential backoff (max 3–5).
  - `-1008` throttled → definite failure; back off and reduce concurrency. Reduce-only / cancels are exempt — keep them flowing.
- **Rate limiter:** token bucket per limit type (request weight per IP, order count per account) loaded from `exchangeInfo.rateLimits`; read `X-MBX-USED-WEIGHT-*` and `X-MBX-ORDER-COUNT-*` headers to correct drift. On 429 → hard back-off; **never** continue after 429 (418 IP bans escalate up to days).
- **Reconciliation loop** every `reconcile_s` (default 30 s): compare local open orders + positions with REST `openOrders` / `positionRisk`. Mismatch → log `RECON_MISMATCH`, cancel all on that symbol, adopt exchange state, alert.
- **Dead-man switch:** use Binance's auto-cancel-all countdown endpoint for futures (verify current name in docs). Heartbeat every `deadman_heartbeat_s` (default 10 s) with countdown `deadman_countdown_s` (default 30 s). If the process dies, the exchange cancels our quotes.
- Startup: set **one-way position mode**, **isolated margin**, configured leverage per symbol; cancel any stale `rb-*` orders.

### 5.6 Risk Engine (independent, veto power)

Every order intent passes `risk.check(intent) -> Approve | Reject(reason)`. Strategy code cannot place orders directly.

| Limit | Default (start) |
|---|---|
| `max_position_usdt` per symbol | 50 |
| `max_gross_exposure_usdt` | 150 |
| `max_net_beta_exposure_usdt` (beta-adjusted to BTC) | 75 |
| `max_open_orders` total | 20 |
| `max_order_rate_per_10s` (self-imposed, below exchange limit) | 50% of exchange limit |
| `daily_loss_limit_pct` of equity | 10% |
| `max_drawdown_pct` from peak equity | 15% |
| `max_leverage` | 3 |
| `min_liq_distance_pct` (mark → liquidation price) | 30% |

States: `OK → REDUCE_ONLY → HALTED`.
- `REDUCE_ONLY`: only orders that reduce inventory allowed (soft breaches).
- `HALTED`: cancel all, flatten positions with reduce-only limit orders (taker allowed only if `emergency_flatten=true`), stop. Requires **manual** restart.
- **Kill switch:** `touch ./KILL` file, `SIGUSR1`, or Telegram `/kill` command → `HALTED` within 1 s.
- Risk engine computes equity from user data stream + periodic REST account check.

### 5.7 Hedger (Milestone 6+)

- Estimate rolling beta of each active symbol vs BTCUSDT (and optionally ETHUSDT) on 1-min returns, window 3 days, shrink toward 1.0.
- `net_beta_exposure = Σ position_usdt_i × beta_i`
- When `|net_beta_exposure| > hedge_threshold_usdt` **and** the hedge order would satisfy the hedge instrument's min notional → place hedge on BTCUSDT (or ETHUSDT if its lower min notional fits better). Prefer post-only near touch; allow taker if exposure > `hedge_urgent_usdt`.
- If hedge min notional makes hedging too coarse for current capital, hedger stays disabled and risk engine's `max_net_beta_exposure_usdt` does the work.

### 5.8 Paper Fill Simulator (shadow mode)

Conservative fill model:
- Track **queue position**: when our order joins price level P, `queue_ahead = visible qty at P` at that moment.
- `aggTrade` **through** our price → full fill.
- `aggTrade` **at** our price with the aggressor hitting our side → decrement `queue_ahead`; fill only when it drops ≤ 0.
- Visible-qty decreases at our level that are not explained by trades are treated as cancellations **behind** us (pessimistic: do not advance our queue).
- Simulate order latency: intents take effect after `sim_latency_ms` (default 150 ms, configurable), using the book state at arrival time.
- Apply maker fee to every simulated fill.
- Output fills in the **same schema** as live fills so analytics is mode-agnostic.

### 5.9 Analytics

- **Markout** per fill at horizons 1 s, 5 s, 30 s, 60 s, 300 s:
  `markout_bps(h) = side × (mid(t+h) − fill_price) / fill_price × 1e4`
- **PnL decomposition** per symbol and total: spread capture, inventory PnL (mark-to-market), fees, funding, hedge PnL.
- **Shadow-vs-live gap**: when both run on the same symbol and time window, compare fill rate and markout.
- Daily report (Markdown + charts saved to `reports/YYYY-MM-DD/`) and Grafana dashboards.
- Statistical honesty: always print `n_fills`, mean, std error; never promote/demote on < configured minimum fills.

### 5.10 Recorder & Storage

- Record raw normalized events (book top-N snapshots every 100 ms, aggTrades, bookTicker, markPrice) to **Parquet**, partitioned by `date/symbol`, rotated hourly.
- Orders, fills, risk events, recon mismatches → **SQLite** (v1) behind a repository interface so Postgres can replace it later.
- Retention configurable; default 30 days raw, forever for fills/orders.

### 5.11 Monitoring & Alerting

- Prometheus metrics: per-symbol inventory, open orders, quote uptime %, fills/min, realized edge, markout, ws latency, rate-limit usage %, risk state, equity.
- Alerts (Telegram bot): risk state change, recon mismatch, 429/418, disconnect > 30 s, daily loss > 50% of limit, process restart.
- Structured JSON logs (`structlog`), log level per component.

---

## 6. Configuration (`config/config.yaml`, validated by pydantic)

```yaml
mode: shadow            # demo | shadow | live
account:
  fee_maker_bps: 2.0    # overridden by commissionRate endpoint at startup if available
  fee_taker_bps: 5.0
  leverage: 3
  margin_type: ISOLATED

universe:
  max_active_symbols: 3
  max_candidate_symbols: 20
  min_listing_days: 30
  edge_ratio_min: 1.5
  vol_min_musd: 5
  vol_max_musd: 300
  toxicity_max: 400
  min_fills_for_promotion: 300
  demotion_window_fills: 500
  screen_interval_s: 300
  hedge_only: [BTCUSDT, ETHUSDT]

quoting:
  order_size_usdt: 10
  size_buffer: 2.0
  max_inventory_units: 5
  min_half_spread_bps: 3.0
  base_half_spread_bps: 2.0
  vol_mult: 1.0
  skew_k: 0.5
  requote_ticks: 1
  vol_kill_bps: 50
  cooldown_s: 60
  toxic_markout_bps: -3.0
  funding_guard_s: 60
  funding_guard_action: widen   # widen | pull

risk:
  max_position_usdt: 50
  max_gross_exposure_usdt: 150
  max_net_beta_exposure_usdt: 75
  max_open_orders: 20
  daily_loss_limit_pct: 10
  max_drawdown_pct: 15
  min_liq_distance_pct: 30
  emergency_flatten: false

ops:
  stale_ms: 3000
  reconcile_s: 30
  deadman_heartbeat_s: 10
  deadman_countdown_s: 30
  sim_latency_ms: 150
```

Secrets via env: `BINANCE_API_KEY`, `BINANCE_API_SECRET` (or RSA/Ed25519 key path), `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `MODE`, `LIVE_CONFIRM`.

---

## 7. Core data model (typed dataclasses / pydantic)

- `SymbolSpec(symbol, tick, step, min_qty, min_notional, price_precision, qty_precision)`
- `BookState(symbol, bids, asks, last_update_id, ts, status: OK|STALE|INVALID)`
- `Features(symbol, ts, mid, micro, imbalance, sigma_bps, flow)`
- `QuoteIntent(symbol, side, price, qty, client_id, reason)`
- `Order(client_id, exchange_id, symbol, side, price, qty, filled_qty, status, ts_created, ts_updated)`
- `Fill(ts, symbol, side, price, qty, fee, is_maker, mode, client_id, mid_at_fill)`
- `RiskEvent(ts, level, rule, detail, state_before, state_after)`

All prices/quantities use `decimal.Decimal` at the exchange boundary; floats allowed inside numeric features only.

---

## 8. Tech stack

- Python 3.12, `asyncio` + `uvloop`, `aiohttp` / `websockets`, `orjson`, `pydantic` v2, `numpy`, `pandas`/`polars` (analytics only, never in hot path), `pyarrow`, `structlog`, `prometheus-client`.
- Tests: `pytest`, `pytest-asyncio`, `hypothesis` (property tests for book sync and rounding).
- Packaging: `uv` or `poetry`; Docker image + `docker-compose.yml` (bot, Prometheus, Grafana).
- Lint/type: `ruff`, `mypy --strict` on `core/`, `risk/`, `orders/`.
- Hot path must avoid pandas and blocking I/O. Disk writes go through a background writer task.

## 9. Project layout

```
riverbank/
├── SPEC.md
├── config/{config.yaml, blacklist.yaml}
├── src/riverbank/
│   ├── main.py                # wiring, mode selection, safety gates
│   ├── core/{events.py, models.py, clock.py, decimal_utils.py}
│   ├── exchange/{rest.py, ws.py, signing.py, rate_limiter.py, specs.py}
│   ├── marketdata/{gateway.py, orderbook.py, recorder.py}
│   ├── features/fair_value.py
│   ├── universe/screener.py
│   ├── quoting/engine.py
│   ├── orders/{manager.py, state_machine.py, reconciler.py, deadman.py}
│   ├── risk/{engine.py, limits.py, killswitch.py}
│   ├── hedge/hedger.py
│   ├── sim/paper_fills.py
│   ├── analytics/{markout.py, pnl.py, report.py}
│   └── ops/{metrics.py, alerts.py, logging.py}
├── tests/{unit, integration, replay}
├── tools/{screen_once.py, replay.py, flatten_all.py, cancel_all.py}
├── docs/{OPEN_QUESTIONS.md, DISCREPANCIES.md, RUNBOOK.md}
└── docker/{Dockerfile, docker-compose.yml, grafana/}
```

---

## 10. Testing strategy

- **Unit:** tick/step rounding (property tests), A-S quote math, inventory caps, risk rules (every limit has a breach test), order state machine transitions incl. UNKNOWN path.
- **Book sync:** replay recorded diff streams with injected gaps → book must detect and resync; compare against REST snapshots (target ≥ 99.9% exact match at L1–L5).
- **Replay harness:** run quoting + paper simulator over recorded Parquet data deterministically (same input → same fills).
- **Fault injection (demo):** kill websocket, return 503-unknown, return 429, drop user-data stream, kill process → verify quotes are cancelled (dead-man) and state reconciles.
- **No test may hit live endpoints with signed requests.**

---

## 11. Safety rules (non-negotiable)

1. `live` requires `MODE=live` + `LIVE_CONFIRM=I_UNDERSTAND_THE_RISK` + successful startup checks (key has no withdraw permission if detectable, isolated margin set, risk limits loaded).
2. Use a **dedicated sub-account** funded only with risk capital; API key: trade-only, IP-whitelisted.
3. Any unhandled exception in order/risk path → `HALTED`.
4. On startup and shutdown: cancel all `rb-*` orders.
5. Never increase limits programmatically. Limit changes are config changes made by the operator.
6. `tools/flatten_all.py` and `tools/cancel_all.py` must work standalone without the main process.

---

## 12. Milestones & acceptance criteria

| # | Milestone | Acceptance |
|---|---|---|
| **M0** | Scaffold, config, logging, exchangeInfo loader, `SymbolSpec` | `pytest` green; prints specs for 5 symbols from live public API |
| **M1** | Market data gateway, local order book, recorder | 24 h run on 20 symbols: no undetected gaps; L1–L5 match REST snapshots ≥ 99.9%; Parquet files readable |
| **M2** | Universe screener (`tools/screen_once.py` + scheduled) | Produces ranked CANDIDATE list with all metrics; excludes hedge-only & blacklist |
| **M3** | Fair value, quoting engine, paper fill simulator, analytics → **shadow mode** | 7-day shadow run on candidates; daily report with markout per symbol and PnL decomposition; deterministic replay |
| **M4** | Order manager, rate limiter, reconciler, dead-man, risk engine → **demo mode** | 72 h demo run: zero `RECON_MISMATCH` unresolved, all fault-injection tests pass, kill switch < 1 s |
| **M5** | **Live small** (gated) on ≤ 3 symbols promoted from shadow | Runs 7 days within all risk limits; shadow-vs-live gap report produced |
| **M6** | Hedger + beta estimation | Net beta exposure stays within limit in replay and live |
| **M7** | Auto promotion/demotion from markout; multi-level quoting (optional) | Universe rotates automatically with documented decisions in logs |

---

## 13. Open questions for the operator

- Quote size and inventory limits after M5 — scale factor and cadence?
- Enable taker hedging, or post-only only?
- Funding guard: widen vs pull?
- Telegram vs other alert channel?
- Hosting: home server vs VPS (region close to Binance matching engine — latency matters less for us, but stability does).
