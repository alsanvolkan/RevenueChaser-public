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


def _settings(con) -> tuple[str, int]:
    return (con.execute("SELECT current_setting('memory_limit')").fetchone()[0],
            con.execute("SELECT current_setting('threads')").fetchone()[0])


def test_a_build_connection_is_capped_by_default(monkeypatch):
    """#319's OOM: the build connection must not take DuckDB's default 80 % of RAM, nor every core."""
    monkeypatch.delenv("RC_DUCKDB_MEMORY_LIMIT", raising=False)
    monkeypatch.delenv("RC_DUCKDB_THREADS", raising=False)
    with serving.build("market") as b:
        memory, threads = _settings(b.con)
    assert memory in ("4.6 GiB", "5.0 GB", "4.6GiB"), memory
    assert threads == serving.THREADS_DEFAULT


def test_the_cap_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("RC_DUCKDB_MEMORY_LIMIT", "2GB")
    monkeypatch.setenv("RC_DUCKDB_THREADS", "2")
    monkeypatch.setenv("RC_DUCKDB_MAX_TEMP", "50GB")
    with serving.build("market") as b:
        memory, threads = _settings(b.con)
        max_temp = b.con.execute("SELECT current_setting('max_temp_directory_size')").fetchone()[0]
    assert memory.replace(" ", "") in ("1.8GiB", "2.0GB"), memory
    assert threads == 2
    assert max_temp.replace(" ", "") in ("46.5GiB", "50.0GB"), max_temp


@pytest.mark.parametrize("memory, threads, max_temp",
                         [("", "", ""), ("lots", "x", "much"), ("5", "0", "200"), ("GB", "-3", "TB")])
def test_an_empty_or_invalid_cap_keeps_the_default(monkeypatch, memory, threads, max_temp):
    monkeypatch.setenv("RC_DUCKDB_MEMORY_LIMIT", memory)
    monkeypatch.setenv("RC_DUCKDB_THREADS", threads)
    monkeypatch.setenv("RC_DUCKDB_MAX_TEMP", max_temp)
    assert serving.build_config() == {"memory_limit": serving.MEMORY_LIMIT_DEFAULT,
                                      "threads": serving.THREADS_DEFAULT,
                                      "max_temp_directory_size": serving.MAX_TEMP_DEFAULT}


def test_a_build_over_its_memory_limit_spills_on_the_raw_disk_and_leaves_no_scratch(monkeypatch):
    """Under a small limit a sort larger than it still finishes, and the spill directory goes with the build. #372:
    the spill lives under the raw root (the server's HDD), not beside the live files: request 71 spilled 47 GB
    there, on the SSD the live stores share."""
    monkeypatch.setenv("RC_DUCKDB_MEMORY_LIMIT", "64MB")
    monkeypatch.setenv("RC_DUCKDB_THREADS", "1")
    rows = 3_000_000
    with serving.build("market") as b:
        spill = Path(b.con.execute("SELECT current_setting('temp_directory')").fetchone()[0])
        b.con.execute(f"CREATE TABLE t AS SELECT range AS i, md5(range::VARCHAR) AS h FROM range({rows}) ORDER BY h")
        b.swap({"t": rows})
    assert Path(config.MARKET_RAW_DIR) in spill.parents, spill
    assert not spill.exists() and _leftovers() == []


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
