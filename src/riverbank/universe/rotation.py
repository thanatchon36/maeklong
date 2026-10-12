"""Two-stage universe: CANDIDATE (shadow only) → ACTIVE (live) → DEMOTED (SPEC §5.2).

Promotion (OPEN_QUESTIONS B10/B11): a symbol becomes ACTIVE only if
  * it is in the current screened universe (hard exclusions still apply),
  * its shadow evidence has n ≥ `min_fills_for_promotion` and realized edge mean − 1·SE > 0
    at `promotion_horizon_s` (`analytics.report.promotion_status`),
  * it is in `universe.live_symbols`, unless `universe.auto_promote` is true,
  * it is not in its demotion cooldown; after a demotion only shadow fills newer than the
    demotion count as evidence,
and at most `max_active_symbols` symbols are ACTIVE (best edge lower bound first).
Demotion (M7, always on): over the last `demotion_window_fills` live fills mean + 1·SE < 0 → DEMOTED;
a symbol that leaves the screened universe → CANDIDATE; after `demotion_cooldown_h` DEMOTED → CANDIDATE.
Every state change is written to `universe_decisions` and logged.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from riverbank.analytics.markout import Stats, stats
from riverbank.analytics.report import fee_bps, promotion_status
from riverbank.core.config import Config, SafetyError, UniverseCfg
from riverbank.core.models import UniverseState
from riverbank.ops.logging import get_logger

log = get_logger("rotation")

Evidence = dict[str, list[tuple[int, float]]]  # symbol -> [(fill ts, realized edge bps)] in time order
States = dict[str, tuple[str, int]]  # symbol -> (latest decided state, ts)

CANDIDATE, ACTIVE, DEMOTED = UniverseState.CANDIDATE.value, UniverseState.ACTIVE.value, UniverseState.DEMOTED.value
NAN = float("nan")


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


def _edge_sql(horizon_s: int, where: str) -> str:
    h = int(horizon_s)
    return f"SELECT symbol, ts, price, qty, fee, mo_{h} FROM fills WHERE {where} AND mo_{h} IS NOT NULL ORDER BY ts, id"


def _evidence(rows: Iterable[tuple[Any, ...]]) -> Evidence:
    out: Evidence = {}
    for sym, ts, price, qty, fee, mo in rows:
        out.setdefault(sym, []).append((ts, mo - fee_bps({"price": price, "qty": qty, "fee": fee})))
    return out


def shadow_evidence(conn: sqlite3.Connection, horizon_s: int) -> Evidence:
    """Realized edge (markout − fee, bps) of every shadow fill whose markout at `horizon_s` is resolved."""
    return _evidence(conn.execute(_edge_sql(horizon_s, "mode='shadow'")))


def live_edges(repo: Any, venue: str, horizon_s: int) -> dict[str, list[float]]:
    """Realized edges of the real venue's own fills, in time order (demotion evidence)."""
    ev = _evidence(repo.query(_edge_sql(horizon_s, "venue=?"), (venue,)))
    return {s: [e for _, e in xs] for s, xs in ev.items()}


def read_shadow_db(u: UniverseCfg, own_db: str) -> Evidence:
    shadow = Path(u.shadow_db)
    if not shadow.is_file():
        raise SafetyError(f"universe.shadow_db {shadow} not found: live needs the shadow run's evidence")
    if shadow.resolve() == Path(own_db).resolve():
        raise SafetyError("universe.shadow_db is this process's own DB: live needs its own ops.state_dir")
    conn = sqlite3.connect(f"{shadow.resolve().as_uri()}?mode=ro", uri=True)
    try:
        return shadow_evidence(conn, u.promotion_horizon_s)
    finally:
        conn.close()


def last_states(rows: list[tuple[Any, ...]]) -> States:
    """Latest decision per symbol from `universe_decisions` rows (symbol, to_state, ts) in id order."""
    return {sym: (state, ts) for sym, state, ts in rows}


def last_demotions(rows: list[tuple[Any, ...]]) -> dict[str, int]:
    """Time of each symbol's latest demotion (only shadow evidence newer than it counts again)."""
    return {sym: ts for sym, state, ts in rows if state == DEMOTED}


def _eligible(evidence: Evidence, u: UniverseCfg, universe: list[str], states: States, demoted_at: dict[str, int],
              now: int, exclude: list[str]) -> list[tuple[float, str, Stats, str]]:
    """Promotable symbols, best edge lower bound first (B10/B11, cooldown, fresh evidence after a demotion)."""
    cooldown_ms = int(u.demotion_cooldown_h * 3_600_000)
    eligible = []
    for sym in sorted(set(universe) - set(u.hedge_only) - set(exclude)):
        state, since = states.get(sym, (CANDIDATE, 0))
        if state == DEMOTED and now < since + cooldown_ms:
            continue
        cutoff = demoted_at.get(sym, since if state == DEMOTED else 0)
        values = [e for ts, e in evidence.get(sym, []) if ts > cutoff]
        st = stats(values)
        ok, why = promotion_status(len(values), st, u.min_fills_for_promotion)
        if not ok:
            continue
        if not u.auto_promote and sym not in u.live_symbols:
            log.info("promotion_eligible_not_allowlisted", symbol=sym, why=why)
            continue
        eligible.append((-st.lower, sym, st, why))
    return sorted(eligible)


