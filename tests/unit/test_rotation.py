"""Universe rotation (SPEC §5.2 two-stage universe, OPEN_QUESTIONS B10/B11)."""

import sqlite3
from decimal import Decimal

import pytest

from riverbank.core.config import Config, SafetyError
from riverbank.core.models import Fill, Side
from riverbank.storage.sqlite import SqliteRepository
from riverbank.universe.rotation import promoted_symbols, select_active, shadow_evidence
from tests.helpers import T0

H = 3_600_000


def ucfg(**kw):
    return Config.model_validate({"universe": {"min_fills_for_promotion": 4, **kw}}).universe


def ev(*edges, t0=T0):
    return [(t0 + i, e) for i, e in enumerate(edges)]


GOOD = ev(3, 4, 5, 4)          # mean 4, lower bound > 0
BETTER = ev(8, 9, 10, 9)
BAD = ev(-1, 1, -2, 0)


def test_promotes_only_allowlisted_symbols_with_enough_positive_evidence():
    evidence = {"AAAUSDT": GOOD, "BBBUSDT": BAD, "CCCUSDT": GOOD[:3], "DDDUSDT": GOOD}
    syms, decisions = select_active(evidence, ucfg(live_symbols=["AAAUSDT", "BBBUSDT", "CCCUSDT"]),
                                    ["AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT"], {}, T0 + H)
    assert syms == ["AAAUSDT"]  # BBB: negative edge; CCC: too few fills; DDD: not allow-listed
    assert [(d.symbol, d.from_state, d.to_state) for d in decisions] == [("AAAUSDT", "CANDIDATE", "ACTIVE")]
    assert decisions[0].n == 4 and decisions[0].mean == pytest.approx(4.0)


def test_auto_promote_ignores_allowlist_and_cap_keeps_best_lower_bounds():
    evidence = {"AAAUSDT": GOOD, "BBBUSDT": BETTER, "CCCUSDT": GOOD}
    syms, _ = select_active(evidence, ucfg(auto_promote=True, max_active_symbols=2),
                            ["AAAUSDT", "BBBUSDT", "CCCUSDT"], {}, T0 + H)
    assert syms == ["AAAUSDT", "BBBUSDT"]  # BBB best; AAA/CCC tie -> symbol order; result sorted


def test_symbol_outside_current_universe_is_never_active():
    syms, _ = select_active({"AAAUSDT": GOOD}, ucfg(live_symbols=["AAAUSDT"]), [], {}, T0 + H)
    assert syms == []


def test_demoted_symbol_waits_for_cooldown_then_needs_fresh_evidence():
    u = ucfg(live_symbols=["AAAUSDT"], demotion_cooldown_h=24)
    states = {"AAAUSDT": ("DEMOTED", T0 + 10)}
    # inside the cooldown: excluded even with good evidence
    assert select_active({"AAAUSDT": GOOD}, u, ["AAAUSDT"], states, T0 + 23 * H)[0] == []
    # after the cooldown: evidence from before the demotion does not count
    assert select_active({"AAAUSDT": GOOD}, u, ["AAAUSDT"], states, T0 + 25 * H)[0] == []
    fresh = ev(3, 4, 5, 4, t0=T0 + 2 * H)
    syms, decisions = select_active({"AAAUSDT": GOOD + fresh}, u, ["AAAUSDT"], states, T0 + 25 * H)
    assert syms == ["AAAUSDT"]
    assert (decisions[0].from_state, decisions[0].to_state, decisions[0].n) == ("DEMOTED", "ACTIVE", 4)


def test_previously_active_symbol_not_selected_is_recorded():
    syms, decisions = select_active({"AAAUSDT": BAD}, ucfg(live_symbols=["AAAUSDT"]), ["AAAUSDT"],
                                    {"AAAUSDT": ("ACTIVE", T0)}, T0 + H)
    assert syms == []
    assert [(d.from_state, d.to_state) for d in decisions] == [("ACTIVE", "CANDIDATE")]


def _fill(repo, venue, mode, sym, ts, edge_60, fee="0", tid="1"):
    f = Fill(ts, sym, Side.BUY, Decimal("1"), Decimal("10"), Decimal(fee), True, mode, f"rb-{ts}", 1.0, tid)
    repo.insert_fill(f, venue)
    if edge_60 is not None:
        repo.update_markout(venue, f.client_id, tid, 60, edge_60)


