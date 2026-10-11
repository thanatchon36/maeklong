import math
from decimal import Decimal

from riverbank.analytics.markout import MarkoutTracker, markout_bps, stats
from riverbank.analytics.pnl import decompose, total
from riverbank.core.config import RiskCfg
from riverbank.core.models import Fill, Order, OrderStatus, QuoteIntent, RiskState, Side
from riverbank.orders.positions import PositionBook
from riverbank.risk.engine import RiskEngine
from riverbank.risk.limits import approx_isolated_liq_price
from tests.helpers import T0

D = Decimal


def fill(side, px, qty, fee="0", ts=T0, sym="AAAUSDT", mid=0.0):
    return Fill(ts, sym, side, D(px), D(qty), D(fee), True, "shadow", "rb-x", mid, "1")


def intent(side=Side.BUY, qty="10", sym="AAAUSDT", reduce_only=False, reason="quote"):
    return QuoteIntent(sym, side, D("1"), D(qty), "rb-AAAUSDT-BUY-1", reason, reduce_only)


def risk(cfg=None, equity=300.0, orders=None, beta=None, events=None, halts=None, kv=None):
    pb = PositionBook(equity)
    ev = events if events is not None else []
    store = kv if kv is not None else {}
    r = RiskEngine(cfg or RiskCfg(), "paper", pb, lambda: orders or [], order_limit_10s=300, leverage=3,
                   beta=beta, on_event=ev.append, persist_halt=(halts.append if halts is not None else None),
                   kv_get=store.get, kv_set=store.__setitem__)
    return r, pb, ev


def open_order(side, qty, sym="AAAUSDT"):
    return Order("rb-o", 1, sym, side, D(1), D(qty), D(0), OrderStatus.NEW, T0, T0)


# ---------------- positions ----------------

def test_position_avg_entry_realized_flip_and_equity():
    pb = PositionBook(100.0)
    pb.apply_fill(fill(Side.BUY, "10", "2", fee="0.01"))
    pb.apply_fill(fill(Side.BUY, "13", "1"))
    p = pb.get("AAAUSDT")
    assert p.qty == 3 and p.entry_price == D(11)
    r = pb.apply_fill(fill(Side.SELL, "12", "4"))  # close 3 at +1 each, open 1 short at 12
    assert r == D(3) and p.qty == -1 and p.entry_price == D(12)
    pb.set_mark("AAAUSDT", 11.0)
    assert pb.unrealized("AAAUSDT") == 1.0
    pb.apply_funding("AAAUSDT", D("0.5"), T0)
    assert math.isclose(pb.equity(), 100 + 3 - 0.01 + 0.5 + 1.0)
    assert pb.gross_exposure() == 11.0


# ---------------- risk: pre-trade limits (one breach test per limit) ----------------

def test_max_position_limit():
    r, pb, _ = risk(RiskCfg(max_position_usdt=50))
    pb.set_mark("AAAUSDT", 1.0)
    assert r.check(intent(qty="40"), 1.0, T0).approved
    pb.apply_fill(fill(Side.BUY, "1", "40"))
    d = r.check(intent(qty="20"), 1.0, T0)
    assert not d.approved and d.reason == "max_position"


def test_max_position_counts_open_orders_worst_case():
    r, _pb, _ = risk(RiskCfg(max_position_usdt=50), orders=[open_order(Side.BUY, 45)])
    assert r.check(intent(qty="10"), 1.0, T0).reason == "max_position"


def test_max_gross_exposure_limit():
    r, pb, _ = risk(RiskCfg(max_gross_exposure_usdt=60, max_position_usdt=50))
    pb.apply_fill(fill(Side.BUY, "1", "45", sym="BBBUSDT"))
    pb.set_mark("BBBUSDT", 1.0)
    assert r.check(intent(qty="20"), 1.0, T0).reason == "max_gross_exposure"


def test_max_net_beta_limit_and_reducing_hedge_allowed():
    r, pb, _ = risk(RiskCfg(max_net_beta_exposure_usdt=30, max_position_usdt=50, max_gross_exposure_usdt=500),
                    beta=lambda s: 2.0)
    pb.set_mark("AAAUSDT", 1.0)
    assert r.check(intent(qty="16"), 1.0, T0).reason == "max_net_beta_exposure"  # 16*2 > 30
    pb.apply_fill(fill(Side.BUY, "1", "14"))  # net beta 28
    assert r.check(intent(side=Side.SELL, qty="5", sym="BTCUSDT", reason="hedge"), 1.0, T0).approved