def _promote(sym: str, states: States, st: Stats, why: str, now: int) -> Decision:
    return Decision(now, sym, states.get(sym, (CANDIDATE, 0))[0], ACTIVE, f"promoted: {why}", st.n, st.mean, st.se)


def select_active(evidence: Evidence, u: UniverseCfg, universe: list[str], states: States,
                  now: int, demoted_at: dict[str, int] | None = None) -> tuple[list[str], list[Decision]]:
    """Live startup: the ACTIVE set from scratch."""
    chosen = _eligible(evidence, u, universe, states, demoted_at or {}, now, [])[: u.max_active_symbols]
    decisions = [_promote(sym, states, st, why, now) for _, sym, st, why in chosen]
    selected = sorted(sym for _, sym, _, _ in chosen)
    for sym, (state, _) in sorted(states.items()):
        if state == ACTIVE and sym not in selected:
            decisions.append(Decision(now, sym, ACTIVE, CANDIDATE, "not selected at startup", 0, NAN, NAN))
    return selected, decisions


def demotion_check(values: list[float], window: int) -> tuple[bool, Stats, str]:
    """B10: demote if over the last `window` live fills mean + 1·SE < 0; never on fewer fills."""
    if len(values) < window:
        return False, stats(values), f"need ≥{window} live fills (have {len(values)})"
    st = stats(values[-window:])
    return st.upper < 0, st, f"live edge upper bound {st.upper:+.2f} bps over the last {window} fills"


def rotate(active: list[str], evidence: Evidence, live_edges: dict[str, list[float]], u: UniverseCfg,
           universe: list[str], states: States, now: int,
           demoted_at: dict[str, int] | None = None) -> tuple[list[str], list[Decision]]:
    """M7 periodic step: demote (always on, B11), release cooldowns, then fill free slots by promotion.
    A better candidate never displaces a healthy ACTIVE symbol (no churn)."""
    demoted_at = dict(demoted_at or {})
    states = dict(states)
    decisions: list[Decision] = []
    keep: list[str] = []
    for sym in sorted(active):
        if sym not in universe:
            decisions.append(Decision(now, sym, ACTIVE, CANDIDATE, "left universe", 0, NAN, NAN))
            continue
        demote, st, why = demotion_check(live_edges.get(sym, []), u.demotion_window_fills)
        if demote:
            decisions.append(Decision(now, sym, ACTIVE, DEMOTED, f"demoted: {why}", st.n, st.mean, st.se))
            demoted_at[sym] = now
            continue
        keep.append(sym)
    for d in decisions:
        states[d.symbol] = (d.to_state, now)
    cooldown_ms = int(u.demotion_cooldown_h * 3_600_000)
    for sym, (state, since) in sorted(states.items()):
        if state == DEMOTED and now >= since + cooldown_ms:
            decisions.append(Decision(now, sym, DEMOTED, CANDIDATE, "cooldown over", 0, NAN, NAN))
            demoted_at.setdefault(sym, since)
            states[sym] = (CANDIDATE, now)
    slots = max(u.max_active_symbols - len(keep), 0)
    for _, sym, st, why in _eligible(evidence, u, universe, states, demoted_at, now, keep)[:slots]:
        decisions.append(_promote(sym, states, st, why, now))
        keep.append(sym)
    return sorted(keep), decisions


def record(repo: Any, decisions: list[Decision]) -> None:
    for d in decisions:
        log.warning("universe_decision", symbol=d.symbol, from_state=d.from_state, to_state=d.to_state,
                    reason=d.reason, n=d.n, mean=d.mean, se=d.se)
        repo.insert_decision(d.ts, d.symbol, d.from_state, d.to_state, d.reason, d.n, d.mean, d.se)


def promoted_symbols(cfg: Config, universe: list[str], repo: Any, now: int) -> list[str]:
    """Live startup: ACTIVE symbols from the shadow service's evidence (read-only) and this DB's decisions."""
    u = cfg.universe
    evidence = read_shadow_db(u, repo.path)
    rows = repo.query("SELECT symbol, to_state, ts FROM universe_decisions ORDER BY id")
    selected, decisions = select_active(evidence, u, universe, last_states(rows), now, last_demotions(rows))
    record(repo, decisions)
    repo.flush()
    log.warning("live_symbols_selected", symbols=selected, allowlist=list(u.live_symbols),
                auto_promote=u.auto_promote)
    return selected
