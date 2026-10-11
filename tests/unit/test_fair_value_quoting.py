import math
from decimal import Decimal

from riverbank.core.config import QuotingCfg
from riverbank.core.models import BookStatus, RiskState, Side
from riverbank.features.fair_value import FairValue
from riverbank.marketdata.orderbook import OrderBook
from riverbank.quoting.engine import QuoteInputs, QuoterState, compute_quotes
from tests.helpers import T0, feats, spec

# ---------------- fair value ----------------

def test_mid_micro_imbalance():
    fv = FairValue("X", QuotingCfg())
    fv.on_bbo(99.0, 3.0, 101.0, 1.0, T0)
    assert fv.mid == 100.0
    assert fv.micro == (101.0 * 3 + 99.0 * 1) / 4  # leans toward the ask when bids are heavier
    b = OrderBook()
    b.load([(99.0, 3.0), (98.0, 1.0)], [(101.0, 1.0), (102.0, 1.0)])
    assert fv.imbalance(b) == (4 - 2) / 6
    assert fv.imbalance(None) == (3 - 1) / 4


def test_sigma_ewma_matches_closed_form():
    cfg = QuotingCfg(sigma_half_life_s=10, sigma_warmup_samples=3)
    fv = FairValue("X", cfg)
    lam = 0.5 ** (1 / 10)
    mids = [100.0, 101.0, 100.0, 102.0, 102.0]
    var = 0.0
    for i, m in enumerate(mids):
        fv.on_bbo(m - 0.5, 1, m + 0.5, 1, T0 + i * 1000)
        fv.sample(T0 + i * 1000)
        if i:
            r = math.log(m / mids[i - 1])
            var = lam * var + (1 - lam) * r * r
    assert abs(fv.sigma_bps - math.sqrt(var) * 1e4) < 1e-9
    assert fv.sigma_ready
    # a 3 s gap is treated as one 3 s return spread over 3 samples
    fv.on_bbo(103.5, 1, 104.5, 1, T0 + 7000)
    fv.sample(T0 + 7000)
    r = math.log(104 / 102)
    var = lam ** 3 * var + (1 - lam ** 3) * r * r / 3
    assert abs(fv.sigma_bps - math.sqrt(var) * 1e4) < 1e-9


def test_flow_decays_with_half_life():
    fv = FairValue("X", QuotingCfg(flow_half_life_s=5))
    fv.on_trade(10, False, T0)  # buyer aggressor
    fv.on_trade(4, True, T0)  # seller aggressor
    assert fv.flow(T0) == 6
    assert abs(fv.flow(T0 + 5000) - 3) < 1e-9


def test_fair_value_alpha_zero_is_micro():
    fv = FairValue("X", QuotingCfg())
    fv.on_bbo(99.0, 3.0, 101.0, 1.0, T0)
    f = fv.features(T0)
    assert f is not None and f.fair_value == f.micro


# ---------------- quoting ----------------

def inputs(f=None, pos="0", qty="10", units=5, risk=RiskState.OK, book=BookStatus.OK, now=T0, **kw):
    return QuoteInputs(spec(), f or feats(), Decimal(pos), Decimal(qty), units, risk, book, now, **kw)


def test_avellaneda_stoikov_math_flat_inventory():
    cfg = QuotingCfg(sigma_horizon_s=4.0)
    f = feats(mid=1.0, sigma=2.0)
    d = compute_quotes(cfg, inputs(f), QuoterState())
    sigma_h = 2.0 * 2.0
    hs = max(3.0, 2.0 + 1.0 * sigma_h)  # = 6 bps
    assert d.reason == "ok" and abs(d.half_spread_bps - hs) < 1e-12 and d.reservation == 1.0
    bid = next(q for q in d.quotes if q.side is Side.BUY)
    ask = next(q for q in d.quotes if q.side is Side.SELL)
    assert bid.price == Decimal("0.9994") and ask.price == Decimal("1.0006")  # floor / ceil to tick
    assert bid.qty == Decimal("10") and not bid.reduce_only


def test_floor_ceil_rounding_direction():
    cfg = QuotingCfg(sigma_horizon_s=1.0, min_half_spread_bps=3.3)
    d = compute_quotes(cfg, inputs(feats(sigma=0.1)), QuoterState())
    prices = {q.side: q.price for q in d.quotes}
    assert prices[Side.BUY] == Decimal("0.9996")  # 0.99967 floored
    assert prices[Side.SELL] == Decimal("1.0004")  # 1.00033 ceiled


def test_skew_lowers_reservation_when_long():
    cfg = QuotingCfg()
    long = compute_quotes(cfg, inputs(pos="30"), QuoterState())  # q = 3 units of 5
    short = compute_quotes(cfg, inputs(pos="-30"), QuoterState())
    assert long.reservation < 1.0 < short.reservation
    assert abs(long.q_norm - 0.6) < 1e-12
    sigma_h = 1.0 * math.sqrt(cfg.sigma_horizon_s)
    assert abs(long.reservation - (1 - 0.5 * 0.6 * sigma_h / 1e4)) < 1e-12


def test_inventory_caps():
    cfg = QuotingCfg()
    full_long = compute_quotes(cfg, inputs(pos="50"), QuoterState())
    assert [q.side for q in full_long.quotes] == [Side.SELL]
    full_short = compute_quotes(cfg, inputs(pos="-50"), QuoterState())
    assert [q.side for q in full_short.quotes] == [Side.BUY]