def test_shadow_evidence_uses_resolved_shadow_fills_net_of_fees(tmp_path):
    repo = SqliteRepository(tmp_path / "s.sqlite")
    _fill(repo, "paper", "shadow", "AAAUSDT", T0 + 1, 5.0, fee="0.002")   # fee 2 bps -> edge 3
    _fill(repo, "paper", "shadow", "AAAUSDT", T0 + 2, None)               # markout not resolved yet
    _fill(repo, "live", "live", "AAAUSDT", T0 + 3, 9.0)                   # live fills are not shadow evidence
    repo.flush()
    conn = sqlite3.connect(repo.path)
    got = shadow_evidence(conn, 60)
    assert list(got) == ["AAAUSDT"]
    assert got["AAAUSDT"] == [(T0 + 1, pytest.approx(3.0))]
    repo.close()


def test_promoted_symbols_reads_shadow_db_and_records_decisions(tmp_path):
    shadow = SqliteRepository(tmp_path / "shadow.sqlite")
    for i, e in enumerate([3, 4, 5, 4]):
        _fill(shadow, "paper", "shadow", "AAAUSDT", T0 + i, float(e))
    shadow.flush()
    live = SqliteRepository(tmp_path / "live" / "riverbank.sqlite")
    cfg = Config.model_validate({"universe": {"min_fills_for_promotion": 4, "live_symbols": ["AAAUSDT"],
                                              "shadow_db": str(tmp_path / "shadow.sqlite")}})
    assert promoted_symbols(cfg, ["AAAUSDT"], live, T0 + H) == ["AAAUSDT"]
    rows = live.query("SELECT symbol, from_state, to_state, n FROM universe_decisions")
    assert rows == [("AAAUSDT", "CANDIDATE", "ACTIVE", 4)]
    # the shadow DB is opened read-only: nothing was written to it
    assert shadow.query("SELECT COUNT(*) FROM universe_decisions") == [(0,)]
    shadow.close()
    live.close()


def test_promoted_symbols_refuses_to_share_the_shadow_db(tmp_path):
    repo = SqliteRepository(tmp_path / "riverbank.sqlite")
    cfg = Config.model_validate({"universe": {"shadow_db": str(tmp_path / "riverbank.sqlite")}})
    with pytest.raises(SafetyError, match="shadow_db"):
        promoted_symbols(cfg, ["AAAUSDT"], repo, T0)
    repo.close()


def test_promoted_symbols_refuses_missing_shadow_db(tmp_path):
    repo = SqliteRepository(tmp_path / "live.sqlite")
    cfg = Config.model_validate({"universe": {"shadow_db": str(tmp_path / "nope.sqlite")}})
    with pytest.raises(SafetyError, match="shadow_db"):
        promoted_symbols(cfg, ["AAAUSDT"], repo, T0)
    repo.close()


async def test_live_selection_stays_blocked_until_acceptance_is_approved(tmp_path):
    from types import SimpleNamespace

    from riverbank.core.models import Mode
    from riverbank.orders import live_venue

    repo = SqliteRepository(tmp_path / "live.sqlite")
    app = SimpleNamespace(mode=Mode.LIVE, cfg=Config(), repo=repo)
    with pytest.raises(SafetyError, match="B1"):
        await live_venue.select_real_symbols(app, ["AAAUSDT"])
    repo.close()


