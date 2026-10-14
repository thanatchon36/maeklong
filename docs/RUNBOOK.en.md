# RUNBOOK — riverbank (operations manual for system administrators)

_This document is the English translation of [`docs/RUNBOOK.md`](RUNBOOK.md) (Thai). The Thai runbook
is the source of truth; where the two differ, the Thai version governs._

> This document covers shadow / demo / M1 validation (§1–§9) and live mode for M5–M7 (§10)
> **Do not run `live` mode until the M1, M3 and M4 acceptance runs pass and the operator approves** (SPEC §0.1, §11, OPEN_QUESTIONS B1)
> Code blocks live with `LIVE_ACCEPTANCE_APPROVED = False`, and compose intentionally has no service for live
> The detailed acceptance test plan is at `docs/ACCEPTANCE_PLAN.md`

Commonly used terms (kept in English because they match the logs and the code):
`HALTED` = stop everything and wait for a human to order a restart, `REDUCE_ONLY` = only orders that
reduce the position may be sent, `RECON_MISMATCH` = local state does not match the market,
dead-man = a countdown on the Binance side that cancels our orders by itself if the bot disappears

---

## 1. Overview

| Mode | Real orders? | Keys? | Config file | state / DB | kill file | metrics |
|---|---|---|---|---|---|---|
| `shadow` | No (fills are simulated) | None | `config/config.yaml` | `state/riverbank.sqlite` | `./KILL` | `:9108` |
| `demo` | Yes, but with fake money (`demo-fapi.binance.com`) | **demo** keys | `config/demo.yaml` | `state/demo/riverbank.sqlite` | `./KILL.demo` | `:9109` |
| M1 validation | No | None | — | — | — | — |
| `live` (§10) | **Yes, real money** | **production** keys | `config/live.yaml` | `state/live/riverbank.sqlite` | `./KILL` (shared with shadow) | `172.17.0.1:9110` (Docker bridge only §10.4) |

- shadow and demo can run at the same time because they use separate state, kill files, metrics ports
  and Telegram bots
- live **must** run alongside shadow, because symbols are selected from shadow's results
  (`state/riverbank.sqlite` is read read-only)
- Raw market data lives under `data/` (shadow), `data/demo/` (demo) and `data/m1-validation/` (M1)
- Daily reports are generated automatically after midnight UTC at `reports/YYYY-MM-DD/` (shadow) and
  `reports/demo/YYYY-MM-DD/` (demo)
- Every alert is always also written to `logs/alerts.log`, even when Telegram is unavailable

---

## 2. Host setup (one time)

A Linux VPS with a static IP is recommended, close to Tokyo, 2 vCPU / 4 GB RAM / at least 40 GB disk.

```bash
# 1) Base packages
sudo apt-get update && sudo apt-get install -y git tmux sqlite3 chrony
timedatectl            # must show "System clock synchronized: yes"; if the clock drifts, signed requests fail with -1021
# 2) Docker (per the official Docker instructions for that distro) and uv
curl -LsSf https://astral.sh/uv/install.sh | sh
# 3) Code
git clone <repo> ~/maeklong && cd ~/maeklong
uv sync && uv run pytest -q          # everything must pass
# 4) Values for docker compose
cp docker/env.example docker/.env
# Edit docker/.env: RB_UID=$(id -u), RB_GID=$(id -g), GRAFANA_ADMIN_PASSWORD, shadow's Telegram, DEMO_ENV_FILE
```

### 2.1 Secrets

- **Never** put keys in `config/*.yaml`, `docker/.env`, or commit them to git
- Demo keys go in a file outside the repo; then point `DEMO_ENV_FILE` in `docker/.env` at that file

```bash
mkdir -p ~/.riverbank && chmod 700 ~/.riverbank
cat > ~/.riverbank/demo.env <<'EOF'
BINANCE_API_KEY=<demo key>
BINANCE_API_SECRET=<demo secret>
TELEGRAM_BOT_TOKEN=<token of Telegram bot #2>
TELEGRAM_CHAT_ID=<chat id>
EOF
chmod 600 ~/.riverbank/demo.env
```

