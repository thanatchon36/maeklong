import pytest

from riverbank.exchange import rest as rest_mod
from riverbank.ops.logging import setup_logging


@pytest.fixture(autouse=True, scope="session")
def _quiet_logs():
    setup_logging("WARNING", {"engine": "ERROR", "risk": "ERROR"})


@pytest.fixture(autouse=True)
def _never_live_signing():
    """No test may sign requests for production hosts (SPEC §10)."""
    rest_mod.lock_live_signing()
    yield
    rest_mod.lock_live_signing()
