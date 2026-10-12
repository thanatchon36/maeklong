"""Prometheus metrics (SPEC §5.11). Exposed on ops.metrics_port (default 9108)."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, start_http_server

RISK_STATE_CODE = {"OK": 0, "REDUCE_ONLY": 1, "HALTED": 2}


class Metrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        r = self.registry = registry or CollectorRegistry()
        self.inventory = Gauge("rb_inventory_usdt", "Signed position value at mark", ["venue", "symbol"], registry=r)
        self.open_orders = Gauge("rb_open_orders", "Open orders", ["venue", "symbol"], registry=r)
        self.quote_uptime = Gauge("rb_quote_uptime_ratio", "Two-sided quote uptime today", ["venue", "symbol"],
                                  registry=r)
        self.fills = Counter("rb_fills_total", "Fills", ["venue", "symbol", "side"], registry=r)
        self.realized_edge = Gauge("rb_realized_edge_bps", "Mean realized edge (60 s) of recent fills",
                                   ["venue", "symbol"], registry=r)
        self.markout = Gauge("rb_markout_bps", "Mean markout of recent fills", ["venue", "symbol", "horizon"],
                             registry=r)
        self.ws_latency = Histogram("rb_ws_latency_ms", "Exchange event time to local receive time",
                                    ["stream"], buckets=(5, 10, 25, 50, 100, 250, 500, 1000, 2500), registry=r)
        self.rate_limit_usage = Gauge("rb_rate_limit_usage_ratio", "Used / limit", ["limit"], registry=r)
        self.risk_state = Gauge("rb_risk_state", "0=OK 1=REDUCE_ONLY 2=HALTED", ["venue"], registry=r)
        self.equity = Gauge("rb_equity_usdt", "Equity", ["venue"], registry=r)
        self.book_status = Gauge("rb_book_ok", "1 if book OK", ["symbol"], registry=r)
        self.recon_mismatch = Counter("rb_recon_mismatch_total", "Reconciliation mismatches", ["venue", "symbol"],
                                      registry=r)
        self.errors = Counter("rb_errors_total", "Errors by kind", ["kind"], registry=r)

    def serve(self, port: int, addr: str = "0.0.0.0") -> None:
        start_http_server(port, addr=addr, registry=self.registry)