- The demo account must be in **Single-Asset** mode (if it is Multi-Assets the bot will refuse to
  start) and must have no self-opened positions or open orders
- **Telegram must use 2 bots** — one for shadow, one for demo —
  because if two processes poll messages from the same token they collide (HTTP 409) and a `/kill`
  command would stop both modes at once
  - Create a bot with @BotFather → `/newbot`
  - Send one message to the bot, then read `chat.id` from
    `curl https://api.telegram.org/bot<TOKEN>/getUpdates`

---

## 3. Start / stop / check status

Run every command from the repo root; it is convenient to set this alias:

```bash
alias dc='docker compose -f docker/docker-compose.yml'
```

| Task | Command |
|---|---|
| Start shadow + Prometheus + Grafana | `dc up -d --build` |
| Start demo | `dc --profile demo up -d demo` |
| Start M1 validation (25 h) | `dc --profile m1 up -d m1` |
| Check status / health | `dc ps` |
| Tail live logs | `dc logs -f --tail 100 shadow` (or `demo`, `m1`) |
| Graceful stop (cancels all `rb-*` orders and closes the dead-man before exiting) | `dc stop demo` (waits up to 90 seconds) |
| Pull code updates and restart | `git pull && dc up -d --build shadow` |

- The bot restarts itself when the process dies (`restart: unless-stopped`)
- But if a HALT state is recorded, or a kill file exists, the bot **refuses to start every time**,
  printing `REFUSED: refusing to start: ...` and exiting
  Docker will keep trying to restart it, which is not dangerous; this loop is a signal for a human to
  investigate (see §5)

### 3.1 Running without Docker

```bash
tools/run_shadow.sh                                         # shadow (the script unsets all Binance keys for you)
set -a; source ~/.riverbank/demo.env; set +a; tools/run_demo.sh  # demo
HOURS=25 SYMBOLS=20 tools/run_m1_validation.sh              # M1
```

Run inside `tmux` so the process survives a dropped SSH connection.

### 3.2 macOS notes (Docker Desktop)

- **Do not open `state/*.sqlite` from the host** (with `sqlite3`, `kill_drill.py`,
  `demo_acceptance.py`, etc.) while the bot in the container is still running,
  because SQLite file locks do not work across Docker Desktop's VM.
  During testing on 2026-10-12, a single DB read from the host left the DB **corrupted beyond use**
  (the header became all zeros)
- On macOS, choose one of:
  - Run the bot directly with `uv` (§3.1)
  - Or run every tool inside the container via `dc exec demo python tools/...`
    (the container has no `sqlite3` binary; use `python -c "import sqlite3; ..."` instead)
- On Linux (VPS) bind mounts work normally, because host and container share the same kernel
- If you run long jobs on a laptop, also run `caffeinate -dimsu` so the machine does not sleep

---

## 4. Kill switch (emergency stop: must reach HALTED within 1 second)

Pick any method; all produce the same result: cancel all orders, close the position with reduce-only
limits, and stop permanently.

| Method | shadow | demo | live (§10) |
|---|---|---|---|
| File | `touch KILL` | `touch KILL.demo` | `touch KILL` (**stops both shadow and live**) |
| Signal | `dc kill -s SIGUSR1 shadow` or `kill -USR1 <pid>` | `dc kill -s SIGUSR1 demo` | `kill -USR1 <pid of live>` |
| Telegram | send `/kill` to bot #1 | send `/kill` to bot #2 | send `/kill` to bot #3 |

- The Telegram `/status` command replies with the current status
- Telegram accepts commands only from the configured `TELEGRAM_CHAT_ID`

**If the bot is hung or unresponsive**, use the standalone tools, which work without the bot running:

```bash
set -a; source ~/.riverbank/demo.env; set +a
uv run python tools/cancel_all.py                 # dry run: list rb-* orders still open on demo
uv run python tools/cancel_all.py --execute       # cancel for real
uv run python tools/flatten_all.py                # dry run: show positions
uv run python tools/flatten_all.py --execute      # close positions with reduce-only limits (--taker only in a genuine emergency)
```

