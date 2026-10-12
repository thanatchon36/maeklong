# ACCEPTANCE PLAN — M1 (24 h), M3 (7-day shadow), M4 (72 h demo)

Written 2026-10-12 against commit `592a541`. Code for M0–M4 is done (129 tests green). **None of the
wall-clock acceptance runs have been done.** Per SPEC §0.1 / §12, M5 does not start until all three pass and
the operator approves.

Safety: none of these runs places a real-money order. M1 and M3 call only public production endpoints with no
keys. M4 places orders only on `demo-fapi.binance.com` using demo keys.

---

## 0. What the operator must prepare

| # | Item | Needed for | Details |
|---|---|---|---|
| P1 | **Host** that stays up for ~8 days | all | Recommended: Linux VPS, static IP, Tokyo region (OPEN_QUESTIONS A5), 2 vCPU / 4 GB RAM / ≥ 40 GB disk. Recording 20 symbols for 25 h plus 7 days of shadow data is roughly 10–30 GB; check `du -sh data` after the first day. A laptop works only if it never sleeps (`caffeinate -dimsu` on macOS) and the network is stable. A reconnect storm counts against M1. |
| P2 | `uv` + Python 3.12 on the host | all | `curl -LsSf https://astral.sh/uv/install.sh \| sh`, then `uv sync && uv run pytest -q` must show 129 passed. Docker is optional until step 3 (compose) lands. |
| P3 | **Clock sync** | all | `timedatectl` should show `System clock synchronized: yes` (chrony/ntpd). Signed demo calls fail with `-1021` if the clock drifts. |
| P4 | **Binance demo account + API key** | M4 | Log in at the Binance Futures demo-trading site, create an **HMAC** API key with futures trading enabled. Demo keys differ from live keys. Set the account to **Single-Asset** margin mode (startup refuses Multi-Assets, DISCREPANCIES #23) and close any manual positions/orders first. Confirm the demo wallet has USDT (≥ 300). |
| P5 | **Two Telegram bots** (one for shadow, one for demo) | M3, M4 | @BotFather → `/newbot` twice → two tokens. Send any message to each bot, then `curl https://api.telegram.org/bot<TOKEN>/getUpdates` to read `chat.id`. **Why two:** both processes long-poll `getUpdates`, and two pollers on one token conflict (HTTP 409). A `/kill` would also halt both bots. Telegram is optional: without it, alerts still go to `logs/alerts.log`. |
| P6 | ~~Separate kill file for demo~~ **done** | M3 + M4 in parallel | `config/demo.yaml` now sets `ops.kill_file: KILL.demo`, so a demo kill drill cannot HALT the parallel shadow run. |
| P7 | `tmux` (or systemd units) | all | Each run must survive SSH disconnects. |

### Environment variables

| Var | M1 | M3 shadow | M4 demo |
|---|---|---|---|
| `SYMBOLS` (default 20), `HOURS` (default 24), `OUT`, `DATA_DIR` | used | — | — |
| `MODE` | — | set to `shadow` by `run_shadow.sh` | set to `demo` by `run_demo.sh` |
| `CONFIG` | — | default `config/config.yaml` | default `config/demo.yaml` |
| `BINANCE_API_KEY`, `BINANCE_API_SECRET` | — | **unset** by the script | **required: demo key** |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | — | bot #1 (optional) | bot #2 (optional) |
| `LIVE_CONFIRM` | never set | never set | never set |

Put secrets in a `chmod 600` file outside the repo (e.g. `~/.riverbank/demo.env`) and `set -a; source …; set +a`
inside the tmux pane. `.env` is gitignored, but keeping secrets out of the repo tree is safer.

### Timeline (all three can overlap)

```
Day 0  ── start M1 (25 h) ── start M3 shadow (runs ≥ 8 calendar days, see M3 note)
Day 0+ ── start M4 demo as soon as demo keys exist (72 h + ~1 h of drills)
Day 1  ── M1 report
Day 3–4 ─ M4 drills + report
Day 8  ── M3 report → operator review of all three → M5 decision
```

### Docker instead of tmux

`docker/docker-compose.yml` runs the same processes; see `docs/RUNBOOK.md` §3. Equivalents:
`dc --profile m1 up -d m1` (M1, 25 h), `dc up -d` (shadow + Prometheus + Grafana),
`dc --profile demo up -d demo` (demo; keys from `DEMO_ENV_FILE`). Logs: `dc logs <service>` instead of the
`tee` files below. Process freeze/kill for F1/F2: `dc pause demo` / `dc unpause demo` and `dc kill demo`.
The acceptance tools and `sqlite3` queries below run on the host against the bind-mounted `state/`. That is
fine on Linux. **On macOS Docker Desktop it corrupted the DB in testing**: run them via `dc exec` instead
(RUNBOOK §3.2). The kill-file drill: `dc exec demo python tools/kill_drill.py --db state/demo/riverbank.sqlite --kill-file KILL.demo`.

Rate-limit budget: M1 + shadow together use well under 25 % of the production `REQUEST_WEIGHT 2400/1M`.
Demo is a different host with its own limits. If production returns a 429/418, **stop M1 first**, because
shadow is the more expensive run to restart.

---

## 1. M1: market data, 24 h on 20 symbols

**Command** (tmux pane `m1`):

```bash
cd ~/maeklong
HOURS=25 SYMBOLS=20 tools/run_m1_validation.sh 2>&1 | tee logs/m1-run.log
```

`HOURS=25` rather than 24: `ops.ws_roll_hours` is 23, so the run sees at least one make-before-break roll *and*
a full 24 h measured window. Interim report: `reports/m1-validation/report.interim.md` (every 10 min).

**Pass (SPEC §12): all must hold in `reports/m1-validation/report.md` / `summary.json`:**

| Check | Criterion |
|---|---|
| Duration | `duration_ok_24h: PASS` (hours ≥ 24) |
| No undetected gaps | `no_undetected_gaps: PASS` (`undetected_mismatches == 0`) |
| L1–L5 match | `l1_l5_match_ge_99_9: PASS` (`match_pct ≥ 99.9`) |
| Parquet | `parquet_readable: PASS` (`parquet_files > 0`, 0 bad files) |

**Also check (not SPEC criteria, but they tell you whether the result means anything):**
- `rolls ≥ 1`. If 0, the 24 h-lifetime path was never exercised; rerun or extend.
- `detected_gaps` and `reconnects` are reported, not hidden. A handful is normal. Hundreds point to a network
  problem on the host; rerun before accepting.
- `snapshot_errors == 0`, `recorder_write_errors == 0`, `parse_errors == 0`.

**Fail →** keep `data/m1-validation/` (raw depth is recorded with `--record-depth`) and the log. Do not delete
them. They are the reproduction.

---

## 2. M3: 7-day shadow run

**Before starting:** `ls state/riverbank.sqlite KILL 2>/dev/null` should print nothing, or only a sqlite left
over from the smoke runs. If smoke-run HALT records exist, run `uv run python tools/clear_halt.py --yes`.
Better: move the old `state/`, `data/` and `reports/` aside so the acceptance window contains only this run.

**Command** (tmux pane `shadow`):

```bash
cd ~/maeklong
set -a; source ~/.riverbank/shadow.env; set +a     # Telegram bot #1 only, no Binance keys
tools/run_shadow.sh 2>&1 | tee -a logs/shadow-run.log
```

Daily reports appear in `reports/YYYY-MM-DD/report.md` after each UTC midnight. Metrics: `curl -s localhost:9108/metrics | head`.

**Daily check (2 min):** the report exists for yesterday; `grep -c '"level":"error"' logs/shadow-run.log` is not
growing fast; `sqlite3 state/riverbank.sqlite "select ts,rule,state_after from risk_events order by ts desc limit 5"`
shows no `HALTED`. A HALT stops the run because restart is refused. Investigate, `clear_halt.py`, restart, and record it.

**After 7 full UTC days** (start day D0 = first full UTC day of the run, so the bot must already be running
before D0 00:00 UTC and still be running after D6 24:00 UTC):

```bash
uv run python tools/shadow_acceptance.py --start D0 --end D6     # e.g. --start 2026-10-13 --end 2026-10-19
```

`duration_ge_7d` passes when first fill → last fill spans 7 days minus at most 1 h at the window edges
(`EDGE_TOLERANCE_MS`). A run that stopped early, or started after D0 began, does not pass.

**Pass (SPEC §12), from `reports/m3-acceptance/report.md`:**

| Check | Criterion |
|---|---|
| 7-day run | `duration_ge_7d: PASS` |
| Daily reports | `daily_reports: PASS`, **and** open 2–3 of them by hand to confirm each one shows markout per symbol at 1/5/30/60/300 s with `n_fills`, mean and SE, plus the PnL decomposition (spread, inventory, fees, funding) |
| Deterministic replay | `deterministic_replay: PASS` (identical digests) |

**Also record (not pass/fail, but input for M5):**
- the per-symbol `READY / not ready` table: which candidates have ≥ 300 fills with `mean − 1·SE > 0` at 60 s;
- number of restarts and HALTs during the 7 days (target 0 unplanned);
- disk used by `data/`.

Shadow PnL is an upper bound (SPEC §3). A green M3 does not mean the strategy makes money.

---

## 3. M4: 72 h demo + fault injection

### 3a. First signed contact (≈ 10 min, before committing to 72 h)

```bash
set -a; source ~/.riverbank/demo.env; set +a   # demo BINANCE_API_KEY/SECRET + Telegram bot #2
uv run python tools/cancel_all.py              # demo, dry run → lists open rb-* orders (expect none)
uv run python tools/flatten_all.py             # demo, dry run → lists positions (expect none)
tools/run_demo.sh 2>&1 | tee -a logs/demo-run.log
```

Expect in the log, in order: time sync, `real_venue_ready` (symbols DOGEUSDT/XRPUSDT/ADAUSDT, no unexpected
`excluded`), a `/private` user-stream connect, then orders and post-only rejects (`-5022`, INFO level). Any
startup refusal (Multi-Assets mode, hedge mode, higher fee) → fix the demo account and retry. This is the first
time the code has ever talked to a signed endpoint, so expect to find something here.

If 3a is clean, leave it running. That starts the 72 h.

### 3b. Fault injection on the real demo venue (during the 72 h, ≥ 1 h apart)

`PID=$(pgrep -f 'riverbank.main --config config/demo.yaml')`

| # | Fault (SPEC §10) | How | Pass |
|---|---|---|---|
| F1 | **Process freeze** (WS + user stream + heartbeats all stop) | `kill -STOP $PID; sleep 35; uv run python tools/cancel_all.py; sleep 10; kill -CONT $PID` | The dry-run at t+35 s lists **0 open rb-\* orders** (exchange dead-man fired). After `CONT`: log shows books INVALID → resync, user stream reconnects, reconciliation runs, quoting resumes; a disconnect alert in `logs/alerts.log`; no unresolved RECON_MISMATCH. Keep the pause < 60 s: user stream down > 60 s with exposure is a designed HALT (OPEN_QUESTIONS B5). |
| F2 | **Process kill** | `kill -9 $PID; sleep 35; uv run python tools/cancel_all.py` → expect 0 orders; then restart `tools/run_demo.sh` | 0 rb-\* orders after 35 s; restart succeeds (no HALT record for SIGKILL); startup cancels stale rb-\*, adopts position, `real_venue_ready` again. |
| F3 | **Network loss** (optional, Linux, needs root) | `sudo iptables -I OUTPUT -p tcp --dport 443 -m owner --uid-owner $(id -u) -j DROP; sleep 40; sudo iptables -D OUTPUT 1` | Same as F1, plus no crash, no 418. Run it under a dedicated user, or you will cut your own other HTTPS too. |
| F4 | **503-unknown, 429/418, user-stream-only drop** | **Not induced on the real venue.** 429s escalate to IP bans, and 503s cannot be triggered on demand. Covered by `tests/integration/test_demo_fault_injection.py` against the in-process fake exchange, which `demo_acceptance.py` runs. | Suite green. **Operator sign-off needed:** this is a deviation from SPEC §10's "fault injection (demo)". |

Note the wall-clock time of each fault in `logs/fault-log.md`. If a fault ends in HALT, the 72 h clock keeps
running (the report measures first → last order), but investigate before `clear_halt.py`.

### 3c. Kill-switch drills (after ≥ 72 h; each one HALTs the bot)

Run them in this order, so the recorded KILL-file drill comes last:

```bash
# 1. Telegram: send /kill to bot #2, note the time (UTC, ms), then:
sqlite3 state/demo/riverbank.sqlite "select ts,rule,detail from risk_events where state_after='HALTED' order by ts desc limit 1"
uv run python tools/cancel_all.py                       # expect 0 rb-* orders
uv run python tools/clear_halt.py --db state/demo/riverbank.sqlite --yes && tools/run_demo.sh   # run ~5 min
# 2. SIGUSR1
date +%s%3N; kill -USR1 $(pgrep -f 'riverbank.main --config config/demo.yaml')
#    → same sqlite query, then clear_halt + restart (~5 min)
# 3. KILL file (recorded for the report; KILL.demo leaves the shadow run untouched)
uv run python tools/kill_drill.py --db state/demo/riverbank.sqlite --kill-file KILL.demo
```

Pass: HALTED < 1 s after the trigger for all three. For Telegram, measure from when the message is sent, which
includes Telegram latency. If that is > 1 s, record it; the file and signal paths are the hard guarantee. After
each HALT, `cancel_all.py` dry run shows 0 orders and `flatten_all.py` dry run shows 0 positions. With
`emergency_flatten: false`, flattening uses reduce-only limit orders and can take a little while.

### 3d. Report

```bash
uv run python tools/demo_acceptance.py --db state/demo/riverbank.sqlite
```

**Pass (SPEC §12), from `reports/m4-acceptance/report.md`:**

| Check | Criterion |
|---|---|
| 72 h | `duration_ge_72h: PASS` |
| Recon | `zero_unresolved_recon_mismatch: PASS` |
| Fault injection | `fault_injection_tests_pass: PASS` **and** F1, F2 (and F3 if run) passed by hand per 3b |
| Kill switch | `kill_switch_lt_1s: PASS` (KILL file) and Telegram + SIGUSR1 measured per 3c |

**Also required before M5 (operator judgement):**
- zero 429/418 in `logs/demo-run.log` (`grep -E '"(429|418)"|rate_limit_halt'`);
- no unplanned HALT in `risk_events`;
- `apiTradingStatus` never alerted ≥ 70 % of a trigger (`grep apiTradingStatus logs/demo-run.log`). If it did,
  the requote rate is too aggressive for live, whatever the result of everything else;
- after the final drill: 0 open rb-\* orders, 0 positions on demo.

### 3e. M7 rotation on the demo venue (after 3c, not part of the M4 verdict)

The M4 run keeps its fixed `static_symbols` (OPEN_QUESTIONS B24), so `RealVenueRuntime.rotate_once`
(`orders/live_venue.py`) has never run against a real exchange: only unit tests and replay cover it. Before
M7 goes live, exercise it once on demo for 1–2 h. **Needs a small code change first** (not built yet): a
config flag, default off, that adds `_rotation_loop` to the demo coroutines. Then, with a separate demo config:

- `universe.shadow_db` points at the M3 shadow DB (read-only), so promotion uses real production evidence.
  On macOS Docker Desktop, a second process reading a live SQLite DB through the bind mount is the setup that
  corrupted a DB before (RUNBOOK §3.2): run 3e after M3 ends or on a copy of the DB;
- `universe.live_symbols` lists a few READY symbols that the demo exchange also lists;
- `screen_interval_s` stays 300.

**Check:** at least one promotion configures the symbol on the exchange (leverage, margin) and starts quoting;
at least one demotion winds the position down to flat and cancels its orders; `universe_decisions` rows match
the `universe` alerts; recon mismatches stay 0. Demo PnL is still meaningless.

---

## 4. Sign-off checklist (M5 gate)

- [ ] `reports/m1-validation/report.md`: 4 × PASS, `rolls ≥ 1`
- [ ] `reports/m3-acceptance/report.md`: 3 × PASS; daily reports spot-checked; READY list noted
- [ ] `reports/m4-acceptance/report.md`: 4 × PASS; F1/F2 notes in `logs/fault-log.md`; 3 kill paths < 1 s
- [ ] F4 deviation (503/429 only in the fake exchange) accepted by operator
- [ ] `docs/PROGRESS.md` "Pending live-time validation" table updated with dates and results
