"""Beta estimation (SPEC §5.7): 1-min returns vs BTCUSDT/ETHUSDT, rolling window, shrunk toward 1."""

import math
import random

import pytest

from riverbank.hedge.beta import BetaTable, estimate_beta, log_returns

M = 60_000


def closes(prices, t0=0):
    return [(t0 + i * M, p) for i, p in enumerate(prices)]


def walk(rets, p0=100.0):
    out = [p0]
    for r in rets:
        out.append(out[-1] * math.exp(r))
    return out


def test_log_returns_are_keyed_by_candle_time_and_skip_gaps():
    r = log_returns([(0, 100.0), (M, 110.0), (3 * M, 121.0)])  # the 2M candle is missing
    assert list(r) == [M]
    assert r[M] == pytest.approx(math.log(1.1))


def test_ols_beta_with_shrinkage_toward_one():
    rng = random.Random(1)
    ref = [rng.gauss(0, 0.001) for _ in range(2000)]
    sym = [2.0 * x + rng.gauss(0, 0.0002) for x in ref]
    rx, rs = log_returns(closes(walk(ref))), log_returns(closes(walk(sym)))
    raw, n = estimate_beta(rs, rx, shrink=0.0)
    assert n == 2000 and raw == pytest.approx(2.0, abs=0.05)
    shrunk, _ = estimate_beta(rs, rx, shrink=0.3)
    assert shrunk == pytest.approx(0.7 * raw + 0.3 * 1.0)


def test_too_little_overlap_falls_back_to_prior_one():
    rx = log_returns(closes(walk([0.001, -0.001] * 10)))
    assert estimate_beta(rx, rx, shrink=0.0) == (1.0, 20)


def test_beta_table_defaults_to_one_and_reference_to_itself():
    t = BetaTable("BTCUSDT")
    t.set("AAAUSDT", "BTCUSDT", 1.7)
    assert t.get("AAAUSDT") == 1.7
    assert t.get("BBBUSDT") == 1.0          # unknown: prior
    assert t.get("ETHUSDT", "ETHUSDT") == 1.0


async def test_refresh_fetches_klines_for_symbols_and_references():
    rng = random.Random(2)
    ref = [rng.gauss(0, 0.001) for _ in range(300)]
    series = {"BTCUSDT": walk(ref), "ETHUSDT": walk([1.5 * x for x in ref]), "AAAUSDT": walk([0.5 * x for x in ref])}
    calls = []

    class Rest:
        async def public(self, method, path, params):
            calls.append((path, params["symbol"], params["interval"]))
            px = series[params["symbol"]]
            start = params["startTime"]
            rows = [[start + i * M, "0", "0", "0", str(p), "0", start + i * M + M - 1] for i, p in enumerate(px)]
            return rows[: params["limit"]]

    t = BetaTable("BTCUSDT", shrink=0.0, min_obs=100)
    await t.refresh(Rest(), ["AAAUSDT", "BTCUSDT"], ("BTCUSDT", "ETHUSDT"), window_days=300 / 1440, now_ms=300 * M)
    assert {c[1] for c in calls} == {"AAAUSDT", "BTCUSDT", "ETHUSDT"}
    assert all(c[0] == "/fapi/v1/klines" and c[2] == "1m" for c in calls)
    assert t.get("AAAUSDT") == pytest.approx(0.5, abs=1e-6)
    assert t.get("AAAUSDT", "ETHUSDT") == pytest.approx(1 / 3, abs=1e-6)
    assert t.get("ETHUSDT") == pytest.approx(1.5, abs=1e-6)
