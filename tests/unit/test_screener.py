import datetime as dt
import json
from decimal import Decimal
from pathlib import Path

from riverbank.core.config import Config
from riverbank.core.events import AggTrade, BookTicker
from riverbank.core.models import SymbolSpec
from riverbank.exchange.specs import parse_exchange_info
from riverbank.marketdata.recorder import Recorder
from riverbank.quoting.sizing import client_id_fits, order_size
from riverbank.universe.history import HistoryMetrics
from riverbank.universe.screener import candidates, evaluate, hard_exclusions, load_blacklist

FIX = Path(__file__).resolve().parents[1] / "fixtures"
NOW = 1791763200000  # 2026-10-12T00:00Z
DAY = 86_400_000


def spec(sym="AAAUSDT", **kw):
    base = dict(symbol=sym, tick=Decimal("0.0001"), step=Decimal("1"), min_qty=Decimal("1"),
                min_notional=Decimal("5"), price_precision=4, qty_precision=0, onboard_date_ms=NOW - 100 * DAY,
                time_in_force=("GTC", "GTX"))
    base.update(kw)
    return SymbolSpec(**base)


def ticker(qv=50e6, hi=1.05, lo=0.95, last=1.0, count=144000):
    return {"quoteVolume": str(qv), "highPrice": str(hi), "lowPrice": str(lo), "lastPrice": str(last),
            "count": count}


def book(bid=0.9996, ask=1.0004):
    return {"bidPrice": str(bid), "askPrice": str(ask)}


def test_hard_exclusions_cover_every_rule():
    cfg = Config()
    assert hard_exclusions(spec("BTCUSDT"), cfg, set(), NOW) == ["hedge_only"]
    assert "status=SETTLING" in hard_exclusions(spec(status="SETTLING"), cfg, set(), NOW)
    assert "contract=TRADIFI_PERPETUAL" in hard_exclusions(spec(contract_type="TRADIFI_PERPETUAL"), cfg, set(), NOW)
    assert "quote=USDC" in hard_exclusions(spec(quote_asset="USDC"), cfg, set(), NOW)
    assert "underlying=EQUITY" in hard_exclusions(spec(underlying_type="EQUITY"), cfg, set(), NOW)
    assert "listed<30d" in hard_exclusions(spec(onboard_date_ms=NOW - 5 * DAY), cfg, set(), NOW)
    assert "blacklist" in hard_exclusions(spec(), cfg, {"AAAUSDT"}, NOW)
    assert "non_ascii_symbol" in hard_exclusions(spec("龙虾USDT"), cfg, set(), NOW)
    assert "position_control=LONG" in hard_exclusions(spec(position_control_side="LONG"), cfg, set(), NOW)
    assert "no_post_only" in hard_exclusions(spec(time_in_force=("GTC",)), cfg, set(), NOW)
    assert hard_exclusions(spec(), cfg, set(), NOW) == []


def test_real_exchange_info_hedge_and_non_perp_excluded():
    info = parse_exchange_info(json.loads((FIX / "exchange_info_sample.json").read_text()))
    cfg = Config()
    tick = {s: ticker() for s in info.specs}
    books = {s: book() for s in info.specs}
    rows = {r.symbol: r for r in evaluate(info.specs, tick, books, cfg, set(), NOW)}
    assert "hedge_only" in rows["BTCUSDT"].reasons and "hedge_only" in rows["ETHUSDT"].reasons
    assert not rows["XAUUSDT"].eligible and not rows["BTCUSDC"].eligible and not rows["BTCUSDT_261225"].eligible
    assert not rows["OPENAIUSDT"].eligible


def test_eligibility_metrics_and_ranking():
    cfg = Config()
    specs = {"AAAUSDT": spec("AAAUSDT"), "BBBUSDT": spec("BBBUSDT"), "TIGHTUSDT": spec("TIGHTUSDT"),
             "HUGEUSDT": spec("HUGEUSDT"), "TOXUSDT": spec("TOXUSDT")}
    tick = {"AAAUSDT": ticker(), "BBBUSDT": ticker(count=14400), "TIGHTUSDT": ticker(),
            "HUGEUSDT": ticker(qv=900e6), "TOXUSDT": ticker(hi=2.0, lo=0.5)}
    books = {s: book() for s in specs}
    books["TIGHTUSDT"] = book(0.99995, 1.00005)  # 1 bps spread < 1.5 x 4 bps
    rows = evaluate(specs, tick, books, cfg, set(), NOW)
    by = {r.symbol: r for r in rows}
    a = by["AAAUSDT"]
    assert a.eligible and abs(a.spread_bps - 8.0) < 1e-6 and abs(a.tick_bps - 1.0) < 1e-6
    assert abs(a.quote_vol_musd - 50) < 1e-9 and abs(a.range_bps - 1000) < 1e-6
    assert abs(a.toxicity_proxy - 125) < 1e-6 and abs(a.fill_opportunity - 100) < 1e-9
    assert abs(a.edge_ratio - 2.0) < 1e-9
    assert a.order_qty == "10" and a.inventory_units == 5
    assert "spread<1.5x_fee" in by["TIGHTUSDT"].reasons
    assert by["HUGEUSDT"].reasons == ["volume_out_of_range"]
    assert by["TOXUSDT"].reasons == ["toxic"]
    assert candidates(rows) == ["AAAUSDT", "BBBUSDT"]  # ranked by score (fill opportunity)
    assert rows[0].rank == 1 and rows[1].rank == 2


