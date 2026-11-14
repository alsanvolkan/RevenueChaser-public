"""``rcdata/serving.py``: build a new file, check it, swap it in; read-only opens. T3 is the acceptance test of #206."""
from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

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
    _free_space(monkeypatch, 10**15)          # a CI runner has less than 50 GB free; the cap is the variable's here
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


def _free_space(monkeypatch, free: int) -> None:
    monkeypatch.setattr(serving.shutil, "disk_usage", lambda p: SimpleNamespace(free=free))


@pytest.mark.parametrize("max_temp, free, expected", [("200GB", 10**15, "200GB"),
                                                      ("200GB", 50 * 10**9, "45000000KB"),
                                                      ("200GiB", 100 * 10**9, "90000000KB"),
                                                      ("1KB", 0, "0KB")])
def test_379_the_spill_cap_never_exceeds_90_percent_of_the_spill_disks_free_space(monkeypatch, tmp_path, max_temp,
                                                                                  free, expected):
    """#379: a 200 GB cap on an SSD with 50 GB free would let a spill fill the disk the live stores share."""
    monkeypatch.setenv("RC_DUCKDB_MAX_TEMP", max_temp)
    _free_space(monkeypatch, free)
    assert serving.build_config(tmp_path)["max_temp_directory_size"] == expected
    assert serving.build_config()["max_temp_directory_size"] == max_temp


def test_379_a_build_opens_with_the_cap_held_to_the_free_space(monkeypatch):
    _free_space(monkeypatch, 50 * 10**9)
    monkeypatch.delenv("RC_DUCKDB_MAX_TEMP", raising=False)
    with serving.build("market") as b:
        max_temp = b.con.execute("SELECT current_setting('max_temp_directory_size')").fetchone()[0]
    assert max_temp.replace(" ", "") in ("41.9GiB", "45.0GB"), max_temp


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


def test_rc_duckdb_temp_dir_moves_the_spill_and_unset_keeps_the_raw_root(monkeypatch, tmp_path):
    """#654 review: the spill folder can move (to the SSD, once a logged peak says it fits); the default is unchanged."""
    monkeypatch.setenv("RC_DUCKDB_TEMP_DIR", str(tmp_path / "ssd_tmp"))
    with serving.build("market") as b:
        spill = Path(b.con.execute("SELECT current_setting('temp_directory')").fetchone()[0])
    assert spill.parent == tmp_path / "ssd_tmp" and spill.name == f"{b.path.name}.tmp"
    monkeypatch.setenv("RC_DUCKDB_TEMP_DIR", " ")
    assert serving.spill_dir(Path("m.duckdb")) == Path(config.MARKET_RAW_DIR) / "build_tmp" / "m.duckdb.tmp"


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


def test_a_table_that_shrinks_by_choice_may_shrink_without_allow_shrink_but_may_not_empty():
    """``SHRINKS_BY_CHOICE``: the source or avo chooses the row count, so a smaller one is not a partial build.
    A request-wide ``allow_shrink`` is not needed for it (it would relax every other table of the kind with it),
    and the "empties" guard still holds."""
    for table in serving.SHRINKS_BY_CHOICE:
        with serving.build("market") as b:
            b.con.execute(f'CREATE TABLE "{table}" AS SELECT range AS i FROM range(100)')
            b.swap({table: 100})
        with serving.build("market") as b:                      # -90 %, far past MAX_SHRINK, no allow_shrink
            b.con.execute(f'CREATE TABLE "{table}" AS SELECT range AS i FROM range(10)')
            b.swap({table: 10})
        with serving.open_read("market") as con:
            assert con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] == 10
        with pytest.raises(serving.SwapRefused, match="empties"):
            with serving.build("market") as b:
                b.con.execute(f'CREATE TABLE "{table}" (i INTEGER)')
                b.swap({table: 0})
        serving.live_path("market").unlink()


def test_the_economic_calendar_shrinks_by_choice_because_its_window_rolls():
    """RC-Build on homesrv failed every hour from 00:05 UTC on 14 Nov 2026: ``uw/economic_calendar`` served 114 rows
    against 126 live (-9.5 %), because a snapshot day's partition is replaced whole by that day's newest fetch and
    events have rolled out of UW's "this week and next" window since. One refused swap fails the whole ``market``
    kind (one file, one swap), so the guard cannot be left to a retry."""
    from RevenueChaser.rcdata.build import uw_economic_calendar as econ
    assert econ.TABLE in serving.SHRINKS_BY_CHOICE


def test_a_retired_table_may_leave_the_build_and_any_other_still_may_not(monkeypatch):
    """Review M2 of #382: PR 382 drops ``prices``' ``crsp_unadjusted`` table on purpose, and the live file still holds
    it. The "every live table must be in the build" guard refused that build even with ``allow_shrink``; a table the
    code retired by name (``RETIRED_TABLES``) may now leave, and any other missing table is refused as before."""
    monkeypatch.setitem(serving.RETIRED_TABLES, "market", ("old",))
    with serving.build("market") as b:
        b.con.execute("CREATE TABLE t AS SELECT range AS i FROM range(10)")
        b.con.execute("CREATE TABLE old AS SELECT range AS i FROM range(5)")
        b.con.execute("CREATE TABLE gone AS SELECT range AS i FROM range(5)")
        b.swap({"t": 10, "old": 5, "gone": 5})
    with pytest.raises(serving.SwapRefused, match="table gone of the live file is missing"):
        _build_t(10)                                   # neither old nor gone: gone is no retired table
    with serving.build("market") as b:
        b.con.execute("CREATE TABLE t AS SELECT range AS i FROM range(10)")
        b.con.execute("CREATE TABLE gone AS SELECT range AS i FROM range(5)")
        b.swap({"t": 10, "gone": 5})                   # old, the retired one, is absent: accepted
    with serving.open_read("market") as con:
        names = {r[0] for r in con.execute("SELECT table_name FROM duckdb_tables()").fetchall()}
    assert "old" not in names and {"t", "gone"} <= names