If the process dies completely, the Binance-side dead-man cancels our orders by itself within about
30 seconds. Verify with a `cancel_all.py` dry run.

---

## 5. Recovering from HALTED

1. **Do not restart immediately** — always find the root cause first:
   ```bash
   sqlite3 state/demo/riverbank.sqlite \
     "select datetime(ts/1000,'unixepoch'),level,rule,detail,state_after from risk_events order by ts desc limit 20"
   tail -50 logs/alerts.log
   dc logs --since 1h demo | grep -E '"level":"(error|critical)"' | tail -50
   ```
2. Confirm there are no leftover orders or positions on the market
   (`cancel_all.py` / `flatten_all.py` dry runs)
3. Record the cause in `logs/fault-log.md` (UTC date/time, symptom, cause, fix)
4. Clear the HALT state and restart:
   ```bash
   uv run python tools/clear_halt.py --db state/demo/riverbank.sqlite --kill-file KILL.demo   # asks for confirmation
   # shadow: uv run python tools/clear_halt.py   (defaults are state/riverbank.sqlite and KILL)
   dc restart demo
   ```

**Severity levels** (OPEN_QUESTIONS B5):

| Trigger | Effect |
|---|---|
| position / gross / beta / leverage over the cap, liquidation closer than the configured distance, daily loss over 75% of the cap | `REDUCE_ONLY` (returns to OK by itself when everything is below 90% of the cap) |
| daily loss hits the cap, drawdown, kill switch, exception on the order/risk path, HTTP 429/418, user stream down more than 60 seconds while a position is open, recon that cannot be resolved (3 consecutive times) | `HALTED` (a human must order a restart) |

---

## 6. Size-increase policy (per OPEN_QUESTIONS A1)

- The program **never** increases order sizes or risk caps by itself (SPEC §11.5). Every change must
  be made by the operator in config
- Usable only after M5 has passed, and every condition must hold:
  - Increases of at most **×1.5 per change**
  - At most **1 change per 14 days**
  - The symbol must have **≥ 500** live fills with realized edge > 0 at the 1σ level (`mean − 1·SE > 0`)
  - There must be no HALT and no outstanding `RECON_MISMATCH` during that period
- Record every change (date, old value → new value, evidence) in the commit message of the config
  commit

---

## 7. Daily tasks (about 5 minutes)

1. Open Grafana and walk through this list:
   - risk state must be OK
   - bot must be UP
   - 24 h recon mismatch must be 0
   - rate-limit must be below 60%
   - Book OK must be green almost all the time
2. `tail -20 logs/alerts.log` to see whether anything is wrong
3. Check that yesterday's report was generated: `ls reports/$(date -u -d yesterday +%F)/`
4. Check disk space: `df -h . && du -sh data` (raw data is kept for 30 days, then deleted automatically)
5. demo/live only: check there are no `apiTradingStatus` warnings (Binance's own indicators above 70%
   of their threshold).
   If it trips, it means orders are being cancelled too often; consider raising `quoting.requote_ticks`

### Opening Grafana / Prometheus from your own machine

Both bind only to the VPS's localhost; you must connect through an SSH tunnel:

```bash
ssh -L 3000:127.0.0.1:3000 -L 9090:127.0.0.1:9090 <user>@<vps>
# then open http://localhost:3000  (user: admin, password as set in GRAFANA_ADMIN_PASSWORD)
```

The `riverbank` dashboard is set as the home page; use the `bot` selector at the top to switch between
shadow, demo and live.
(live runs on the host with `uv`, so Prometheus scrapes it via `host.docker.internal:9110`; if live is
not running it shows as down, which is normal)

---

## 8. Common incidents and how to handle them

