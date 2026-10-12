import importlib.util
import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

from riverbank.core.models import Order, OrderStatus, Side
from riverbank.storage.sqlite import SqliteRepository

ROOT = Path(__file__).resolve().parents[2]
T0 = 1791763200000


def test_demo_acceptance_report(tmp_path):
    repo = SqliteRepository(tmp_path / "db.sqlite")
    o = Order("rb-X-BUY-1", 1, "DOGEUSDT", Side.BUY, Decimal("1"), Decimal("1"), Decimal(0), OrderStatus.CANCELED,
              T0, T0 + 73 * 3600_000)
    repo.upsert_order(o, "demo", "demo")
    repo.insert_recon(T0 + 1000, "demo", "DOGEUSDT", "RECON_MISMATCH", "x")
    repo.resolve_recon(T0 + 2000, "demo", "DOGEUSDT")
    repo.kv_set("drill:kill", json.dumps({"halted": True, "latency_s": 0.2}))
    repo.flush()
    repo.close()
    out = tmp_path / "out"
    p = subprocess.run([sys.executable, "tools/demo_acceptance.py", "--db", str(tmp_path / "db.sqlite"),
                        "--skip-fault-tests", "--out", str(out)], cwd=ROOT, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    v = json.loads((out / "summary.json").read_text())["verdict"]
    assert v == {"duration_ge_72h": True, "zero_unresolved_recon_mismatch": True,
                 "fault_injection_tests_pass": None, "kill_switch_lt_1s": True}


def test_shadow_acceptance_seven_day_window_can_pass():
    spec = importlib.util.spec_from_file_location("shadow_acceptance", ROOT / "tools" / "shadow_acceptance.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    day = 86_400_000
    # --start D0 --end D6: first fill 30 s after D0 opens, last fill 30 s before D6 closes
    assert mod.covers_days(T0 + 30_000, T0 + 7 * day - 30_000, 7)
    # the run stopped half a day early
    assert not mod.covers_days(T0 + 30_000, T0 + 6 * day + day // 2, 7)
