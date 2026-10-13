# riverbank

[![CI](https://github.com/thanatchon36/maeklong/actions/workflows/ci.yml/badge.svg)](https://github.com/thanatchon36/maeklong/actions/workflows/ci.yml)
![Python 3.12](https://img.shields.io/badge/python-3.12-blue)
![mypy strict](https://img.shields.io/badge/mypy-strict%20on%20risk%20path-2a6db2)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

> The repo is named after the Mae Klong river; the bot that lives on it is `riverbank`.

A multi-symbol market maker for Binance USDⓈ-M perpetual futures. It quotes both sides on a small,
screened set of contracts. It also manages inventory, hedges the shared beta exposure, and pulls quotes
when order flow turns toxic. It only scales a symbol after its realized edge per fill has been measured,
first in simulation and then with real orders:

```
realized edge per fill = captured half-spread + markout(h) − maker fee  > 0
```

> **Status (2026-10-12).** Code for milestones M0–M7 is done and tested, and the bot is running in demo
> (fake money). Live trading is **blocked in code** (`LIVE_ACCEPTANCE_APPROVED = False`) until the M1,
> M3 and M4 acceptance runs pass and the operator approves (`docs/OPEN_QUESTIONS.md` B1).

## Modes

| Mode | Orders | Keys | Config |
|---|---|---|---|
| `shadow` (default) | simulated fills on real public market data | none | `config/config.yaml` |
| `demo` | real orders on `demo-fapi.binance.com`, fake money | demo keys | `config/demo.yaml` |
| `live` | real orders, **real money** | production keys | `config/live.yaml` |

Live needs all of the following:
- `MODE=live` and `LIVE_CONFIRM=I_UNDERSTAND_THE_RISK` in the environment (config alone can never enable it)
- the code unlock above
- startup checks: the key can't withdraw, is IP-restricted and has futures enabled; the account is in
  Single-Asset and one-way mode; isolated margin and leverage are set per symbol
- symbols promoted from shadow evidence that are also on the operator's allow-list

## Quick start

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run pytest -q                                   # unit, integration (in-process fake exchange), replay
uv run python tools/show_specs.py                  # symbol specs from the public exchangeInfo
uv run python tools/screen_once.py                 # one screener pass: ranked CANDIDATE symbols
tools/run_shadow.sh                                # shadow mode (unsets any Binance keys)
```

For long-running use, Docker Compose runs the bot plus Prometheus and Grafana:

```bash
cp docker/env.example docker/.env                  # set GRAFANA_ADMIN_PASSWORD, RB_UID/RB_GID, DEMO_ENV_FILE
docker compose -f docker/docker-compose.yml up -d --build                    # shadow + monitoring
docker compose -f docker/docker-compose.yml --profile demo up -d demo        # demo (keys in DEMO_ENV_FILE)
```

Grafana is at `http://localhost:3000` (dashboard `riverbank`). Prometheus and Grafana bind to
127.0.0.1 only. Keys come only from the environment or a secret file outside git, never from config.

## Emergency stop

Any of the following puts the bot in `HALTED` within one second: it cancels every order, flattens with
reduce-only orders and refuses to restart until a human clears it.

- `touch KILL` (or `KILL.demo` for demo)
- `SIGUSR1`
- `/kill` to the Telegram bot

`tools/cancel_all.py` and `tools/flatten_all.py` work without the bot running; both are dry runs by
default.

## How it works

```
market data (WS + REST snapshots) ─► order books ─► fair value ─► quoting engine ─┐
universe screener ─► CANDIDATE (shadow) ─► ACTIVE (live) ─► DEMOTED                 ├─► risk engine (veto) ─► order manager ─► exchange
beta estimator ─► hedger (post-only, BTC/ETH) ────────────────────────────────────┘
paper fill simulator · markout analytics · daily report · reconciler · exchange dead-man
```

- **Engine** (`src/riverbank/engine.py`): one single-threaded event dispatcher shared by replay, shadow,
  demo and live, so a given input sequence always produces the same orders and fills.
- **Risk** (`risk/`): checks every order before it is sent; soft limit breaches move the bot to
  `REDUCE_ONLY` and hard breaches to `HALTED`; a HALT is persisted. Limits are only ever changed by
  hand in config.
- **Universe** (`universe/`): screens symbols by spread, fees, volume and a toxicity proxy. Promotion
  needs ≥ 300 shadow fills with realized edge mean − 1·SE > 0; demotion is automatic in live.
- **Shadow twin**: demo and live also run a paper venue on the same symbols; the shadow-vs-live gap
  report shows how optimistic the fill model is.

## Repository layout

| Path | Contents |
|---|---|
| `SPEC.md` | the specification (milestones, acceptance criteria, safety rules) |
| `src/riverbank/` | `core` · `exchange` · `marketdata` · `universe` · `features` · `quoting` · `orders` · `risk` · `hedge` · `sim` · `analytics` · `storage` · `ops` |
| `config/` | `config.yaml` (shadow), `demo.yaml`, `live.yaml`, `blacklist.yaml` |
| `tools/` | run scripts, replay, screener, acceptance checks, kill drill, standalone cancel/flatten |
| `tests/` | `unit/`, `integration/` (fake Binance), `replay/` (synthetic recorded data) |
| `docker/` | Dockerfile, compose, Prometheus, Grafana dashboard |
| `docs/` | see below |

## Documentation

| File | What it's for |
|---|---|
| `docs/PROGRESS.md` | what is built per milestone, test and smoke results, pending runs |
| `docs/RUNBOOK.md` | operations manual in Thai: start/stop, kill switch, recovery, daily checks, live (§10) |
| `docs/ACCEPTANCE_PLAN.md` | how to run and judge the M1/M3/M4 acceptance runs |
| `docs/OPEN_QUESTIONS.md` | every design decision taken where the spec was silent (most conservative option) |
| `docs/DISCREPANCIES.md` | places where the spec differs from the current Binance docs (the docs win) |

## Roadmap

Open work is tracked as [issues](https://github.com/thanatchon36/maeklong/issues): the M1/M3/M4 wall-clock
acceptance runs, deploying the post-M4 fixes, the M5 sign-off and the live parts of M6/M7.

## Development

```bash
uv run pytest -q
uv run ruff check .
uv run mypy src/riverbank/core src/riverbank/risk src/riverbank/orders src/riverbank/hedge   # strict
```

Tests never sign requests for production hosts: a fixture enforces it. Each milestone is one commit
(`M0:` … `M7:`).

## Disclaimer

This is a research project, not financial advice. Market making on leveraged perpetual futures can lose more
than the margin you post. Live trading is disabled in code; if you enable it, you do so at your own risk.
See [LICENSE](LICENSE) (MIT, no warranty) and [SECURITY.md](SECURITY.md).