| Symptom | Likely cause | What to do |
|---|---|---|
| Container restart loop with `REFUSED: refusing to start` | a recorded HALT, or a `KILL` / `KILL.demo` file exists | follow §5 |
| `REFUSED` about Multi-Assets or position mode (demo) | account configured incorrectly | switch the account to Single-Asset, close self-opened positions, then restart |
| HALTED from 429 / 418 | sending requests over the rate limit | **do not restart immediately** — wait at least `Retry-After` (minimum 60 seconds). Repeated 418s make the IP ban longer each time (up to 3 days). Find the cause in the logs first |
| `RECON_MISMATCH` | local state does not match the market | the bot cancels that symbol's orders and adopts the market state itself. 3 consecutive occurrences HALT the bot; check `recon_events` in the DB |
| disconnect alert for more than 30 seconds | network trouble | meanwhile the bot pulls its orders; when reconnected it resyncs and reconciles before quoting again. If it happens often, check the VPS network |
| Book OK stuck at 0 for a single symbol | that symbol is illiquid (STALE) or data has gaps | briefly is normal; if it persists, consider adding the symbol to `config/blacklist.yaml` |
| health is `unhealthy` | the metrics port is not answering | check `dc logs`; if the process is hung use `dc restart` (safe because the Binance-side dead-man protects the orders) |
| disk almost full | raw data piling up | lower `ops.raw_retention_days` or move `data/` to another disk. Never delete `state/` |

---

## 9. Acceptance testing (M1 / M3 / M4)

See `docs/ACCEPTANCE_PLAN.md` for the detailed steps, commands and pass/fail criteria. Summary of the
commands when using Docker:

```bash
dc --profile m1 up -d m1                                   # M1: report at reports/m1-validation/report.md
dc up -d shadow                                            # M3: run for 7+ days, then:
uv run python tools/shadow_acceptance.py --start D0 --end D6
dc --profile demo up -d demo                               # M4: run 72 h + fault injection + kill drill, then:
uv run python tools/demo_acceptance.py --db state/demo/riverbank.sqlite
```

---

## 10. Live mode (M5–M7) — real money

> Every step in this section may be used **only after** the M1, M3 and M4 acceptance runs have passed
> and the operator has approved (OPEN_QUESTIONS B1).
> Until then, do not skip past step 10.1.

### 10.1 Unlocking live (one time, with evidence)

1. Verify that all 3 passing reports exist: `reports/m1-validation/report.md`,
   `reports/m3-acceptance/report.md`, `reports/m4-acceptance/report.md`
2. Change `src/riverbank/orders/live_venue.py` from `LIVE_ACCEPTANCE_APPROVED = False` to `True`
3. Commit with links or filenames of all 3 reports in the commit message, and update
   OPEN_QUESTIONS B1 to record the approval date
4. `uv run pytest -q` must pass completely (the test
   `test_live_selection_stays_blocked_until_acceptance_is_approved` will fail after the unlock; fix
   that test in the same commit so it checks the flag is `True` as approved)

Even after unlocking, the bot must still pass every other gate: `MODE=live`, `LIVE_CONFIRM`, and the
startup checks (§10.4).

### 10.2 Production account and keys

- The Futures account must be **Single-Asset** and **one-way**, and must have no self-opened positions
  or orders
- Deposit only money you can afford to lose entirely; the risk caps in `config/live.yaml` are fixed
  USDT numbers and do not scale with the balance
- Create a new API key just for this bot:
  - Enable only **Enable Futures** (and Reading)
  - **Do not enable Withdrawals** (the bot will refuse to start if enabled)
  - **IP-restrict it** to the VPS's static IP (the bot will refuse to start otherwise)
- Store the key in a file outside the repo, and use Telegram bot #3 (separate from shadow and demo):

```bash
cat > ~/.riverbank/live.env <<'EOT'
BINANCE_API_KEY=<production key>
BINANCE_API_SECRET=<production secret>
TELEGRAM_BOT_TOKEN=<token of Telegram bot #3>
TELEGRAM_CHAT_ID=<chat id>
EOT
chmod 600 ~/.riverbank/live.env
```

### 10.3 Choosing symbols to trade (allow-list)

live quotes only symbols that pass **every** condition (OPEN_QUESTIONS B10, B11, B21):

- it is in the universe the screener has selected at that time
- in shadow it has at least `min_fills_for_promotion` (300) fills, and its 60-second realized edge
  passes `mean − 1·SE > 0`