def test_candidate_cap_and_blacklist_file(tmp_path):
    cfg = Config.model_validate({"universe": {"max_candidate_symbols": 1}})
    specs = {s: spec(s) for s in ("AAAUSDT", "BBBUSDT", "CCCUSDT")}
    bl = tmp_path / "bl.yaml"
    bl.write_text("symbols:\n  - cccusdt\n")
    rows = evaluate(specs, {s: ticker() for s in specs}, {s: book() for s in specs}, cfg, load_blacklist(bl), NOW)
    assert sum(r.candidate for r in rows) == 1
    assert "blacklist" in next(r for r in rows if r.symbol == "CCCUSDT").reasons
    assert load_blacklist(tmp_path / "missing.yaml") == set()


def test_order_size_rules():
    s = spec(step=Decimal("1"), min_notional=Decimal("5"))
    ok = order_size(s, 1.0, 10, 2.0, 50, 5)
    assert ok.ok and ok.qty == Decimal("10") and ok.inventory_units == 5
    coarse = order_size(spec(step=Decimal("1")), 60.0, 10, 2.0, 50, 5)  # 10/60 -> 0 contracts
    assert not coarse.ok and coarse.reason == "below_min_qty"
    big_min = order_size(spec(min_notional=Decimal("20"), step=Decimal("0.01")), 10.0, 10, 2.0, 50, 5)
    assert big_min.ok and big_min.notional == 40.0 and big_min.inventory_units == 1
    too_big = order_size(spec(min_notional=Decimal("100"), step=Decimal("0.001")), 10.0, 10, 2.0, 50, 5)
    assert not too_big.ok and too_big.reason == "order_exceeds_max_position"
    assert client_id_fits("CSOPSKHYNIX2LUSDT") and not client_id_fits("X" * 25)


async def test_history_metrics_preferred_over_snapshot(tmp_path):
    rec = Recorder(tmp_path)
    d0 = NOW - DAY  # complete previous day
    for i in range(0, 13 * 3600, 60):  # 13 h of data, 1 sample / min
        ts = d0 + i * 1000
        mid = 1.0 + (0.01 if i > 6 * 3600 else 0.0)
        rec.on_event(BookTicker("AAAUSDT", i + 1, ts, ts, mid - 0.0002, 1, mid + 0.0002, 1, ts))
        rec.on_event(AggTrade("AAAUSDT", i + 1, mid, 100.0, 100.0, ts, ts, False, ts))
    await rec.flush()
    hm = HistoryMetrics(tmp_path, tmp_path / "cache.json")
    m, ndays = hm.metrics("AAAUSDT", NOW + 3600_000, 7)
    assert ndays == 1 and abs(m["spread_bps"] - 4.0) < 0.1 and abs(m["range_bps"] - 100) < 2
    assert abs(m["fill_opportunity"] - 1.0) < 0.01
    assert (tmp_path / "cache.json").exists()
    cfg = Config()
    rows = evaluate({"AAAUSDT": spec()}, {"AAAUSDT": ticker()}, {"AAAUSDT": book()}, cfg, set(), NOW,
                    history=lambda s: hm.metrics(s, NOW + 3600_000, 7))
    assert rows[0].source == "history" and rows[0].history_days == 1
    assert "spread<1.5x_fee" in rows[0].reasons  # 4 bps historical median < 6 bps, snapshot said 8


class FakeRest:
    def __init__(self, info_json, fail_first=False):
        self.info_json = info_json
        self.calls = 0
        self.fail_first = fail_first
        self.public_book = book()

    async def public(self, method, path, params=None):
        self.calls += 1
        if self.fail_first and self.calls == 1:
            raise RuntimeError("network down")
        if path.endswith("exchangeInfo"):
            return self.info_json
        syms = [s["symbol"] for s in self.info_json["symbols"]]
        if path.endswith("24hr"):
            return [dict(ticker(), symbol=s) for s in syms]
        return [dict(self.public_book, symbol=s) for s in syms]


async def test_scheduled_screener_survives_errors(tmp_path, monkeypatch):
    import asyncio

    from riverbank.universe import screener as scr

    info_json = json.loads((FIX / "exchange_info_sample.json").read_text())
    cfg = Config.model_validate({"ops": {"state_dir": str(tmp_path), "data_dir": str(tmp_path)}})
    s = scr.Screener(cfg, FakeRest(info_json, fail_first=True))
    results = []
    sleeps = []

    async def fake_sleep(sec):
        sleeps.append(sec)
        if len(sleeps) >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(scr.asyncio, "sleep", fake_sleep)
    try:
        await s.run(lambda rows: results.append(rows))
    except asyncio.CancelledError:
        pass
    assert len(results) == 1  # first cycle failed (network), second produced a universe
    assert sleeps == [cfg.universe.screen_interval_s] * 2
    assert (tmp_path / "screen_latest.json").exists()


