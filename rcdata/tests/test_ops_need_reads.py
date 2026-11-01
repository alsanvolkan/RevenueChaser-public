"""``ops.need_reads``: the read log's ``need:<name>`` rows, per need, process and day, for the Settings Needs tab (#331)."""
from __future__ import annotations

from RevenueChaser.rcdata import ops


def _log(rows):
    ops.ensure()
    with ops.connect() as con:
        con.executemany("INSERT INTO read_log (day, process, table_name, reads) VALUES (?, ?, ?, ?)", rows)


def test_need_reads_returns_one_row_per_need_process_and_day_without_the_prefix():
    _log([("2026-10-30", "dashboard", "need:prices.bars", 4),
          ("2026-10-31", "dashboard", "need:prices.bars", 2),
          ("2026-10-31", "builder", "need:prices.bars", 1),
          ("2026-10-31", "dashboard", "need:macro.dgs10", 7)])
    rows = ops.need_reads("2026-10-30")
    assert sorted((r["need"], r["process"], r["day"], r["reads"]) for r in rows) == [
        ("macro.dgs10", "dashboard", "2026-10-31", 7),
        ("prices.bars", "builder", "2026-10-31", 1),
        ("prices.bars", "dashboard", "2026-10-30", 4),
        ("prices.bars", "dashboard", "2026-10-31", 2)]
    assert all(set(r) == {"need", "process", "day", "reads"} for r in rows)


def test_need_reads_leaves_out_table_rows_and_days_before_the_first():
    _log([("2026-10-31", "dashboard", "price_bar_daily", 99),
          ("2026-10-31", "dashboard", "needish_table", 5),
          ("2026-10-20", "dashboard", "need:prices.bars", 3),
          ("2026-10-31", "cli", "need:prices.bars", 1)])
    rows = ops.need_reads("2026-10-25")
    assert [(r["need"], r["process"], r["reads"]) for r in rows] == [("prices.bars", "cli", 1)]


def test_need_reads_never_creates_the_ops_file():
    assert not ops.ops_path().exists()
    assert ops.need_reads("2026-10-01") == []
    assert not ops.ops_path().exists()


# ── The process a read is counted under (#331, decision 2) ──────────────────────────────────────────────────────

import sys  # noqa: E402

import pytest  # noqa: E402


@pytest.mark.parametrize("argv0, name", [
    ("/x/RevenueChaser/rcdata/build/__main__.py", "builder"),        # python -m RevenueChaser.rcdata.build
    ("/x/RevenueChaser/rcdata/__main__.py", "rcdata"),               # python -m RevenueChaser.rcdata (backup)
    ("C:\\x\\RevenueChaser\\module_spx_gamma\\__main__.py", "module_spx_gamma"),
    ("/x/RevenueChaser/app/dashboard.py", "dashboard"),              # python -m RevenueChaser.app.dashboard
    ("", "python"),
])
def test_a_python_m_run_is_named_after_its_package_not___main__(monkeypatch, argv0, name):
    monkeypatch.delenv("RC_PROCESS_NAME", raising=False)
    monkeypatch.setattr(sys, "argv", [argv0])
    assert ops._process_name() == name


def test_rc_process_name_still_wins(monkeypatch):
    monkeypatch.setenv("RC_PROCESS_NAME", "nightly")
    monkeypatch.setattr(sys, "argv", ["/x/RevenueChaser/rcdata/build/__main__.py"])
    assert ops._process_name() == "nightly"
