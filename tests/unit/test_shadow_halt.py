"""Shadow (paper venue) HALTs must be persisted like real-venue HALTs (OPEN_QUESTIONS B6), so a crash and
automatic restart cannot resume quoting after a daily-loss or drawdown HALT."""

from riverbank.core.clock import RealClock
from riverbank.core.models import Mode
from riverbank.engine import Engine
from riverbank.main import App
from riverbank.marketdata.market_state import MarketState
from riverbank.orders.client_ids import ClientIdAllocator
from riverbank.risk.killswitch import halt_record, startup_blockers
from riverbank.storage.sqlite import SqliteRepository
from tests.helpers import T0, cfg, spec

SYM = "AAAUSDT"


async def test_paper_venue_halt_is_persisted_and_blocks_restart(tmp_path):
    c = cfg()
    repo = SqliteRepository(tmp_path / "riverbank.sqlite")
    market = MarketState([SYM], stale_ms=c.ops.stale_ms, request_snapshot=lambda *a: None, refresh_interval_ms=0)
    app = App(c, Mode.SHADOW, "config/config.yaml")
    app.repo = repo
    app.engine = Engine(c, {SYM: spec(SYM)}, market, RealClock(), ClientIdAllocator(repo.reserve_seq_block),
                        repo=repo)
    await app._setup_venues([SYM])

    app.engine.venues["paper"].risk.halt("drawdown 16.00% >= 15%", T0)

    rec = halt_record(repo)
    assert rec is not None and "drawdown" in str(rec["reason"])
    assert any("persisted HALT" in b for b in startup_blockers(repo, tmp_path / "KILL"))
    repo.close()
