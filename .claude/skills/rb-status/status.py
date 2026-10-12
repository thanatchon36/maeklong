"""Gather the status of the demo, shadow and M1 runs in one read-only pass (python3 >= 3.9, no deps).

Prints compact sections for the rb-status skill to format. Never touches state/*.sqlite.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
WINDOWS = json.loads((Path(__file__).parent / "windows.json").read_text())
UTC = dt.timezone.utc  # noqa: UP017 - runs on the host python3 (3.9)
NOW = dt.datetime.now(UTC)
BAD = re.compile(r'ERROR|CRITICAL|Traceback|"level": ?"(error|critical)"')
RATE = re.compile(r'"status": ?(429|418)|rate_limit_halt')  # bare 418/429 also match prices
SHADOW_EQUITY0 = 300.0  # ops.shadow_equity_usdt


def sh(*cmd: str) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return r.stdout + r.stderr


def events(lines: list[str], name: str) -> list[dict]:
    out = []
    for ln in lines:
        if f'"event": "{name}"' in ln:
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
    return out


def prom(query: str) -> list[tuple[dict, float]]:
    try:
        url = "http://127.0.0.1:9090/api/v1/query?" + urllib.parse.urlencode({"query": query})
        with urllib.request.urlopen(url, timeout=10) as r:
            return [(x["metric"], float(x["value"][1])) for x in json.load(r)["data"]["result"]]
    except Exception as e:
        print(f"  prometheus error for {query}: {e!r}")
        return []


def section(title: str) -> None:
    print(f"\n## {title}")


def containers() -> dict[str, list[str]]:
    section(f"containers (now {NOW:%Y-%m-%d %H:%M} UTC = {NOW + dt.timedelta(hours=7):%H:%M} Thai)")
    logs = {}
    for name, w in WINDOWS.items():
        info = sh("docker", "inspect", "-f", "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}|"
                  "{{.State.StartedAt}}|{{.RestartCount}}|{{.State.ExitCode}}", name).strip()
        if info.count("|") != 4:
            print(f"- {w['label']}: docker inspect failed: {info[:200]}")
            continue
        status, health, started, restarts, code = info.split("|")
        start = dt.datetime.fromisoformat(w["start"]).replace(tzinfo=UTC)
        end = start + dt.timedelta(hours=w["hours"])
        reset = not started.startswith(w["start"]) or restarts != "0"
        text = sh("docker", "logs", name)
        logs[name] = text.splitlines()
        print(f"- {w['label']}: {status} {health} restarts={restarts} exit={code} "
              f"elapsed={(NOW - start).total_seconds() / 3600:.2f}h/{w['hours']}h ends={end:%Y-%m-%d %H:%M}Z "
              f"errors={sum(bool(BAD.search(x)) for x in logs[name])} "
              f"http429_418={sum(bool(RATE.search(x)) for x in logs[name])}"
              + (f" WINDOW_RESET(StartedAt {started[:19]} != {w['start']})" if reset else ""))
    return logs


def demo(lines: list[str]) -> None:
    section("demo (fake money; PnL meaningless)")
    fills = events(lines, "fill")
    by_venue = Counter(f.get("venue") for f in fills)
    last = max((f["timestamp"] for f in fills if f.get("venue") == "demo"), default="none")
    print(f"- fills: demo={by_venue['demo']} twin={by_venue['shadow']} last_demo_fill={last[:19]}")
    kinds = ("hedge", "hedge_rejected", "hedge_too_coarse", "risk_event", "recon_mismatch", "user_stream_down",
             "md_disconnect", "engine_exception", "external_fill", "trading_rules")
    print("- events: " + ", ".join(f"{k}={len(events(lines, k))}" for k in kinds))
    for e in events(lines, "risk_event")[-3:]:
        print(f"  risk {e['timestamp'][11:19]} {e.get('venue')} {e.get('rule')} {e.get('detail')} "
              f"{e.get('before')}->{e.get('after')}")
    for e in events(lines, "hedge_rejected")[-3:]:
        print(f"  hedge_rejected {e['timestamp'][11:19]} {e.get('venue')} {e.get('side')} {e.get('reason')}")
    ready = events(lines, "real_venue_ready")
    wallet0 = float(ready[0]["wallet"]) if ready else float("nan")
    eq = {m.get("venue"): v for m, v in prom('rb_equity_usdt{bot="demo"}')}
    print(f"- pnl: demo {eq.get('demo', float('nan')) - wallet0:+.2f} USDT (wallet0 {wallet0:.2f}), "
          f"twin {eq.get('shadow', float('nan')) - SHADOW_EQUITY0:+.2f} USDT")
    inv = {m.get("symbol"): round(v, 1) for m, v in prom('rb_inventory_usdt{bot="demo",venue="demo"}')}
    oo = {m.get("symbol"): int(v) for m, v in prom('rb_open_orders{bot="demo",venue="demo"}')}
    print(f"- inventory_usdt={inv} open_orders={oo}")


def shadow(lines: list[str]) -> None:
    section("shadow (paper on production prices; the PnL that matters)")
    fills = Counter(e.get("symbol") for e in events(lines, "fill"))
    prev: set[str] | None = None
    ever: set[str] = set()
    churn = []
    for e in events(lines, "universe_set"):
        s = set(e["symbols"])
        ever |= s
        if prev is not None:
            churn.append(f"{e['timestamp'][11:16]} -{len(prev - s)}+{len(s - prev)}")
        prev = s
    print(f"- fills={sum(fills.values())} top5={fills.most_common(5)} (promotion bar 300)")
    print(f"- churn last 6 screens: {churn[-6:]}; distinct symbols ever={len(ever)}; now={len(prev or [])}")
    screens = {e["timestamp"][:19] for e in events(lines, "universe_set")}
    drops = [e["timestamp"][11:19] for e in events(lines, "stream_down_books_invalid")
             if e["timestamp"][:19] not in screens]
    print(f"- stream drops not at a rescreen: {len(drops)} {drops[-3:]}")
    risk = events(lines, "risk_event")
    print(f"- risk_events={len(risk)}")
    for e in risk[-3:]:
        print(f"  risk {e['timestamp'][11:19]} {e.get('rule')} {e.get('detail')} {e.get('before')}->{e.get('after')}")
    eq = prom('rb_equity_usdt{bot="shadow"}')
    if eq:
        print(f"- pnl: {eq[0][1] - SHADOW_EQUITY0:+.2f} USDT ({(eq[0][1] / SHADOW_EQUITY0 - 1) * 100:+.2f}%)")
    edge = sorted(((m["symbol"], v) for m, v in prom('rb_realized_edge_bps{bot="shadow"}')), key=lambda t: -t[1])
    fmt = lambda xs: [(s, round(v, 1), fills.get(s, 0)) for s, v in xs]  # noqa: E731
    if edge:
        w = sum(fills.get(s, 0) for s, _ in edge)
        wavg = sum(v * fills.get(s, 0) for s, v in edge) / w if w else float("nan")
        print(f"- realized edge bps (symbol, bps, fills): fill-weighted avg {wavg:+.1f}; "
              f"best {fmt(edge[:3])}; worst {fmt(edge[-3:])}")


def metrics() -> None:
    section("prometheus")
    for label, q in (("risk_state (0 OK,1 REDUCE_ONLY,2 HALTED)", "max by (bot,venue)(rb_risk_state)"),
                     ("rate_limit_max (<0.6)", "max by (bot)(rb_rate_limit_usage_ratio)"),
                     ("recon_mismatch_total", "sum by (bot)(rb_recon_mismatch_total)"),
                     ("ws_p99_ms", "histogram_quantile(0.99, sum by (bot,le)(rate(rb_ws_latency_ms_bucket[15m])))")):
        print(f"- {label}: " + str(sorted((m.get("bot"), m.get("venue", ""), round(v, 3)) for m, v in prom(q))))


def m1() -> None:
    section("M1")
    out = REPO / "reports" / "m1-validation"
    final = out / "summary.json"
    path = final if final.exists() else out / "summary.interim.json"
    if not path.exists():
        print("- no report yet (first interim after 10 min)")
        return
    s = json.loads(path.read_text())
    mtime = dt.datetime.fromtimestamp(path.stat().st_mtime, UTC)
    keys = ("hours", "checks", "match_pct", "undetected_mismatches", "detected_gaps", "reconnects", "rolls",
            "parse_errors", "snapshots", "snapshot_errors", "recorder_write_errors", "rows_written")
    print(f"- {'FINAL' if path == final else 'interim'} written {mtime:%H:%M}Z: "
          + ", ".join(f"{k}={round(s[k], 3) if isinstance(s[k], float) else s[k]}" for k in keys))
    rs = [r["resyncs"] for r in s["per_symbol"]]
    print(f"- resyncs per symbol {min(rs)}-{max(rs)}; symbols below 100%: "
          f"{[r['symbol'] for r in s['per_symbol'] if r['match_pct'] < 100]}")
    if path == final:
        print(f"- verdict: {s['verdict']}; parquet files={s['parquet_files']} unreadable={s['parquet_unreadable']}")


def host() -> None:
    section("host")
    free_gib = int(sh("df", "-k", str(REPO)).splitlines()[1].split()[3]) / 2**20  # same figure as `df -h`
    print(f"- disk free {free_gib:.0f} GiB; data/m1-validation "
          f"{sh('du', '-sh', str(REPO / 'data' / 'm1-validation')).split()[0]}")
    uid = sh("id", "-u").strip()
    agent = "running" if "state = running" in sh("launchctl", "print", f"gui/{uid}/com.riverbank.caffeinate") \
        else "NOT RUNNING"
    print(f"- caffeinate LaunchAgent: {agent}")


def main() -> None:
    logs = containers()
    if "riverbank-demo-1" in logs:
        demo(logs["riverbank-demo-1"])
    if "riverbank-shadow-1" in logs:
        shadow(logs["riverbank-shadow-1"])
    metrics()
    m1()
    host()


if __name__ == "__main__":
    main()