- it is in `universe.live_symbols` in `config/live.yaml` (unless `auto_promote: true` is set, which is
  **not recommended**)
- it is not inside the post-demotion cooldown (24 h), and after a demotion there must be a fresh set
  of shadow results dated after the demotion date
- the total stays within `max_active_symbols` (3)

To see which symbols are ready: open the "Promotion readiness" section in shadow's daily report
(`reports/YYYY-MM-DD/report.md`),
then put the names you want into the allow-list, e.g. `live_symbols: [DOGEUSDT, XRPUSDT]`
**If the allow-list is empty, live will not quote anything at all.**

### 10.4 Starting live

live still has no service in docker compose; run it with `uv` in `tmux` on the Linux VPS (the same
machine as shadow).
**Do not run live on macOS Docker Desktop**, because live must read shadow's DB, which cannot cross
the VM (§3.2).

```bash
tmux new -s live
cd ~/maeklong
set -a; source ~/.riverbank/live.env; set +a
MODE=live LIVE_CONFIRM=I_UNDERSTAND_THE_RISK uv run python -m riverbank.main --config config/live.yaml
```

At startup the bot checks in this order and **refuses to start** (`REFUSED`) if any check fails:

| Check | If it fails |
|---|---|
| `LIVE_ACCEPTANCE_APPROVED` | not yet unlocked → do §10.1 |
| `MODE=live` and `LIVE_CONFIRM` are correct | set the env vars completely |
| no pending HALT in `state/live/` and no `KILL` file | follow §5 |
| `universe.shadow_db` is found and is not live's own DB | shadow must be running, and live must use `config/live.yaml` |
| machine clock drift within half of recvWindow | check `timedatectl` / chrony |
| key has no withdrawal permission, is IP-restricted, Futures enabled | fix on the API Management page |
| Single-Asset, one-way, actual maker fee not above the config value | fix the account, or `account.fee_maker_bps` |
| ISOLATED + leverage 2 can be set on every symbol | symbols that cannot be set are dropped (see log `symbol_setup_failed`) |

On successful startup you will see the log `live_symbols_selected` (the symbols chosen) and
`real_venue_ready`

**live's metrics port (one time, before starting live):**
The metrics contain position and equity data. The internet must not reach live, so it binds only the
Docker bridge IP (`ops.metrics_addr: 172.17.0.1` in `config/live.yaml`), which Prometheus in the
container can reach via `host.docker.internal`

1. Check `docker0`'s IP matches the config: `ip -4 addr show docker0`. If it is not `172.17.0.1`,
   change `ops.metrics_addr`.
   If binding fails, live still trades but has no metrics and sends a `metrics_unavailable` alert
2. Set the firewall so containers can reach this port (and as a second layer of protection in case
   the bind is wrong):

```bash
sudo ufw default deny incoming
sudo ufw allow OpenSSH                                          # must come before enable, or SSH will drop
sudo ufw allow from 172.16.0.0/12 to any port 9110 proto tcp    # Docker network only
sudo ufw enable && sudo ufw status
curl -s --max-time 3 http://<VPS public IP>:9110/metrics || echo "closed from outside (correct)"   # run from another machine
ss -ltn | grep 9110                    # must show 172.17.0.1:9110, not 0.0.0.0:9110
```

Check that Prometheus sees live: open `http://localhost:9090/targets` through the SSH tunnel (§7);
`bot="live"` must be UP.

**Start with a single symbol first**, watch the first hour closely, then extend the allow-list
(live must be restarted for the new config to take effect).

Graceful stop: press `Ctrl-C` in the tmux (the bot cancels all `rb-*` orders and closes the dead-man
before exiting).
**Open positions are not closed automatically.** To close them, use the kill switch (§4) or
`flatten_all.py` below.

### 10.5 What the bot does by itself during live

- **shadow twin:** a paper venue named `shadow` quotes the same symbols as live in the same process,
  used for the gap report.
  If the twin enters HALTED it does not affect live and does not block restarts, but the kill switch
  stops both
