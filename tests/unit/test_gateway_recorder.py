import gzip
from pathlib import Path

import orjson
import pyarrow.parquet as pq

from riverbank.core.events import AggTrade, BookTicker, BookTopN, DepthDiff, MarkPrice
from riverbank.core.models import Mode
from riverbank.exchange.environments import ENVIRONMENTS
from riverbank.marketdata.gateway import combined_url, parse_message, shard, stream_names
from riverbank.marketdata.recorder import Recorder, list_files, read_stream, recorded_symbols

FIX = Path(__file__).resolve().parents[1] / "fixtures"

# Captured from wss://fstream.binance.com/{public,market} on 2026-10-12.
BT = ('{"stream":"dogeusdt@bookTicker","data":{"e":"bookTicker","u":11790587783194,"s":"DOGEUSDT","ps":"DOGEUSDT",'
      '"b":"0.086350","B":"604722","a":"0.086360","A":"29874","T":1791753816442,"E":1791753816443,"st":1}}')
AGG = ('{"stream":"dogeusdt@aggTrade","data":{"e":"aggTrade","E":1791753817274,"a":1039800971,"s":"DOGEUSDT",'
       '"p":"0.086350","q":"59","nq":"58","f":3507460762,"l":3507460762,"T":1791753817123,"m":true,"st":1}}')
MARK = ('{"stream":"dogeusdt@markPrice@1s","data":{"e":"markPriceUpdate","E":1791753817000,"s":"DOGEUSDT",'
        '"p":"0.08635000","ap":"0.08635000","P":"0.08679993","i":"0.08638537","r":"0.00006790","T":1791763200000,'
        '"st":1}}')


def test_parse_real_messages():
    bt = parse_message(BT, 5)
    assert isinstance(bt, BookTicker) and bt.bid_px == 0.08635 and bt.ask_qty == 29874 and bt.recv_ts == 5
    agg = parse_message(AGG, 5)
    assert isinstance(agg, AggTrade) and agg.buyer_maker and agg.qty == 59 and agg.nq == 58
    mk = parse_message(MARK, 5)
    assert isinstance(mk, MarkPrice) and mk.next_funding_ms == 1791763200000 and mk.funding_rate == 0.0000679
    with gzip.open(FIX / "depth_capture.jsonl.gz") as f:
        line = next(x for x in f if x.startswith(b'{"t":"ws"'))
    d = parse_message(orjson.loads(line)["text"], 1)
    assert isinstance(d, DepthDiff) and d.u >= d.U and d.pu < d.U


def test_parse_ignores_unknown_and_raw_payloads():
    assert parse_message('{"result":null,"id":1}', 0) is None
    raw = orjson.loads(BT)["data"]
    assert isinstance(parse_message(orjson.dumps(raw), 0), BookTicker)


def test_routed_urls_and_sharding():
    env = ENVIRONMENTS[Mode.SHADOW]
    assert stream_names(["DOGEUSDT"], "public") == ["dogeusdt@depth@100ms", "dogeusdt@bookTicker"]
    assert stream_names(["DOGEUSDT"], "market") == ["dogeusdt@aggTrade", "dogeusdt@markPrice@1s"]
    assert combined_url(env, "public", ["A"]).startswith("wss://fstream.binance.com/public/stream?streams=")
    assert combined_url(env, "market", ["A"]).startswith("wss://fstream.binance.com/market/stream?streams=")
    demo = ENVIRONMENTS[Mode.DEMO]
    assert combined_url(demo, "public", ["A"]).startswith("wss://demo-fstream.binance.com/public/")
    shards = shard([f"S{i:02d}" for i in range(23)], 10)
    assert [len(s) for s in shards] == [10, 10, 3]


async def test_recorder_roundtrip_compaction_and_retention(tmp_path):
    rec = Recorder(tmp_path, flush_s=1, record_depth=True, retention_days=30)
    t0 = 1791763200000  # 2026-10-12T00:00:00Z (exact hour boundary)
    for i in range(5):
        rec.on_event(BookTicker("DOGEUSDT", i + 1, t0, t0, 0.1, 1.0, 0.2, 2.0, t0 + i * 100))
        rec.on_event(AggTrade("DOGEUSDT", i + 1, 0.1, 3.0, 3.0, t0, t0, i % 2 == 0, t0 + i * 100))
    rec.on_event(MarkPrice("DOGEUSDT", t0, 0.1, 0.1, 0.0001, t0 + 3600_000, t0))
    rec.on_event(BookTopN("DOGEUSDT", 7, [(0.1, 1.0), (0.09, 2.0)], [(0.2, 1.0)], t0))
    rec.on_event(DepthDiff("DOGEUSDT", t0, t0, 1, 2, 0, [(0.1, 1.0)], [], t0))
    await rec.flush()
    rec.on_event(BookTicker("DOGEUSDT", 99, t0, t0, 0.1, 1.0, 0.2, 2.0, t0 + 3600_000 + 5))  # next hour
    await rec.flush()
    files = list_files(tmp_path, "bookTicker", "DOGEUSDT")
    assert len(files) == 2 and all(".part-" in f.name for f in files)
    assert all(pq.read_table(f).num_rows > 0 for f in files)  # every part file readable
    await rec.compact(t0 + 3600_000 + 10)  # first hour finished -> compacted
    files = list_files(tmp_path, "bookTicker", "DOGEUSDT")
    assert sorted(f.name for f in files) == ["00.parquet", "01.part-000002.parquet"]
    t = read_stream(tmp_path, "bookTicker", "DOGEUSDT", t0, t0 + 3600_000)
    assert t.num_rows == 5 and t.column("u").to_pylist() == [1, 2, 3, 4, 5]
    assert read_stream(tmp_path, "book", "DOGEUSDT").column("bids_px").to_pylist() == [[0.1, 0.09]]
    assert read_stream(tmp_path, "aggTrade", "DOGEUSDT").num_rows == 5
    assert read_stream(tmp_path, "depth", "DOGEUSDT").num_rows == 1
    assert recorded_symbols(tmp_path) == ["DOGEUSDT"]
    assert "date=2026-10-12" in str(files[0]) and "symbol=DOGEUSDT" in str(files[0])
    removed = await rec.enforce_retention(t0 + 31 * 86400_000)
    assert removed >= 1 and list_files(tmp_path, "bookTicker", "DOGEUSDT") == []


async def test_recorder_skips_depth_unless_enabled(tmp_path):
    rec = Recorder(tmp_path)
    rec.on_event(DepthDiff("X", 0, 0, 1, 2, 0, [], [], 1791753600000))
    assert rec.pending_rows() == 0
