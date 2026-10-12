"""Shadow-vs-live gap report (SPEC §5.9): fill rate and markout of the real venue vs its shadow twin."""

import sqlite3
from decimal import Decimal

import pytest

from riverbank.analytics.gap import gap_lines, gap_rows
from riverbank.core.models import Fill, Side
from riverbank.storage.sqlite import SqliteRepository
from tests.helpers import T0

DATE = "2026-10-12"
HOUR = 3_600_000


def _fill(repo, venue, mode, sym, i, mo60, fee="0"):
    f = Fill(T0 + i, sym, Side.BUY, Decimal("1"), Decimal("10"), Decimal(fee), True, mode, f"rb-{venue}-{i}", 1.0,
             str(i))
    repo.insert_fill(f, venue)
    repo.update_markout(venue, f.client_id, f.trade_id, 60, mo60)


@pytest.fixture
def conn(tmp_path):
    repo = SqliteRepository(tmp_path / "live.sqlite")
    for i, mo in enumerate([2.0, 4.0]):            # live: 2 fills in 1 quoted hour, edge mean 3 - 2 = 1
        _fill(repo, "live", "live", "AAAUSDT", i, mo, fee="0.002")
    for i, mo in enumerate([5.0, 6.0, 7.0, 6.0]):  # shadow: 4 fills in 2 quoted hours, edge mean 6
        _fill(repo, "shadow", "shadow", "AAAUSDT", 10 + i, mo)
    _fill(repo, "shadow", "shadow", "ZZZUSDT", 20, 1.0)  # not traded live: no gap row
    repo.add_uptime(DATE, "live", "AAAUSDT", HOUR, 2 * HOUR)
    repo.add_uptime(DATE, "shadow", "AAAUSDT", 2 * HOUR, 2 * HOUR)
    repo.flush()
    c = sqlite3.connect(repo.path)
    c.row_factory = sqlite3.Row
    yield c
    c.close()
    repo.close()


def test_gap_rows_compare_fill_rate_and_edge_per_symbol(conn):
    rows = gap_rows(conn, "live", "shadow", T0, T0 + 24 * HOUR, 60)
    assert [r["symbol"] for r in rows] == ["AAAUSDT"]
    r = rows[0]
    assert (r["real_fills"], r["shadow_fills"]) == (2, 4)
    assert r["real_fph"] == pytest.approx(2.0) and r["shadow_fph"] == pytest.approx(2.0)
    assert r["real_edge"].mean == pytest.approx(1.0) and r["shadow_edge"].mean == pytest.approx(6.0)
    assert r["edge_gap"] == pytest.approx(-5.0)
    assert r["edge_gap_se"] == pytest.approx((r["real_edge"].se ** 2 + r["shadow_edge"].se ** 2) ** 0.5)


def test_gap_lines_render_markdown_table(conn):
    text = "\n".join(gap_lines(conn, "live", "shadow", T0, T0 + 24 * HOUR, 60))
    assert "Shadow-vs-live gap" in text
    assert "| AAAUSDT | 2 | 4 |" in text
    assert "ZZZUSDT" not in text


def test_gap_lines_empty_without_a_twin(conn):
    assert gap_lines(conn, "live", "nope", T0, T0 + 24 * HOUR, 60) == []


def test_daily_report_includes_the_gap_section(conn, tmp_path):
    from riverbank.analytics.report import build_daily_report
    from riverbank.core.config import Config

    db = conn.execute("PRAGMA database_list").fetchone()[2]
    text = build_daily_report(db, DATE, tmp_path / "reports", Config()).read_text()
    assert "Shadow-vs-live gap (`live` vs twin `shadow`)" in text