- **hedger (M6):** if net beta exposure exceeds `hedge_threshold_usdt` (25) it sends post-only hedge
  orders.
  With the defaults it hedges with **ETHUSDT**, because BTCUSDT's min notional (100 USDT) exceeds
  `max_position_usdt` (50).
  The hedger only runs while the risk state is OK; if no instrument is usable you will see
  `hedge_too_coarse` in the log, which is still safe because the risk engine enforces
  `max_net_beta_exposure_usdt`
- **beta:** recomputed every hour from 3 days of 1-minute klines (log `beta`); if the fetch fails the
  previous value is kept
- **universe rotation (M7):** every 5 minutes
  - demotes symbols whose live edge over the latest 500 fills has `mean + 1·SE < 0`, then a 24 h
    cooldown
  - symbols that fall out of the screener's universe go back to CANDIDATE
  - when a slot frees up, new symbols are promoted per the §10.3 rules, with ISOLATED + leverage set
    on the exchange before quoting starts
  - removed symbols quote only the position-reducing side until flat
  - every change is recorded in `universe_decisions`, logged as `universe_decision`, and sent as an
    INFO alert
- **No automatic size increases** — every size change must follow §6

### 10.6 Additional daily tasks for live

1. Do all of §7 (select `bot` = `live` in Grafana).
   If Grafana shows live as down while live is running, check the firewall per §10.4.
   You can also check directly with Telegram `/status` (bot #3) or
   `curl -s 172.17.0.1:9110/metrics | grep -E "^rb_(risk_state|equity_usdt)"`
2. Open `reports/live/YYYY-MM-DD/report.md` and check:
   - **Shadow-vs-live gap** section: if the edge gap is very negative (beyond 2·SE), or live's fill
     rate is much lower than shadow's, the paper fill model is too optimistic — be careful before
     scaling up
   - **Universe decisions** section: yesterday's promote/demote actions
   - PnL decomposition and the hedge column
3. Look at the gap report over several days:
   ```bash
   uv run python -m riverbank.analytics.gap --db state/live/riverbank.sqlite --start 2026-10-20 --end 2026-10-26
   ```
4. Look at the universe rotation history:
   ```bash
   sqlite3 state/live/riverbank.sqlite \
     "select datetime(ts/1000,'unixepoch'),symbol,from_state,to_state,reason,n,round(mean,2),round(se,2) from universe_decisions order by id desc limit 20"
   ```

### 10.7 Emergency stop and standalone tools for live

Always use the kill switch per §4 first. If the bot is hung or dead:

```bash
set -a; source ~/.riverbank/live.env; set +a
export MODE=live LIVE_CONFIRM=I_UNDERSTAND_THE_RISK
uv run python tools/cancel_all.py --live                  # dry run: rb-* orders still open on production
uv run python tools/cancel_all.py --live --execute        # cancel for real
uv run python tools/flatten_all.py --live                 # dry run: positions
uv run python tools/flatten_all.py --live --execute       # close with reduce-only limits (--taker genuine emergencies only)
```

To recover from HALTED, follow §5 but with live's DB:

```bash
sqlite3 state/live/riverbank.sqlite \
  "select datetime(ts/1000,'unixepoch'),level,rule,detail,state_after from risk_events order by ts desc limit 20"
uv run python tools/clear_halt.py --db state/live/riverbank.sqlite --kill-file KILL   # asks for confirmation
```

Warning: `clear_halt.py` deletes the `KILL` file, which shadow shares. If shadow has its own recorded
HALT, it must be cleared separately with `tools/clear_halt.py` (the defaults).

### 10.8 M5–M7 acceptance testing

| Milestone | Criteria (SPEC §12) | Where to look |
|---|---|---|
| M5 | run live 7 days without breaching any risk cap, and produce a shadow-vs-live gap report | `risk_events` must contain no HALT, 7 daily reports, `analytics.gap` over 7 days |
| M6 | net beta exposure within caps in both replay and live | replay: `tests/replay/test_replay_hedge.py`; live: `risk_events` has no lingering soft breach `net_beta_exposure` |
| M7 | universe rotates automatically, with decision records in the log | `universe_decisions` and the `universe_decision` log |
