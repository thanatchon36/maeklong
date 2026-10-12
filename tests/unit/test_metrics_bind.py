"""Metrics bind address (RUNBOOK §10.4): live binds to the Docker bridge, never 0.0.0.0 on the host."""

from riverbank.core.config import Config, load_config
from riverbank.ops import metrics as metrics_mod
from riverbank.ops.metrics import Metrics


def test_serve_binds_the_configured_address(monkeypatch):
    calls = []
    monkeypatch.setattr(metrics_mod, "start_http_server", lambda port, addr, registry: calls.append((port, addr)))
    Metrics().serve(9110, "172.17.0.1")
    assert calls == [(9110, "172.17.0.1")]


def test_containers_listen_on_all_interfaces_but_live_only_on_the_bridge():
    assert Config().ops.metrics_addr == "0.0.0.0"  # inside a container: reachable on the compose network
    assert load_config("config/config.yaml").ops.metrics_addr == "0.0.0.0"
    assert load_config("config/demo.yaml").ops.metrics_addr == "0.0.0.0"
    assert load_config("config/live.yaml").ops.metrics_addr == "172.17.0.1"  # host: Docker bridge only