async def test_live_selection_uses_promoted_symbols_once_approved(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from riverbank.core.models import Mode
    from riverbank.orders import live_venue

    shadow = SqliteRepository(tmp_path / "shadow.sqlite")
    for i, e in enumerate([3, 4, 5, 4]):
        _fill(shadow, "paper", "shadow", "AAAUSDT", T0 + i, float(e))
    shadow.close()
    repo = SqliteRepository(tmp_path / "live" / "riverbank.sqlite")
    cfg = Config.model_validate({"universe": {"min_fills_for_promotion": 4, "live_symbols": ["AAAUSDT"],
                                              "shadow_db": str(tmp_path / "shadow.sqlite")}})
    monkeypatch.setattr(live_venue, "LIVE_ACCEPTANCE_APPROVED", True)
    app = SimpleNamespace(mode=Mode.LIVE, cfg=cfg, repo=repo)
    assert await live_venue.select_real_symbols(app, ["AAAUSDT", "BBBUSDT"]) == ["AAAUSDT"]
    app.mode = Mode.DEMO  # demo keeps quoting the configured universe
    assert await live_venue.select_real_symbols(app, ["AAAUSDT", "BBBUSDT"]) == ["AAAUSDT", "BBBUSDT"]
    repo.close()


# ---- M7: periodic rotation ----

def test_demotion_needs_a_full_window_and_negative_upper_bound():
    from riverbank.universe.rotation import demotion_check

    assert demotion_check([-5.0, -6.0, -4.0], 4)[0] is False         # never decide on fewer fills
    demote, st, _ = demotion_check([9.0] * 10 + [-5.0, -6.0, -4.0, -5.0], 4)
    assert demote and st.n == 4 and st.mean + st.se < 0              # only the last window counts
    assert demotion_check([-5.0, 5.0, -4.0, 3.0], 4)[0] is False     # mean + SE ≥ 0: keep


def test_rotate_demotes_bad_symbol_and_fills_the_slot():
    from riverbank.universe.rotation import rotate

    u = ucfg(live_symbols=["AAAUSDT", "BBBUSDT"], max_active_symbols=1, demotion_window_fills=4)
    active, decisions = rotate(["AAAUSDT"], {"BBBUSDT": GOOD}, {"AAAUSDT": [-5.0, -6.0, -4.0, -5.0]}, u,
                               ["AAAUSDT", "BBBUSDT"], {"AAAUSDT": ("ACTIVE", T0)}, T0 + H)
    assert active == ["BBBUSDT"]
    assert [(d.symbol, d.from_state, d.to_state) for d in decisions] == [
        ("AAAUSDT", "ACTIVE", "DEMOTED"), ("BBBUSDT", "CANDIDATE", "ACTIVE")]


def test_rotate_keeps_good_active_and_respects_cap():
    from riverbank.universe.rotation import rotate

    u = ucfg(live_symbols=["AAAUSDT", "BBBUSDT"], max_active_symbols=1, demotion_window_fills=4)
    active, decisions = rotate(["AAAUSDT"], {"BBBUSDT": BETTER}, {"AAAUSDT": [1.0, 2.0, 1.0, 2.0]}, u,
                               ["AAAUSDT", "BBBUSDT"], {"AAAUSDT": ("ACTIVE", T0)}, T0 + H)
    assert active == ["AAAUSDT"] and decisions == []  # no churn: a better candidate waits for a free slot


def test_rotate_drops_symbols_that_left_the_universe():
    from riverbank.universe.rotation import rotate

    active, decisions = rotate(["AAAUSDT"], {}, {}, ucfg(live_symbols=["AAAUSDT"]), [],
                               {"AAAUSDT": ("ACTIVE", T0)}, T0 + H)
    assert active == []
    assert [(d.from_state, d.to_state, d.reason) for d in decisions] == [("ACTIVE", "CANDIDATE", "left universe")]


def test_rotate_returns_demoted_to_candidate_after_cooldown_once():
    from riverbank.universe.rotation import rotate

    u = ucfg(live_symbols=["AAAUSDT"], demotion_cooldown_h=24)
    states = {"AAAUSDT": ("DEMOTED", T0 + 10)}
    assert rotate([], {}, {}, u, ["AAAUSDT"], states, T0 + 23 * H)[1] == []
    active, decisions = rotate([], {"AAAUSDT": GOOD}, {}, u, ["AAAUSDT"], states, T0 + 25 * H)
    assert active == []  # its evidence predates the demotion
    assert [(d.from_state, d.to_state) for d in decisions] == [("DEMOTED", "CANDIDATE")]
    # recorded as CANDIDATE now, but evidence must still be newer than the demotion
    states2 = {"AAAUSDT": ("CANDIDATE", T0 + 25 * H)}
    assert rotate([], {"AAAUSDT": GOOD}, {}, u, ["AAAUSDT"], states2, T0 + 26 * H, {"AAAUSDT": T0 + 10})[0] == []
