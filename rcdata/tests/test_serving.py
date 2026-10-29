"""``rcdata/serving.py``: build a new file, check it, swap it in; read-only opens. T3 is the acceptance test of #206."""
from __future__ import annotations

import hashlib
from pathlib import Path

import duckdb
import pytest

from RevenueChaser.core import config
from RevenueChaser.rcdata import serving


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build(rows: int, *, fail_after_write: bool = False) -> None:
    with serving.build("market") as b:
        b.con.execute("CREATE TABLE t (i INTEGER)")
        b.con.execute(f"INSERT INTO t SELECT range FROM range({rows})")
        if fail_after_write:
            b.con.execute("CHECKPOINT")
            assert b.path.exists()
            raise RuntimeError("the build broke after writing its file")
        b.swap({"t": rows})


def _leftovers() -> list[str]:
    return sorted(p.name for p in Path(config.MARKET_DIR).iterdir()
                  if p.name not in ("market.duckdb", "market.prev.duckdb"))


def test_t3_a_build_that_raises_after_writing_leaves_the_live_file_byte_identical():
    _build(3)
    live = serving.live_path("market")
    before = _sha(live)
    with pytest.raises(RuntimeError, match="broke"):
        _build(5, fail_after_write=True)
    assert _sha(live) == before
    assert _leftovers() == [], "the abandoned build file (or its .wal/.tmp) must not remain"
    with serving.open_read("market") as con:
        assert con.execute("SELECT count(*) FROM t").fetchone()[0] == 3


@pytest.mark.parametrize("bad", ["count", "version", "kind", "failed_check", "missing_table"])
def test_a_build_that_fails_its_check_is_deleted_and_the_live_file_is_untouched(bad):
    _build(3)
    live = serving.live_path("market")
    before = _sha(live)
    with pytest.raises(serving.SwapRefused):
        with serving.build("market") as b:
            b.con.execute("CREATE TABLE t (i INTEGER)")
            b.con.execute("INSERT INTO t VALUES (1), (2)")
            expected, checks = {"t": 2}, []
            if bad == "count":
                expected = {"t": 3}
            elif bad == "version":
                b.con.execute("UPDATE serving_schema_version SET version = version + 1")
            elif bad == "kind":
                b.con.execute("UPDATE serving_schema_version SET kind = 'other'")
            elif bad == "failed_check":
                checks = [{"dataset": "market_series", "result": "failed"}]
            else:
                expected = {"t": 2, "absent": 0}
            b.swap(expected, checks=checks)
    assert _sha(live) == before
    assert _leftovers() == []


def test_a_swap_keeps_the_previous_file_as_prev():
    _build(3)
    first = _sha(serving.live_path("market"))
    _build(4)
    assert _sha(serving.prev_path("market")) == first
    with serving.open_read("market") as con:
        assert con.execute("SELECT count(*) FROM t").fetchone()[0] == 4
        assert con.execute("SELECT kind, version FROM serving_schema_version").fetchall() == [
            ("market", serving.SERVING_SCHEMA_VERSION["market"])]
    assert _leftovers() == []


def test_a_build_left_without_a_swap_is_removed():
    _build(3)
    with serving.build("market") as b:
        b.con.execute("CREATE TABLE t (i INTEGER)")
    assert _leftovers() == []


def test_open_read_refuses_to_write():
    _build(2)
    with serving.open_read("market") as con:
        with pytest.raises(duckdb.Error):
            con.execute("INSERT INTO t VALUES (9)")


def test_open_read_resolves_the_path_on_every_call(tmp_path, monkeypatch):
    _build(2)
    with serving.open_read("market") as con:
        assert con.execute("SELECT count(*) FROM t").fetchone()[0] == 2
    monkeypatch.setattr(config, "MARKET_DIR", tmp_path / "elsewhere")
    with pytest.raises(serving.ServingFileMissing):
        serving.open_read("market")


def test_build_paths_and_unknown_kinds():
    assert serving.build_path("market", "b1") == Path(config.MARKET_DIR) / "market.b1.duckdb"
    with pytest.raises(ValueError):
        serving.live_path("nope")


def _build_t(rows: int, **swap_kw) -> None:
    with serving.build("market") as b:
        b.con.execute(f"CREATE TABLE t AS SELECT range AS i FROM range({rows})")
        b.swap({"t": rows}, **swap_kw)


@pytest.mark.parametrize("rows", [0, 9])
def test_b2_a_table_that_empties_or_shrinks_against_the_live_file_is_refused(rows):
    """B2: the expected counts come from the build itself, so they cannot catch a build from a missing Parquet
    tree; the live file is the reference."""
    _build_t(10)
    before = _sha(serving.live_path("market"))
    with pytest.raises(serving.SwapRefused, match="shrinks|empties"):
        _build_t(rows)
    assert _sha(serving.live_path("market")) == before and _leftovers() == []


def test_b2_a_table_the_live_file_has_must_be_in_the_build():
    _build_t(10)
    with pytest.raises(serving.SwapRefused, match="missing"):
        with serving.build("market") as b:
            b.con.execute("CREATE TABLE other (i INTEGER)")
            b.swap({"other": 0})


def test_b2_a_shrink_is_allowed_only_when_asked_for():
    _build_t(10)
    _build_t(0, allow_shrink=("t",))
    with serving.open_read("market") as con:
        assert con.execute("SELECT count(*) FROM t").fetchone()[0] == 0
    _build_t(11)                                   # growing is always fine
