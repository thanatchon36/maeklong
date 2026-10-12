"""M6 acceptance in replay: with the hedger, net beta exposure stays within the risk limit (and the
quoter is blocked by the net-beta limit far less often than without it)."""

import asyncio

import pytest

from riverbank.core.config import Config
from riverbank.hedge.beta import BetaTable
from riverbank.hedge.hedger import Hedger
from riverbank.sim.replay import load_events, run_replay
from tests.helpers import T0, spec
from tests.replay.synth import write_dataset

SYMS = {"AAAUSDT": 1.0, "BBBUSDT": 2.0}
DRIFT = {"AAAUSDT": -0.08, "BBBUSDT": -0.08}  # both fall: we keep buying, longs pile up


def specs():
    return {s: spec(s) for s in SYMS} | {
        "BTCUSDT": spec("BTCUSDT", tick="0.1", step="0.001", min_notional="100"),
        "ETHUSDT": spec("ETHUSDT", tick="0.01", step="0.01", min_notional="20")}


@pytest.fixture(scope="module")
def events(tmp_path_factory):
    root = tmp_path_factory.mktemp("hedge")
    start = T0 + 3600_000
    asyncio.run(write_dataset(root, SYMS, start, minutes=40, drift_bps=DRIFT))
    asyncio.run(write_dataset(root, {"ETHUSDT": 10.0}, start, minutes=40, seed=11, tick=0.01, step_bps=0.3))
    return load_events(root, [*SYMS, "ETHUSDT"])


def run(events, hedge: bool):
    cfg = Config.model_validate({"quoting": {"sigma_warmup_samples": 20}, "risk": {"max_net_beta_exposure_usdt": 35},
                                 "hedge": {"enabled": hedge, "hedge_threshold_usdt": 10, "hedge_urgent_usdt": 20}})
    table = BetaTable("BTCUSDT")
    samples: list[float] = []

    def setup(engine):
        engine.beta = table.get
        if hedge:
            engine.timer_hooks.append(Hedger(cfg, engine, table.get).on_timer)

        def sample(now):
            if now % 1000 == 0:
                samples.append(engine.venues["paper"].risk.net_beta())

        engine.timer_hooks.append(sample)

    res = run_replay(cfg, specs(), events, list(SYMS), hedge_symbols=["ETHUSDT"], setup=setup)
    return res, samples, cfg


def test_net_beta_stays_within_limit_with_hedger(events):
    base, base_net, cfg = run(events, hedge=False)
    hedged, net, _ = run(events, hedge=True)
    limit = cfg.risk.max_net_beta_exposure_usdt
    hedge_fills = [f for _, f in hedged.fills if f.symbol == "ETHUSDT"]
    print(f"no hedge: max|net|={max(map(abs, base_net)):.1f}  hedged: max|net|={max(map(abs, net)):.1f} "
          f"hedge fills={len(hedge_fills)} limit={limit} "
          f"rejects {base.venue.risk.rejects} vs {hedged.venue.risk.rejects} "
          f"fills {len(base.fills)} vs {len(hedged.fills)}")
    assert hedge_fills, "the hedger should have hedged the accumulated inventory"
    assert all(f.is_maker for f in hedge_fills)  # post-only (A2)
    assert max(map(abs, net)) <= limit
    # the risk engine already caps net beta pre-trade; the hedger frees that room so quoting is blocked less
    rej = "max_net_beta_exposure"
    assert hedged.venue.risk.rejects.get(rej, 0) < 0.5 * base.venue.risk.rejects[rej]