def test_max_open_orders_limit():
    r, _, _ = risk(RiskCfg(max_open_orders=2), orders=[open_order(Side.BUY, 1), open_order(Side.SELL, 1)])
    assert r.check(intent(qty="1"), 1.0, T0).reason == "max_open_orders"


def test_order_rate_limit_is_fraction_of_exchange_limit():
    r, _, _ = risk(RiskCfg(max_order_rate_frac=0.01, max_open_orders=100))  # 1% of 300 = 3 per 10 s
    assert all(r.check(intent(qty="1"), 1.0, T0 + i).approved for i in range(3))
    assert r.check(intent(qty="1"), 1.0, T0 + 5).reason == "order_rate"
    assert r.check(intent(qty="1"), 1.0, T0 + 10_010).approved


def test_max_leverage_limit():
    r, _pb, _ = risk(RiskCfg(max_leverage=1.0, max_position_usdt=500, max_gross_exposure_usdt=500,
                            max_net_beta_exposure_usdt=500), equity=20.0)
    assert r.check(intent(qty="25"), 1.0, T0).reason == "max_leverage"


def test_reduce_only_state_allows_only_reducing_orders():
    r, pb, _ = risk()
    pb.apply_fill(fill(Side.BUY, "1", "10"))
    r.state = RiskState.REDUCE_ONLY
    assert r.check(intent(side=Side.BUY, qty="1"), 1.0, T0).reason == "reduce_only_state"
    assert r.check(intent(side=Side.SELL, qty="10", reduce_only=True), 1.0, T0).approved
    assert r.check(intent(side=Side.SELL, qty="11", reduce_only=True), 1.0, T0).reason == "reduce_only_state"


def test_halted_rejects_everything_but_flatten():
    r, pb, ev = risk()
    pb.apply_fill(fill(Side.BUY, "1", "10"))
    r.halt("test", T0)
    assert r.check(intent(qty="1"), 1.0, T0).reason == "halted"
    assert r.check(intent(side=Side.SELL, qty="10", reduce_only=True, reason="flatten"), 1.0, T0).approved
    assert ev[-1].state_after is RiskState.HALTED


# ---------------- risk: periodic evaluation ----------------

def test_daily_loss_limit_halts_and_alert_at_half():
    halts = []
    r, pb, ev = risk(RiskCfg(daily_loss_limit_pct=10), equity=100.0, halts=halts)
    r.evaluate(T0)
    pb.apply_fill(fill(Side.BUY, "1", "10", fee="5.5"))
    r.evaluate(T0 + 1000)
    assert any(e.rule == "daily_loss_alert" for e in ev) and r.state is RiskState.OK
    pb.apply_fill(fill(Side.SELL, "1", "10", fee="2.1"))  # 7.6% loss > 75% of 10% -> soft
    r.evaluate(T0 + 2000)
    assert r.state is RiskState.REDUCE_ONLY and "daily_loss" in r.breaches
    pb.apply_fill(fill(Side.BUY, "1", "1", fee="3.0"))  # 10.6% loss
    r.evaluate(T0 + 3000)
    assert r.state is RiskState.HALTED and halts and "daily loss" in halts[0]


def test_max_drawdown_halts():
    r, pb, _ = risk(RiskCfg(max_drawdown_pct=15, daily_loss_limit_pct=50), equity=100.0)
    r.evaluate(T0)
    r.peak_equity = 120.0  # earlier peak (e.g. persisted)
    pb.apply_fill(fill(Side.BUY, "1", "1", fee="2.0"))  # equity 98 -> dd 18.3 %
    r.evaluate(T0 + 1000)
    assert r.state is RiskState.HALTED