def test_prices_retires_the_crsp_unadjusted_table_of_pr_353():
    """PR 382 removed ``crsp_unadjusted`` (the CRSP bars no action adjusted): CRSP serves no bar any more."""
    from RevenueChaser.rcdata.build.prices import serving as prices_serving
    assert "crsp_unadjusted" in serving.RETIRED_TABLES["prices"]
    assert not set(serving.RETIRED_TABLES["prices"]) & set(prices_serving.SCHEMAS), "a retired table is still built"


# ── base="live": the build file starts as a copy of the live one (PR 1 of the incremental plan) ─────────────────

def _mode(con) -> str:
    return con.execute("SELECT build_mode FROM serving_schema_version").fetchone()[0]


def test_a_live_based_build_starts_as_a_copy_and_appends_to_it():
    """The point of the mode: the rows already served are in the build file, so a build may add only what changed."""
    _build_t(10)
    first = _sha(serving.live_path("market"))
    with serving.build("market", base="live") as b:
        assert b.mode == "live"
        assert b.con.execute("SELECT count(*) FROM t").fetchone()[0] == 10, "the live rows are not in the copy"
        b.con.execute("INSERT INTO t SELECT range + 10 FROM range(5)")
        b.swap({"t": 15})
    with serving.open_read("market") as con:
        assert con.execute("SELECT count(*) FROM t").fetchone()[0] == 15
        assert _mode(con) == "live"
        assert con.execute("SELECT kind, version FROM serving_schema_version").fetchall() == [
            ("market", serving.SERVING_SCHEMA_VERSION["market"])], "the copied version row must be replaced"
    assert _sha(serving.prev_path("market")) == first, "the rollback file is still the file that was live"
    assert _leftovers() == []


def test_a_live_based_build_is_fresh_when_there_is_no_live_file():
    with serving.build("market", base="live") as b:
        assert b.mode == "fresh"
        b.con.execute("CREATE TABLE t AS SELECT range AS i FROM range(3)")
        b.swap({"t": 3})
    with serving.open_read("market") as con:
        assert _mode(con) == "fresh"


def test_a_live_file_of_another_serving_schema_version_is_not_appended_to(monkeypatch):
    """A version bump changes which tables the code builds, so its first build must be a whole one."""
    _build_t(10)
    monkeypatch.setitem(serving.SERVING_SCHEMA_VERSION, "market", serving.SERVING_SCHEMA_VERSION["market"] + 1)
    with serving.build("market", base="live") as b:
        assert b.mode == "fresh"
        tables = {r[0] for r in b.con.execute("SELECT table_name FROM duckdb_tables()").fetchall()}
        assert "t" not in tables, "nothing of the live file may be copied into a build of another version"


def test_a_live_based_build_keeps_the_shrink_guard_and_the_rollback():
    _build_t(10)
    before = _sha(serving.live_path("market"))
    with pytest.raises(serving.SwapRefused, match="shrinks"):
        with serving.build("market", base="live") as b:
            b.con.execute("DELETE FROM t WHERE i < 5")
            b.swap({"t": 5})
    assert _sha(serving.live_path("market")) == before and _leftovers() == []


def test_an_unknown_base_is_refused():
    with pytest.raises(ValueError, match="unknown build base"):
        with serving.build("market", base="incremental"):
            pass
    assert not Path(config.MARKET_DIR).exists(), "the refusal comes before any file is made"


def test_a_live_file_without_the_build_mode_column_gets_it_in_the_copy():
    """Why this needs no SERVING_SCHEMA_VERSION bump: a file built before the column is upgraded, not refused."""
    live = serving.live_path("market")
    live.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(live))
    con.execute(f"CREATE TABLE {serving.VERSION_TABLE} (kind TEXT, version INTEGER, built_at DOUBLE, "
                "build_id TEXT)")                                  # the four columns of before build_mode
    con.execute(f"INSERT INTO {serving.VERSION_TABLE} VALUES ('market', ?, 0, 'old')",
                [serving.SERVING_SCHEMA_VERSION["market"]])
    con.execute("CREATE TABLE t AS SELECT range AS i FROM range(10)")
    con.execute("CHECKPOINT")
    con.close()
    with serving.build("market", base="live") as b:
        assert b.mode == "live", "an older live file of the same version is still copied"
        b.con.execute("INSERT INTO t SELECT range + 10 FROM range(5)")
        b.swap({"t": 15})
    with serving.open_read("market") as con:
        assert _mode(con) == "live"
        assert con.execute("SELECT count(*) FROM t").fetchone()[0] == 15


def test_a_copy_that_breaks_leaves_no_partial_build_file(monkeypatch):
    """A half-copied 2.9 GB file on the live files' own SSD is what a failed copy must not leave behind."""
    _build_t(10)
    before = _sha(serving.live_path("market"))

    def no_space(src, dst):
        Path(dst).write_bytes(b"half a file")
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(serving.shutil, "copy2", no_space)
    with pytest.raises(OSError, match="No space"):
        with serving.build("market", base="live"):
            pass
    assert _leftovers() == [], "the partial copy must go with the failure"
    assert _sha(serving.live_path("market")) == before
