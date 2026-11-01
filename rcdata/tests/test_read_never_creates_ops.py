"""A page read never creates ``ops.sqlite`` (#336).

``read.dataset_status`` used to ask ``ops.latest_check``, which opens through ``ops._open`` and so ensured the file
and every table on a process's first use: a page view on a fresh install left an empty ops file behind, and "never
built" could no longer be told from "built, no checks". The read side now goes through ``ops.read_latest_check``,
built on the same ``_select`` as the other Settings read helpers (F7 A). The builder keeps ``latest_check``.
"""
from __future__ import annotations

import sqlite3

from RevenueChaser.rcdata import needs, ops, read


def test_dataset_status_without_an_ops_file_is_never_checked_and_creates_nothing():
    assert not ops.ops_path().exists()
    assert read.dataset_status("spine/security") == read.NEVER_CHECKED
    assert not ops.ops_path().exists()


def test_a_table_summary_and_a_need_summary_create_no_ops_file():
    assert not ops.ops_path().exists()
    s = read.table_summary("security", day_column="source_as_of", key_column="ticker")
    assert s["status"] == read.NEVER_CHECKED
    n = needs.summary("identity.universe")
    assert n["state"] == "missing" and n["status"] == read.NEVER_CHECKED
    assert not ops.ops_path().exists()


def test_read_latest_check_matches_latest_check_once_the_file_exists():
    ops.record_check("market_series", "changed", note="first")
    ops.record_check("market_series", "unchanged", note="second")
    assert ops.read_latest_check("market_series") == ops.latest_check("market_series")
    assert read.dataset_status("market_series") == "unchanged"
    assert ops.read_latest_check("nothing") is None


def test_an_old_file_without_the_check_table_reads_as_never_checked():
    path = ops.ops_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE something_else (x INTEGER)")
    assert ops.read_latest_check("market_series") is None
    assert read.dataset_status("market_series") == read.NEVER_CHECKED
    with sqlite3.connect(path) as con:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"something_else"}, "a read added no table to the file"


def test_only_the_builder_calls_latest_check():
    """``latest_check`` ensures the file; a reader uses ``read_latest_check``. Tests are left out."""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    callers = sorted(str(p.relative_to(root)).replace("\\", "/") for p in root.rglob("*.py")
                     if "tests" not in p.relative_to(root).parts and ".git" not in p.parts
                     and re.search(r"\bops\.latest_check\(", p.read_text(encoding="utf-8", errors="replace")))
    assert callers == ["rcdata/build/base.py", "rcdata/build/due.py"], callers