def test_soft_breach_reduce_only_and_hysteresis_recovery():
    r, pb, _ = risk(RiskCfg(max_position_usdt=50, resume_frac=0.9))
    r.leverage = 2.0  # at 3x isolated a fresh position is ~31.6% from liquidation (> 30% min, < 33.3% resume)
    pb.apply_fill(fill(Side.BUY, "1", "52"))
    pb.set_mark("AAAUSDT", 1.0)
    r.evaluate(T0)
    assert r.state is RiskState.REDUCE_ONLY and r.breaches == ["max_position"]
    pb.apply_fill(fill(Side.SELL, "1", "6"))  # 46 -> 92% of limit: still REDUCE_ONLY (hysteresis)
    r.evaluate(T0 + 1000)
    assert r.state is RiskState.REDUCE_ONLY
    pb.apply_fill(fill(Side.SELL, "1", "2"))  # 44 -> 88%
    r.evaluate(T0 + 2000)
    assert r.state is RiskState.OK


def test_liquidation_distance_soft_breach():
    r, pb, _ = risk(RiskCfg(min_liq_distance_pct=30, max_position_usdt=500, max_gross_exposure_usdt=500,
                            max_net_beta_exposure_usdt=500))
    pb.apply_fill(fill(Side.BUY, "100", "0.1"))
    pb.set_mark("AAAUSDT", 97.0)  # ~2.4% adverse at 3x isolated
    r.evaluate(T0)
    assert r.state is RiskState.REDUCE_ONLY and "liq_distance" in r.breaches
    assert 68 < approx_isolated_liq_price(1, 100, 3) < 69


def test_halt_is_sticky_and_persists_peak():
    kv = {}
    r, _pb, _ = risk(kv=kv, equity=100.0)
    r.evaluate(T0)
    assert kv["risk:paper:peak_equity"].startswith("100")
    r.halt("x", T0)
    r.evaluate(T0 + 1000)
    r._transition(RiskState.OK, "manual?", "", T0)
    assert r.state is RiskState.HALTED


# ---------------- analytics ----------------

def test_markout_definition_and_tracker():
    assert markout_bps(1, 100.0, 100.1) == 10.000000000000568 or abs(markout_bps(1, 100.0, 100.1) - 10) < 1e-9
    assert abs(markout_bps(-1, 100.0, 100.1) + 10) < 1e-9
    mt = MarkoutTracker(horizons=(1, 5))
    resolved = []
    mt.listeners.append(lambda rec, h, v: resolved.append((h, round(v, 6))))
    f = fill(Side.BUY, "100", "1", fee="0.02", mid=100.05)
    rec = mt.on_fill(f, "paper")
    assert abs(rec.fee_bps - 2.0) < 1e-9 and abs(rec.spread_capture_bps - 5.0) < 1e-9
    mt.resolve(T0 + 999, lambda s: 100.2)
    assert resolved == []
    mt.resolve(T0 + 1000, lambda s: 100.2)
    mt.resolve(T0 + 5000, lambda s: 99.9)
    assert resolved == [(1, 20.0), (5, -10.0)]
    assert abs(rec.edge(1) - 18.0) < 1e-9
    st = mt.symbol_stats("AAAUSDT", 1)
    assert st.n == 1 and abs(st.mean - 18.0) < 1e-9


def test_stats_mean_se():
    s = stats([1.0, 2.0, 3.0, 4.0])
    assert s.n == 4 and s.mean == 2.5 and abs(s.se - math.sqrt(5 / 3) / 2) < 1e-12
    assert stats([]).n == 0


def test_pnl_decomposition_by_hand():
    fills_ = [{"symbol": "AAAUSDT", "side": "BUY", "price": "99", "qty": "1", "mid_at_fill": 100.0},
              {"symbol": "AAAUSDT", "side": "SELL", "price": "102", "qty": "1", "mid_at_fill": 101.0},
              {"symbol": "BTCUSDT", "side": "SELL", "price": "50000", "qty": "0.001", "mid_at_fill": 50000.0}]
    start = {}
    end = {"AAAUSDT": {"realized": 3.0, "unrealized": 0.0, "fees": 0.08, "funding": -0.01},
           "BTCUSDT": {"realized": 0.0, "unrealized": -0.5, "fees": 0.02, "funding": 0.0}}
    rows = decompose(fills_, start, end, hedge_symbols=["BTCUSDT"])
    a = rows["AAAUSDT"]
    assert a.spread == 2.0 and a.inventory == 1.0 and abs(a.net - (3.0 - 0.08 - 0.01)) < 1e-12
    b = rows["BTCUSDT"]
    assert b.hedge == -0.5 and b.spread == 0.0
    t = total(rows)
    assert abs(t.net - (a.net + b.net)) < 1e-12 and t.n_fills == 3
