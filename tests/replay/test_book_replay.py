"""Book-sync replay over a real recorded diff stream (public prod capture) with injected gaps.

The fixture contains raw `@depth@100ms` messages for two symbols plus REST snapshots
(one 500-level, then 100-level every ~15 s). Every snapshot after the first is used as a
*check*: a fresh book is built from it and compared with the long-running book at the same
update id (exact L1–L5). With injected gaps the book must detect them, resync from the
next snapshot, and match again.
"""

import gzip
import random
from pathlib import Path

import orjson
import pytest

from riverbank.core.events import DepthSnapshot
from riverbank.marketdata.gateway import parse_message
from riverbank.marketdata.market_state import MarketState

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "depth_capture.jsonl.gz"


def load():
    out = []
    with gzip.open(FIX) as f:
        for line in f:
            r = orjson.loads(line)
            if r["t"] == "ws":
                out.append(("ws", r["recv"], parse_message(r["text"], r["recv"])))
            else:
                d = r["data"]
                out.append(("snap", r["recv"], (r["symbol"], int(d["lastUpdateId"]),
                                                [(float(p), float(q)) for p, q in d["bids"]],
                                                [(float(p), float(q)) for p, q in d["asks"]])))
    out.sort(key=lambda x: x[1])
    return out


def run(drop_prob: float, seed: int):
    events = load()
    syms = sorted({e[2][0] for e in events if e[0] == "snap"})
    requests: dict[str, str] = {}
    checks: list[tuple[str, bool]] = []
    ms = MarketState(syms, stale_ms=10_000, request_snapshot=lambda s, lim, p: requests.__setitem__(s, p),
                     on_check=lambda s, ok, d: checks.append((s, ok)), refresh_interval_ms=0)
    rng = random.Random(seed)
    dropped = 0
    for kind, ts, payload in events:
        if kind == "ws":
            if drop_prob and rng.random() < drop_prob:
                dropped += 1
                continue
            ms.on_depth(payload)
            continue
        sym, uid, bids, asks = payload
        m = ms.get(sym)
        if not m.sync.synced:
            ms.on_snapshot(DepthSnapshot(sym, uid, bids, asks, ts, "sync"))
        elif ms.start_pending(sym, "check", ts):
            ms.on_snapshot(DepthSnapshot(sym, uid, bids, asks, ts, "check"))
    return ms, checks, dropped


def test_clean_replay_all_checks_match():
    ms, checks, _ = run(0.0, 0)
    assert checks, "no checks completed"
    assert all(ok for _, ok in checks), checks
    for m in ms.symbols.values():
        assert m.sync.synced and m.sync.gaps == 0 and m.check_mismatches == 0


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_injected_gaps_detected_and_resynced(seed):
    ms, checks, dropped = run(0.01, seed)
    assert dropped > 0
    total_gaps = sum(m.sync.gaps for m in ms.symbols.values())
    assert total_gaps > 0, "injected gaps were not detected"
    # Never a silently wrong book: every completed check matches.
    assert all(ok for _, ok in checks), checks