async def test_one_noisy_book_snapshot_does_not_drop_a_candidate(tmp_path):
    # Thin books flip between 1 and 2 ticks: a single bookTicker snapshot made shadow candidates churn
    # every screen. Without recorded history, the spread is the median of recent snapshots.
    from riverbank.universe import screener as scr

    info_json = json.loads((FIX / "exchange_info_sample.json").read_text())
    cfg = Config.model_validate({"ops": {"state_dir": str(tmp_path), "data_dir": str(tmp_path)}})
    rest = FakeRest(info_json)
    s = scr.Screener(cfg, rest)
    first = candidates(await s.screen_once(NOW))  # 8 bps spread: >= 1.5 x 4 bps round-trip fee
    assert first
    await s.screen_once(NOW + 300_000)
    rest.public_book = book(bid=0.9998, ask=1.0002)  # one 4 bps snapshot: below 6 bps on its own
    assert candidates(await s.screen_once(NOW + 600_000)) == first


def test_hysteresis_keeps_an_incumbent_near_the_entry_threshold():
    # Enter at spread >= 1.5 x fee (6 bps) and toxicity <= 400; an incumbent stays down to 0.85 of that
    # (spread >= 5.1 bps, toxicity <= 470), so a symbol sitting on the line does not flap in and out.
    cfg = Config()
    specs = {s: spec(s) for s in ("NEARUSDT", "LOWUSDT", "TOXUSDT")}
    tick = {"NEARUSDT": ticker(), "LOWUSDT": ticker(), "TOXUSDT": ticker(hi=1.176, lo=0.824)}  # tox 440 at 8 bps
    books = {"NEARUSDT": book(0.99973, 1.00027), "LOWUSDT": book(0.99978, 1.00022), "TOXUSDT": book()}
    assert candidates(evaluate(specs, tick, books, cfg, set(), NOW)) == []  # 5.4 bps, 4.4 bps, tox 440
    rows = evaluate(specs, tick, books, cfg, set(), NOW, incumbents=set(specs))
    assert sorted(candidates(rows)) == ["NEARUSDT", "TOXUSDT"]
    assert "spread<1.5x_fee" in next(r for r in rows if r.symbol == "LOWUSDT").reasons


async def test_screener_passes_its_last_candidates_as_incumbents(tmp_path):
    from riverbank.universe import screener as scr

    info_json = json.loads((FIX / "exchange_info_sample.json").read_text())
    cfg = Config.model_validate({"ops": {"state_dir": str(tmp_path), "data_dir": str(tmp_path)}})
    rest = FakeRest(info_json)
    s = scr.Screener(cfg, rest)
    first = candidates(await s.screen_once(NOW))
    rest.public_book = book(0.99973, 1.00027)  # 5.4 bps: below entry, above the incumbent floor
    for i in range(1, 2 * scr.SPREAD_SAMPLES):
        assert candidates(await s.screen_once(NOW + i * 300_000)) == first
    rest.public_book = book(0.99978, 1.00022)  # 4.4 bps: below the floor too
    for i in range(2 * scr.SPREAD_SAMPLES, 4 * scr.SPREAD_SAMPLES):
        last = candidates(await s.screen_once(NOW + i * 300_000))
    assert last == []


async def test_history_read_survives_compaction_between_listing_and_reading(tmp_path, monkeypatch):
    # Shadow 2026-10-12 20:00:11: the recorder compacted hour 19's parts (rename + delete) after the screener
    # listed them, so reading a listed part raised FileNotFoundError and the screen failed. Skipping the missing
    # part would drop an hour, and a completed day is cached for good: re-list and read again instead.
    from riverbank.universe import history

    rec = Recorder(tmp_path)
    d0 = NOW - DAY
    day0 = dt.datetime.fromtimestamp(d0 / 1000, dt.UTC).date().isoformat()
    for i in range(0, 13 * 3600, 60):
        ts = d0 + i * 1000
        rec.on_event(BookTicker("AAAUSDT", i + 1, ts, ts, 0.9998, 1, 1.0002, 1, ts))
        if i % 3600 == 0:
            await rec.flush()  # several part files per hour, like the live recorder
    await rec.flush()
    stale = {s: history.list_files(tmp_path, s, "AAAUSDT", [day0])
             for s in ("bookTicker", "aggTrade")}
    await rec.compact(NOW)  # parts -> HH.parquet, parts deleted
    calls = []

    def listing(root, stream, symbol=None, dates=None):
        calls.append(stream)
        return stale[stream] if calls.count(stream) == 1 else real_list(root, stream, symbol, dates)

    real_list = history.list_files
    monkeypatch.setattr(history, "list_files", listing)
    m = history.day_metrics(tmp_path, "AAAUSDT", day0)
    assert m is not None and abs(m["spread_bps"] - 4.0) < 0.1 and m["coverage_h"] > 12.9
