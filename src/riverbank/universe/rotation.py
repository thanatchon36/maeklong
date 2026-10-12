"""Two-stage universe: CANDIDATE (shadow only) → ACTIVE (live) → DEMOTED (SPEC §5.2).

Promotion (OPEN_QUESTIONS B10/B11): a symbol becomes ACTIVE only if
  * it is in the current screened universe (hard exclusions still apply),
  * its shadow evidence has n ≥ `min_fills_for_promotion` and realized edge mean − 1·SE > 0
    at `promotion_horizon_s` (`analytics.report.promotion_status`),
  * it is in `universe.live_symbols`, unless `universe.auto_promote` is true,
  * it is not in its demotion cooldown; after a demotion only shadow fills newer than the
    demotion count as evidence,
and at most `max_active_symbols` symbols are ACTIVE (best edge lower bound first).
Every state change is written to `universe_decisions` and logged.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from riverbank.analytics.markout import stats
from riverbank.analytics.report import fee_bps, promotion_status
from riverbank.core.config import Config, SafetyError, UniverseCfg
from riverbank.core.models import UniverseState
from riverbank.ops.logging import get_logger

log = get_logger("rotation")

Evidence = dict[str, list[tuple[int, float]]]  # symbol -> [(fill ts, realized edge bps)] in time order
States = dict[str, tuple[str, int]]  # symbol -> (latest decided state, ts)

CANDIDATE, ACTIVE, DEMOTED = UniverseState.CANDIDATE.value, UniverseState.ACTIVE.value, UniverseState.DEMOTED.value


@dataclass(frozen=True, slots=True)
class Decision:
    ts: int
    symbol: str
    from_state: str
    to_state: str
    reason: str
    n: int
    mean: float
    se: float


def shadow_evidence(conn: sqlite3.Connection, horizon_s: int) -> Evidence:
    """Realized edge (markout − fee, bps) of every shadow fill whose markout at `horizon_s` is resolved."""
    out: Evidence = {}
    q = f"SELECT symbol, ts, price, qty, fee, mo_{int(horizon_s)} FROM fills WHERE mode='shadow' " \
        f"AND mo_{int(horizon_s)} IS NOT NULL ORDER BY ts, id"
    for sym, ts, price, qty, fee, mo in conn.execute(q):
        out.setdefault(sym, []).append((ts, mo - fee_bps({"price": price, "qty": qty, "fee": fee})))
    return out


def last_states(rows: list[tuple[Any, ...]]) -> States:
    """Latest decision per symbol from `universe_decisions` rows (symbol, to_state, ts) in id order."""
    return {sym: (state, ts) for sym, state, ts in rows}


def select_active(evidence: Evidence, u: UniverseCfg, universe: list[str], states: States,
                  now: int) -> tuple[list[str], list[Decision]]:
    cooldown_ms = int(u.demotion_cooldown_h * 3_600_000)
    eligible: list[tuple[float, str, Any]] = []
    for sym in sorted(set(universe) - set(u.hedge_only)):
        state, since = states.get(sym, (CANDIDATE, 0))
        if state == DEMOTED and now < since + cooldown_ms:
            continue
        values = [e for ts, e in evidence.get(sym, []) if state != DEMOTED or ts > since]
        st = stats(values)
        ok, why = promotion_status(len(values), st, u.min_fills_for_promotion)
        if not ok:
            continue
        if not u.auto_promote and sym not in u.live_symbols:
            log.info("promotion_eligible_not_allowlisted", symbol=sym, why=why)
            continue
        eligible.append((-st.lower, sym, (st, why)))
    chosen = sorted(eligible)[: u.max_active_symbols]
    decisions = []
    for _, sym, (st, why) in chosen:
        decisions.append(Decision(now, sym, states.get(sym, (CANDIDATE, 0))[0], ACTIVE, f"promoted: {why}",
                                  st.n, st.mean, st.se))
    selected = sorted(sym for _, sym, _ in chosen)
    for sym, (state, _) in sorted(states.items()):
        if state == ACTIVE and sym not in selected:
            decisions.append(Decision(now, sym, ACTIVE, CANDIDATE, "not selected at startup", 0,
                                      float("nan"), float("nan")))
    return selected, decisions


def record(repo: Any, decisions: list[Decision]) -> None:
    for d in decisions:
        log.warning("universe_decision", symbol=d.symbol, from_state=d.from_state, to_state=d.to_state,
                    reason=d.reason, n=d.n, mean=d.mean, se=d.se)
        repo.insert_decision(d.ts, d.symbol, d.from_state, d.to_state, d.reason, d.n, d.mean, d.se)


def promoted_symbols(cfg: Config, universe: list[str], repo: Any, now: int) -> list[str]:
    """Live startup: ACTIVE symbols from the shadow service's evidence (read-only) and this DB's decisions."""
    u = cfg.universe
    shadow = Path(u.shadow_db)
    if not shadow.is_file():
        raise SafetyError(f"universe.shadow_db {shadow} not found: live needs the shadow run's evidence")
    if shadow.resolve() == Path(repo.path).resolve():
        raise SafetyError("universe.shadow_db is this process's own DB: live needs its own ops.state_dir")
    conn = sqlite3.connect(f"{shadow.resolve().as_uri()}?mode=ro", uri=True)
    try:
        evidence = shadow_evidence(conn, u.promotion_horizon_s)
    finally:
        conn.close()
    states = last_states(repo.query("SELECT symbol, to_state, ts FROM universe_decisions ORDER BY id"))
    selected, decisions = select_active(evidence, u, universe, states, now)
    record(repo, decisions)
    repo.flush()
    log.warning("live_symbols_selected", symbols=selected, allowlist=list(u.live_symbols),
                auto_promote=u.auto_promote)
    return selected
