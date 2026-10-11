"""Replay harness: deterministic fills, daily report, kill switch/flatten, funding (M3)."""

import sqlite3
from decimal import Decimal

import pytest

from riverbank.analytics.report import build_daily_report
from riverbank.core.config import Config
from riverbank.core.models import RiskState
from riverbank.sim.replay import load_events, run_replay
from riverbank.storage.sqlite import SqliteRepository
from tests.helpers import T0, spec
from tests.replay.synth import write_dataset

SYMS = {"AAAUSDT": 1.0, "BBBUSDT": 2.0}


def specs():
    return {s: spec(s) for s in SYMS} | {"BTCUSDT": spec("BTCUSDT", tick="0.1", step="0.001", min_notional="100")}


def cfg(**kw):
    base = {"quoting": {"sigma_warmup_samples": 20}, "ops": {"sim_latency_ms": 150}}
    for k, v in kw.items():
        base.setdefault(k, {}).update(v)
    return Config.model_validate(base)


@pytest.fixture(scope="module")
def dataset(tmp_path_factory):
    import asyncio

    root = tmp_path_factory.mktemp("data")
    asyncio.run(write_dataset(root, SYMS, T0 + 3600_000, minutes=25, funding_ms=T0 + 3600_000 + 15 * 60_000))
    return root, load_events(root, list(SYMS))


def test_replay_is_deterministic(dataset):
    _, events = dataset
    r1 = run_replay(cfg(), specs(), events, list(SYMS))
    r2 = run_replay(cfg(), specs(), events, list(SYMS))
    assert len(r1.fills) > 20, "expected the synthetic market to produce fills"
    assert r1.digest() == r2.digest()
    v = r1.venue
    assert all(f.is_maker for _, f in r1.fills)
    assert v.orders.placed > 0
    print(f"fills={len(r1.fills)} placed={v.orders.placed} canceled={v.orders.canceled} "
          f"post_only={v.orders.post_only_rejects} rejects={v.orders.rejects}")
    # positions never exceed the risk limit
    for s in SYMS:
        assert abs(float(v.positions.qty(s))) * SYMS[s] <= Config().risk.max_position_usdt * 1.2


def test_latency_changes_outcome_but_stays_deterministic(dataset):
    _, events = dataset
    fast = run_replay(cfg(ops={"sim_latency_ms": 0}), specs(), events, list(SYMS))
    slow = run_replay(cfg(ops={"sim_latency_ms": 500}), specs(), events, list(SYMS))
    assert fast.digest() != slow.digest()
    assert slow.digest() == run_replay(cfg(ops={"sim_latency_ms": 500}), specs(), events, list(SYMS)).digest()


def test_replay_with_repo_produces_daily_report(dataset, tmp_path):
    _, events = dataset
    repo = SqliteRepository(tmp_path / "r.sqlite")
    res = run_replay(cfg(), specs(), events, list(SYMS), repo=repo)
    repo.flush()
    n_db = repo.query("SELECT COUNT(*) FROM fills")[0][0]
    resolved = repo.query("SELECT COUNT(*) FROM fills WHERE mo_60 IS NOT NULL")[0][0]
    repo.close()
    assert n_db == len(res.fills) and resolved == n_db
    path = build_daily_report(tmp_path / "r.sqlite", "2026-10-12", tmp_path / "reports", cfg())
    md = path.read_text()
    assert "### Markout and edge per symbol" in md and "### PnL decomposition" in md
    assert "AAAUSDT" in md and "(n=" in md and "Promotion readiness" in md
    assert (path.parent / "summary.json").exists()
    assert list((path.parent / "charts").glob("markout_*.png"))


def test_paper_funding_applied(dataset):
    _, events = dataset
    res = run_replay(cfg(quoting={"funding_guard_s": 0}), specs(), events, list(SYMS))
    v = res.venue
    paid = sum(float(p.funding) for p in v.positions.positions.values())
    had_pos = any(p.qty != 0 for p in v.positions.positions.values())
    assert paid != 0.0 or not had_pos


def test_kill_switch_halts_cancels_and_flattens(dataset):
    _, events = dataset
    halt_at = events[len(events) // 2].recv_ts

    def setup(engine):
        def hook(now):
            if now >= halt_at and engine.venues["paper"].risk.state is not RiskState.HALTED:
                engine.halt_all("test kill switch")
        engine.timer_hooks.append(hook)

    res = run_replay(cfg(risk={"emergency_flatten": True, "flatten_taker_after_s": 5}), specs(), events,
                     list(SYMS), setup=setup)
    v = res.venue
    assert v.risk.state is RiskState.HALTED
    assert all(p.qty == 0 for p in v.positions.positions.values())
    assert v.flatten_done
    assert not [o for o in v.orders.live_orders() if o.reason != "flatten"]
    after = [f for _, f in res.fills if f.ts > halt_at + 1000]
    assert all(f.reason == "flatten" for f in after)
    assert Decimal(0) == sum((p.qty for p in v.positions.positions.values()), Decimal(0))


def test_report_cli_tables_are_consistent(dataset, tmp_path):
    _, events = dataset
    repo = SqliteRepository(tmp_path / "r.sqlite")
    run_replay(cfg(), specs(), events, list(SYMS), repo=repo)
    repo.flush()
    repo.close()
    conn = sqlite3.connect(tmp_path / "r.sqlite")
    snaps = conn.execute("SELECT COUNT(*) FROM pnl_snapshots WHERE symbol='__TOTAL__'").fetchone()[0]
    uptime = conn.execute("SELECT SUM(quoted_ms), SUM(total_ms) FROM quote_uptime").fetchone()
    conn.close()
    assert snaps > 10 and uptime[1] > 0 and 0 < uptime[0] <= uptime[1]
