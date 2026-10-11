"""Daily report (SPEC §5.9): Markdown + charts in reports/YYYY-MM-DD/.

Statistical honesty: every markout/edge figure is printed with n, mean and standard error;
promotion readiness is never claimed with fewer than `min_fills_for_promotion` fills.

    uv run python -m riverbank.analytics.report --db state/riverbank.sqlite --date 2026-10-12
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sqlite3
from pathlib import Path
from typing import Any

from riverbank.analytics.markout import HORIZONS_S, Stats, stats
from riverbank.analytics.pnl import decompose, total
from riverbank.core.config import Config, load_config

DAY_MS = 86_400_000


def _connect(db: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    return conn


def day_bounds(date: str) -> tuple[int, int]:
    d = dt.datetime.fromisoformat(date).replace(tzinfo=dt.UTC)
    t0 = int(d.timestamp() * 1000)
    return t0, t0 + DAY_MS


def fills(conn: sqlite3.Connection, venue: str, t0: int, t1: int, symbol: str | None = None) -> list[dict[str, Any]]:
    q = "SELECT * FROM fills WHERE venue=? AND ts>=? AND ts<?"
    args: list[Any] = [venue, t0, t1]
    if symbol:
        q += " AND symbol=?"
        args.append(symbol)
    return [dict(r) for r in conn.execute(q + " ORDER BY ts", args)]


def snapshot_at(conn: sqlite3.Connection, venue: str, t: int) -> dict[str, dict[str, float]]:
    rows = conn.execute(
        "SELECT p.* FROM pnl_snapshots p JOIN (SELECT symbol, MAX(ts) AS mts FROM pnl_snapshots "
        "WHERE venue=? AND ts<=? GROUP BY symbol) m ON p.symbol=m.symbol AND p.ts=m.mts WHERE p.venue=?",
        (venue, t, venue)).fetchall()
    return {r["symbol"]: dict(r) for r in rows if r["symbol"] != "__TOTAL__"}


def equity_curve(conn: sqlite3.Connection, venue: str, t0: int, t1: int) -> list[tuple[int, float]]:
    return [(r[0], r[1]) for r in conn.execute(
        "SELECT ts, equity FROM pnl_snapshots WHERE venue=? AND symbol='__TOTAL__' AND ts>=? AND ts<? ORDER BY ts",
        (venue, t0, t1))]


def venues(conn: sqlite3.Connection, t0: int, t1: int) -> list[str]:
    v = {r[0] for r in conn.execute("SELECT DISTINCT venue FROM fills WHERE ts>=? AND ts<?", (t0, t1))}
    v |= {r[0] for r in conn.execute("SELECT DISTINCT venue FROM pnl_snapshots WHERE ts>=? AND ts<?", (t0, t1))}
    return sorted(v)


def fee_bps(f: dict[str, Any]) -> float:
    notional = float(f["price"]) * float(f["qty"])
    return float(f["fee"]) / notional * 1e4 if notional > 0 else 0.0


def edge_values(rows: list[dict[str, Any]], h: int) -> list[float]:
    return [r[f"mo_{h}"] - fee_bps(r) for r in rows if r.get(f"mo_{h}") is not None]


def fmt_stats(s: Stats) -> str:
    if s.n == 0:
        return "n=0"
    se = "–" if math.isnan(s.se) else f"{s.se:.2f}"
    return f"{s.mean:+.2f} ± {se} (n={s.n})"


def promotion_status(n: int, st: Stats, min_fills: int) -> tuple[bool, str]:
    if n < min_fills:
        return False, f"need ≥{min_fills} fills (have {n})"
    if st.n < min_fills:
        return False, f"only {st.n} fills have a resolved markout"
    if not (st.mean - st.se > 0):
        return False, f"edge lower bound {st.mean - st.se:+.2f} bps ≤ 0"
    return True, f"edge lower bound {st.mean - st.se:+.2f} bps > 0"


def build_daily_report(db: str | Path, date: str, out_root: str | Path, cfg: Config) -> Path:
    t0, t1 = day_bounds(date)
    out = Path(out_root) / date
    (out / "charts").mkdir(parents=True, exist_ok=True)
    conn = _connect(db)
    hedge = set(cfg.universe.hedge_only)
    horizon = cfg.universe.promotion_horizon_s
    min_fills = cfg.universe.min_fills_for_promotion
    lines = [f"# Daily report — {date} (UTC)", "",
             "All markouts/edges: mean ± standard error (n). Edge = markout(h) − fee (bps). "
             f"Promotion uses h = {horizon} s and requires n ≥ {min_fills} and mean − 1·SE > 0.", ""]
    summary: dict[str, Any] = {"date": date, "venues": {}}
    for venue in venues(conn, t0, t1):
        vf = fills(conn, venue, t0, t1)
        start, end = snapshot_at(conn, venue, t0 - 1), snapshot_at(conn, venue, t1 - 1)
        pnl = decompose(vf, start, end, hedge)
        tot = total(pnl)
        eq = equity_curve(conn, venue, t0, t1)
        uptime = {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT symbol, quoted_ms, total_ms FROM quote_uptime WHERE date=? AND venue=?", (date, venue))}
        risk_ev = conn.execute("SELECT ts, level, rule, detail, state_before, state_after FROM risk_events "
                               "WHERE venue=? AND ts>=? AND ts<? ORDER BY ts", (venue, t0, t1)).fetchall()
        recon = conn.execute("SELECT ts, symbol, kind, detail, resolved FROM recon_events WHERE venue=? AND ts>=? "
                             "AND ts<? ORDER BY ts", (venue, t0, t1)).fetchall()
        lines += [f"## Venue `{venue}`", "",
                  f"- Fills: {len(vf)}; net PnL: **{tot.net:+.4f} USDT** (spread {tot.spread:+.4f}, inventory "
                  f"{tot.inventory:+.4f}, hedge {tot.hedge:+.4f}, fees −{tot.fees:.4f}, funding {tot.funding:+.4f})",
                  f"- Equity: {eq[0][1]:.4f} → {eq[-1][1]:.4f} USDT" if eq else "- Equity: n/a",
                  f"- Risk events: {len(risk_ev)}; reconciliation mismatches: {len(recon)} "
                  f"(unresolved {sum(1 for r in recon if not r['resolved'])})", ""]
        lines += ["### Markout and edge per symbol", "",
                  "| symbol | fills | buy/sell | volume $ | uptime % | fills/quoted h | spread cap. bps | "
                  + " | ".join(f"mo {h}s" for h in HORIZONS_S) + f" | edge {horizon}s |",
                  "|---|---:|---:|---:|---:|---:|---:|" + "---:|" * len(HORIZONS_S) + "---:|"]
        sym_summary: dict[str, Any] = {}
        curves: dict[str, list[float]] = {}
        for sym in sorted({f["symbol"] for f in vf} | set(uptime)):
            sf = [f for f in vf if f["symbol"] == sym]
            q_ms, t_ms = uptime.get(sym, (0, 0))
            up = 100.0 * q_ms / t_ms if t_ms else float("nan")
            fph = len(sf) / (q_ms / 3_600_000) if q_ms else float("nan")
            sc = stats(((f["mid_at_fill"] - float(f["price"])) * (1 if f["side"] == "BUY" else -1)
                        / float(f["price"]) * 1e4) for f in sf if f["mid_at_fill"])
            mos = {h: stats(f[f"mo_{h}"] for f in sf if f[f"mo_{h}"] is not None) for h in HORIZONS_S}
            edge = stats(edge_values(sf, horizon))
            buys = sum(1 for f in sf if f["side"] == "BUY")
            vol = sum(float(f["price"]) * float(f["qty"]) for f in sf)
            lines.append(f"| {sym} | {len(sf)} | {buys}/{len(sf) - buys} | {vol:.0f} | {_num(up, '.1f')} | "
                         f"{_num(fph, '.1f')} | "
                         f"{_num(sc.mean if sc.n else float('nan'))} | "
                         + " | ".join(fmt_stats(mos[h]) for h in HORIZONS_S) + f" | {fmt_stats(edge)} |")
            curves[sym] = [mos[h].mean if mos[h].n else float("nan") for h in HORIZONS_S]
            sym_summary[sym] = {"fills": len(sf), "uptime_pct": up, "fills_per_quoted_hour": fph,
                                "edge": {"n": edge.n, "mean": edge.mean, "se": edge.se},
                                "markout": {h: {"n": mos[h].n, "mean": mos[h].mean, "se": mos[h].se}
                                            for h in HORIZONS_S}}
        lines += ["", "### PnL decomposition (USDT)", "",
                  "| symbol | spread | inventory | hedge | fees | funding | net | fills |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|"]
        for r in [*sorted(pnl.values(), key=lambda r: r.symbol), tot]:
            lines.append(f"| {r.symbol} | {r.spread:+.4f} | {r.inventory:+.4f} | {r.hedge:+.4f} | {-r.fees:+.4f} | "
                         f"{r.funding:+.4f} | **{r.net:+.4f}** | {r.n_fills} |")
        # cumulative evidence (all history of this venue) for promotion
        lines += ["", f"### Promotion readiness (cumulative evidence, venue `{venue}`)", "",
                  f"| symbol | fills (all) | edge {horizon}s | status |", "|---|---:|---:|---|"]
        readiness: dict[str, Any] = {}
        for sym in sorted({r[0] for r in conn.execute("SELECT DISTINCT symbol FROM fills WHERE venue=? AND ts<?",
                                                       (venue, t1))} - hedge):
            allf = fills(conn, venue, 0, t1, sym)
            st = stats(edge_values(allf, horizon))
            ok, why = promotion_status(len(allf), st, min_fills)
            readiness[sym] = {"ready": ok, "why": why, "n": len(allf), "edge_mean": st.mean, "edge_se": st.se}
            lines.append(f"| {sym} | {len(allf)} | {fmt_stats(st)} | {'**READY**' if ok else 'not ready'}: {why} |")
        if risk_ev:
            lines += ["", "### Risk events", ""]
            lines += [f"- {_ts(r['ts'])} {r['level']} {r['rule']}: {r['state_before']}→{r['state_after']} "
                      f"{r['detail']}" for r in risk_ev]
        if recon:
            lines += ["", "### Reconciliation", ""]
            lines += [f"- {_ts(r['ts'])} {r['symbol']} {r['kind']} resolved={bool(r['resolved'])}: {r['detail']}"
                      for r in recon]
        charts = _charts(out / "charts", venue, curves, eq)
        lines += [""] + [f"![{c.stem}](charts/{c.name})" for c in charts] + [""]
        summary["venues"][venue] = {"pnl": {k: v.as_dict() for k, v in pnl.items()}, "total": tot.as_dict(),
                                    "symbols": sym_summary, "readiness": readiness,
                                    "risk_events": len(risk_ev), "recon_mismatches": len(recon),
                                    "recon_unresolved": sum(1 for r in recon if not r["resolved"])}
    decisions = conn.execute("SELECT ts, symbol, from_state, to_state, reason, n, mean, se FROM universe_decisions "
                             "WHERE ts>=? AND ts<? ORDER BY ts", (t0, t1)).fetchall()
    if decisions:
        lines += ["## Universe decisions", ""]
        lines += [f"- {_ts(r['ts'])} {r['symbol']}: {r['from_state']} → {r['to_state']} ({r['reason']}; n={r['n']}, "
                  f"mean={r['mean']:+.2f}, se={r['se']:.2f})" for r in decisions]
    conn.close()
    (out / "report.md").write_text("\n".join(lines) + "\n")
    (out / "summary.json").write_text(json.dumps(summary, indent=1, default=_json_default))
    return out / "report.md"


def _num(x: float, fmt: str = "+.2f") -> str:
    return "–" if x is None or math.isnan(x) else format(x, fmt)


def _json_default(o: Any) -> Any:
    if isinstance(o, float) and math.isnan(o):
        return None
    return str(o)


def _ts(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.UTC).strftime("%H:%M:%S")


def _charts(out: Path, venue: str, curves: dict[str, list[float]], eq: list[tuple[int, float]]) -> list[Path]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    paths: list[Path] = []
    if curves:
        fig, ax = plt.subplots(figsize=(8, 4.5))
        for sym, ys in sorted(curves.items()):
            ax.plot(list(HORIZONS_S), ys, marker="o", label=sym)
        ax.set_xscale("log")
        ax.axhline(0, color="grey", lw=0.8)
        ax.set_xlabel("horizon (s)")
        ax.set_ylabel("mean markout (bps)")
        ax.set_title(f"Markout by horizon — {venue}")
        if len(curves) <= 12:
            ax.legend(fontsize=7)
        p = out / f"markout_{venue}.png"
        fig.tight_layout()
        fig.savefig(p, dpi=110)
        plt.close(fig)
        paths.append(p)
    if len(eq) > 1:
        fig, ax = plt.subplots(figsize=(8, 3.5))
        ax.plot([dt.datetime.fromtimestamp(t / 1000, dt.UTC) for t, _ in eq], [e for _, e in eq])
        ax.set_title(f"Equity — {venue}")
        ax.set_ylabel("USDT")
        fig.autofmt_xdate()
        p = out / f"equity_{venue}.png"
        fig.tight_layout()
        fig.savefig(p, dpi=110)
        plt.close(fig)
        paths.append(p)
    return paths


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="state/riverbank.sqlite")
    ap.add_argument("--date", default=(dt.datetime.now(dt.UTC) - dt.timedelta(days=1)).strftime("%Y-%m-%d"))
    ap.add_argument("--out", default="reports")
    ap.add_argument("--config", default="config/config.yaml")
    a = ap.parse_args()
    path = build_daily_report(a.db, a.date, a.out, load_config(a.config))
    print(path)


if __name__ == "__main__":
    main()