def test_never_crosses_the_touch():
    cfg = QuotingCfg()
    # fair value far above the ask: bid must still be below the best ask
    f = feats(mid=1.0, spread=0.0004, fair=1.01)
    d = compute_quotes(cfg, inputs(f), QuoterState())
    bid = next(q for q in d.quotes if q.side is Side.BUY)
    ask = next(q for q in d.quotes if q.side is Side.SELL)
    assert bid.price < Decimal(repr(f.best_ask)) and ask.price > Decimal(repr(f.best_bid))


def test_guards_pull_quotes():
    cfg = QuotingCfg()
    st = QuoterState()
    assert compute_quotes(cfg, inputs(book=BookStatus.STALE), st).reason == "book_stale"
    assert compute_quotes(cfg, inputs(book=BookStatus.INVALID), st).reason == "book_invalid"
    assert compute_quotes(cfg, inputs(risk=RiskState.HALTED), st).reason == "risk_halted"
    assert compute_quotes(cfg, inputs(user_stream_ok=False), st).reason == "user_stream_down"
    assert compute_quotes(cfg, inputs(feats(ready=False)), st).reason == "sigma_warmup"
    assert compute_quotes(cfg, inputs(units=0), st).reason == "no_size"


def test_vol_circuit_breaker_and_cooldown():
    cfg = QuotingCfg(vol_kill_bps=10, cooldown_s=60)
    st = QuoterState()
    assert compute_quotes(cfg, inputs(feats(sigma=11)), st).reason == "vol_kill"
    assert compute_quotes(cfg, inputs(feats(sigma=1), now=T0 + 30_000), st).reason == "vol_cooldown"
    assert compute_quotes(cfg, inputs(feats(sigma=1), now=T0 + 60_001), st).reason == "ok"


def test_toxicity_ladder_widens_then_pulls_then_recovers():
    cfg = QuotingCfg(toxicity_min_samples=3, toxicity_step_s=10, cooldown_s=60, toxic_markout_bps=-3)
    st = QuoterState()
    base = compute_quotes(cfg, inputs(), st).half_spread_bps
    for _ in range(3):
        st.on_markout_5s(-5.0)
    d1 = compute_quotes(cfg, inputs(now=T0), st)
    assert abs(d1.half_spread_bps - 1.5 * base) < 1e-9
    d2 = compute_quotes(cfg, inputs(now=T0 + 5_000), st)  # too soon for another step
    assert abs(d2.half_spread_bps - 1.5 * base) < 1e-9
    d3 = compute_quotes(cfg, inputs(now=T0 + 10_000), st)
    assert abs(d3.half_spread_bps - 2.0 * base) < 1e-9
    assert compute_quotes(cfg, inputs(now=T0 + 20_000), st).reason == "toxic"
    assert compute_quotes(cfg, inputs(now=T0 + 50_000), st).reason == "toxic"  # cooldown
    d4 = compute_quotes(cfg, inputs(now=T0 + 80_001), st)  # back at 2x with a fresh window
    assert d4.reason == "ok" and abs(d4.half_spread_bps - 2.0 * base) < 1e-9
    for _ in range(3):
        st.on_markout_5s(+2.0)
    d5 = compute_quotes(cfg, inputs(now=T0 + 95_000), st)
    assert abs(d5.half_spread_bps - 1.5 * base) < 1e-9  # de-escalates on good markouts


def test_funding_guard_pull_and_widen():
    pull = QuotingCfg(funding_guard_action="pull", funding_guard_s=60)
    fund = T0 + 30_000
    assert compute_quotes(pull, inputs(next_funding_ms=fund), QuoterState()).reason == "funding_guard"
    assert compute_quotes(pull, inputs(prev_funding_ms=T0 - 59_000), QuoterState()).reason == "funding_guard"
    assert compute_quotes(pull, inputs(next_funding_ms=T0 + 61_000), QuoterState()).reason == "ok"
    widen = QuotingCfg(funding_guard_action="widen", funding_widen_mult=2.0)
    base = compute_quotes(widen, inputs(), QuoterState()).half_spread_bps
    w = compute_quotes(widen, inputs(next_funding_ms=fund), QuoterState())
    assert w.reason == "ok" and abs(w.half_spread_bps - 2 * base) < 1e-12


def test_reduce_only_quotes_only_reducing_side():
    cfg = QuotingCfg()
    d = compute_quotes(cfg, inputs(pos="7", risk=RiskState.REDUCE_ONLY), QuoterState())
    assert [(q.side, q.qty, q.reduce_only) for q in d.quotes] == [(Side.SELL, Decimal("7"), True)]
    d = compute_quotes(cfg, inputs(pos="-30", risk=RiskState.REDUCE_ONLY), QuoterState())
    assert [(q.side, q.qty) for q in d.quotes] == [(Side.BUY, Decimal("10"))]
    assert compute_quotes(cfg, inputs(pos="0", risk=RiskState.REDUCE_ONLY), QuoterState()).quotes == []


def test_multi_level_quotes():
    cfg = QuotingCfg(levels=3, level_spacing_bps=5)
    d = compute_quotes(cfg, inputs(), QuoterState())
    bids = sorted((q for q in d.quotes if q.side is Side.BUY), key=lambda q: q.level)
    assert [q.level for q in bids] == [0, 1, 2]
    assert bids[0].price > bids[1].price > bids[2].price


def test_percent_price_filter_blocks_outlier():
    cfg = QuotingCfg()
    d = compute_quotes(cfg, inputs(mark_price=2.0), QuoterState())  # mark far away -> prices out of band
    assert d.quotes == []
