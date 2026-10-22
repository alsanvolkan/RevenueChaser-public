"""#156 (avo, 2026-10-09): the daily price tables live in their own ``.store/prices.duckdb``.

``price_bars_daily``, ``price_actions`` and their watermarks (``price_checks``, the shape of ``source_checks``)
declare MANIFEST ``"db": "prices"``, so the multi-GiB price history stays out of ``raw.duckdb`` and out of raw's
never-pruned weekly backups (review 2026-10-08 §6, F8). A store from before keeps the two tables and the price rows
of ``source_checks`` in raw until the first call moves them: copied, checked row by row, and only then removed from
raw. On the live store both tables are empty, so the move is two DROPs. Temp stores only.

D03 (#293, avo 2026-10-10): ``index_levels_daily`` (the SPX daily levels) joins them before it has a writer. A store
from before keeps the empty G1 table in raw until the first call moves it the same way, so on the live store the move
is one more DROP and the marker names it too.
"""
from __future__ import annotations

import logging
import math
from pathlib import Path

import duckdb
import pytest

from RevenueChaser.core import rawstore

PRICE_TABLES = ("price_bars_daily", "price_actions", "price_checks", "index_levels_daily")
MOVED = ("price_bars_daily", "price_actions")
MARKER = "price_tables_to_prices_db"

_BARS = [
    ("AAA", "2026-10-06", 1.0, 2.0, 0.5, 1.5, 1.4, 100.0, None, "yfinance", "yfinance", 1.7e9, "r1", 1.7e9),
    ("AAA", "2026-10-07", 1.5, 2.5, 1.0, float("nan"), None, None, None, None, "uw", 1.7e9, "r1", 1.7e9),
    ("BBB", "2026-10-07", 9.0, 9.0, 9.0, 9.0, 9.0, 5.0, None, "yfinance", "yfinance", None, None, None),
]
_ACTIONS = [
    ("AAA", "2026-10-06", "dividend", 0.25, "2026-10-06", "yfinance", 1.7e9, "r1", 1.7e9),
    ("BBB", "2026-09-01", "split", 2.0, None, "yfinance", 1.7e9, "r1", 1.7e9),
]
_CHECKS = [
    ("price_actions", "yfinance", "AAA", 1.7e9, "r1"),
    ("price_bars_daily", "yfinance_max", "AAA", 1.7e9, "r1"),
    ("security_master", "uw", "AAA", 1.6e9, "id1"),           # a D22 identity stamp: stays in raw
]


def _con(path: Path, read_only: bool = True):
    return duckdb.connect(str(path), read_only=read_only)


def _tables(path: Path) -> set[str]:
    if not path.exists():
        return set()
    con = _con(path)
    try:
        return {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables "
                                          "WHERE table_schema = 'main'").fetchall()}
    finally:
        con.close()


def _dump(path: Path, table: str, where: str = "") -> list[tuple]:
    """Every row of ``table`` in ``path`` as text, sorted (NaN compares equal to itself here)."""
    con = _con(path)
    try:
        cols = [r[0] for r in con.execute("SELECT column_name FROM information_schema.columns WHERE table_name = ? "
                                          "ORDER BY ordinal_position", [table]).fetchall()]
        rows = con.execute(f"SELECT {', '.join(cols)} FROM {table} {where}").fetchall()
    finally:
        con.close()
    def cell(v):
        return "nan" if isinstance(v, float) and math.isnan(v) else (None if v is None else str(v))
    return sorted(tuple(cell(v) for v in r) for r in rows)


def _fresh():
    rawstore._TABLE_READY.clear()
    rawstore._ENSURED.clear()


@pytest.fixture
def legacy_store(isolate_rawstore):
    """A store as main 2de1f17 left it: the #156 tables in raw.duckdb, prices.duckdb not there yet (the D03 table,
    which moved later, is the fixtures' at the end of this file)."""
    saved = dict(rawstore.MANIFEST)
    try:
        for t in MOVED:                                          # the MANIFEST of 2de1f17, for the build only
            rawstore.MANIFEST[t] = {k: v for k, v in saved[t].items() if k != "db"}
        rawstore.MANIFEST.pop("price_checks", None)
        _fresh()
        rawstore.ensure_all_tables()
    finally:
        rawstore.MANIFEST.clear()
        rawstore.MANIFEST.update(saved)                          # same entries, same order
        _fresh()
    if rawstore.prices_db_path().exists():
        rawstore.prices_db_path().unlink()
    assert {"price_bars_daily", "price_actions", "source_checks"} <= _tables(Path(rawstore.DB_PATH))
    return Path(rawstore.DB_PATH)


def _fill(raw: Path) -> None:
    con = _con(raw, read_only=False)
    try:
        con.executemany("INSERT INTO price_bars_daily VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", _BARS)
        con.executemany("INSERT INTO price_actions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", _ACTIONS)
        con.executemany("INSERT INTO source_checks VALUES (?, ?, ?, ?, ?)", _CHECKS)
    finally:
        con.close()


def _marker() -> list[dict]:
    return rawstore.query("SELECT dataset, source_count, source_hash, verify FROM migration_manifest "
                          "WHERE dataset = ?", [MARKER])


# ── routing and the file ─────────────────────────────────────────────────────

def test_the_price_tables_route_to_prices_duckdb_beside_the_raw_store(isolate_rawstore):
    assert rawstore.prices_db_path() == Path(rawstore.DB_PATH).parent / "prices.duckdb"
    assert rawstore._db_path("prices") == rawstore.prices_db_path()
    assert sorted(n for n in rawstore.MANIFEST if rawstore._db_of(n) == "prices") == sorted(PRICE_TABLES)
    assert rawstore._db_of("source_checks") == "raw"            # D22's identity stamps stay beside their tables
    assert rawstore._db_of("index_levels_daily") == "prices"     # D03 beside D01 (#293, avo 2026-10-10)


def test_price_checks_has_the_shape_of_source_checks():
    m = rawstore.MANIFEST
    for field in ("key", "columns", "kind", "write", "history_mode"):
        assert m["price_checks"][field] == m["source_checks"][field], field


def test_a_fresh_store_makes_prices_duckdb_with_exactly_the_four_tables(rawstore_tmp_db):
    prices = rawstore.prices_db_path()
    assert prices.is_file()
    assert _tables(prices) == set(PRICE_TABLES)
    assert not (set(PRICE_TABLES) & _tables(Path(rawstore.DB_PATH)))
    assert "source_checks" in _tables(Path(rawstore.DB_PATH))
    assert rawstore.count("price_bars_daily") == 0                # Data Health shows 0, not "no table"
    assert _marker() == []                                       # nothing to move on a fresh store


def test_price_writes_land_in_prices_duckdb_and_one_transaction_stays_in_one_file(rawstore_tmp_db):
    rawstore.write_rows("price_bars_daily", [{"ticker": "AAA", "session_date": "2026-10-07", "close": 1.0}])
    rawstore.atomic(lambda tx: tx.write_rows("price_checks", [
        {"dataset": "price_actions", "source": "yfinance", "key": "AAA", "checked_at": 1.0, "run_id": "r"}]),
        db="prices")
    assert _dump(rawstore.prices_db_path(), "price_bars_daily")[0][0] == "AAA"
    assert len(_dump(rawstore.prices_db_path(), "price_checks")) == 1
    with pytest.raises(rawstore.RawStoreError, match="one transaction, one file"):
        rawstore.atomic(lambda tx: tx.write_rows("source_checks", [
            {"dataset": "x", "source": "y", "key": "z", "checked_at": 1.0, "run_id": "r"}]), db="prices")


def test_reset_db_removes_prices_only_on_a_full_reset(rawstore_tmp_db):
    rawstore.write_rows("price_bars_daily", [{"ticker": "AAA", "session_date": "2026-10-07", "close": 1.0}])
    rawstore.reset_db(include_edgar=False)
    assert rawstore.count("price_bars_daily") == 1
    rawstore.reset_db(include_edgar=True)
    assert rawstore.count("price_bars_daily") == 0


def test_the_move_did_not_bump_the_schema_version():
    """A new file and no changed table (the #72 precedent): an old checkout ignores prices.duckdb."""
    assert rawstore.schema_version() == "12"


# ── the one-time move out of raw.duckdb ──────────────────────────────────────

def test_the_live_case_two_empty_tables_are_dropped_from_raw(legacy_store):
    rawstore.ensure_all_tables()
    raw = _tables(legacy_store)
    assert not (set(MOVED) & raw) and "source_checks" in raw
    assert _tables(rawstore.prices_db_path()) == set(PRICE_TABLES)
    assert all(rawstore.count(t) == 0 for t in PRICE_TABLES)
    assert _marker() == [{"dataset": MARKER, "source_count": 0, "verify": "PASS",
                          "source_hash": "price_bars_daily:0,price_actions:0"}]


def test_rows_move_whole_and_raw_keeps_only_its_own_watermarks(legacy_store):
    _fill(legacy_store)
    bars, acts = _dump(legacy_store, "price_bars_daily"), _dump(legacy_store, "price_actions")
    rawstore.ensure_all_tables()
    prices = rawstore.prices_db_path()
    assert _dump(prices, "price_bars_daily") == bars                      # NULL and NaN kept
    assert _dump(prices, "price_actions") == acts
    assert _dump(prices, "price_checks") == sorted(
        tuple(None if v is None else str(v) for v in r) for r in _CHECKS[:2])
    assert not (set(MOVED) & _tables(legacy_store))
    assert _dump(legacy_store, "source_checks") == [("security_master", "uw", "AAA", str(1.6e9), "id1")]
    assert _marker() == [{"dataset": MARKER, "source_count": 7, "verify": "PASS",
                          "source_hash": "price_bars_daily:3,price_actions:2,source_checks:2"}]


def test_the_second_call_moves_nothing_and_changes_nothing(legacy_store):
    _fill(legacy_store)
    rawstore.ensure_all_tables()
    before = {t: _dump(rawstore.prices_db_path(), t) for t in PRICE_TABLES}
    raw_checks = _dump(legacy_store, "source_checks")
    marker = rawstore.query("SELECT * FROM migration_manifest WHERE dataset = ?", [MARKER])
    _fresh()
    rawstore.ensure_all_tables()
    assert {t: _dump(rawstore.prices_db_path(), t) for t in PRICE_TABLES} == before
    assert _dump(legacy_store, "source_checks") == raw_checks
    assert rawstore.query("SELECT * FROM migration_manifest WHERE dataset = ?", [MARKER]) == marker


def test_a_conflicting_row_in_prices_keeps_the_raw_table_and_warns(legacy_store, caplog):
    """Both files hold a row for one key with different values: nothing is overwritten, a human decides."""
    _fill(legacy_store)
    prices = rawstore.prices_db_path()
    con = _con(prices, read_only=False)
    try:
        con.execute(rawstore._ddl("price_bars_daily"))
        con.execute("INSERT INTO price_bars_daily (ticker, session_date, close) VALUES ('AAA', '2026-10-06', 99.0)")
    finally:
        con.close()
    bars = _dump(legacy_store, "price_bars_daily")
    with caplog.at_level(logging.WARNING, logger="RevenueChaser.core.rawstore"):
        rawstore.ensure_all_tables()
    assert _dump(legacy_store, "price_bars_daily") == bars                # raw untouched
    assert [r[5] for r in _dump(prices, "price_bars_daily")] == ["99.0"]  # prices untouched: the copy rolled back
    assert any("price_bars_daily" in r.getMessage() and "by hand" in r.getMessage() for r in caplog.records)
    assert "price_actions" not in _tables(legacy_store)                   # the other tables still moved
    assert len(_dump(prices, "price_checks")) == 2


def test_a_move_cut_off_after_the_copy_is_finished_by_the_next_call(legacy_store):
    """The copy committed in prices.duckdb but the process stopped before raw dropped its table."""
    _fill(legacy_store)
    bars = _dump(legacy_store, "price_bars_daily")
    prices = rawstore.prices_db_path()
    con = _con(prices, read_only=False)
    try:
        con.execute(rawstore._ddl("price_bars_daily"))
        con.executemany("INSERT INTO price_bars_daily VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", _BARS)
    finally:
        con.close()
    rawstore.ensure_all_tables()
    assert _dump(prices, "price_bars_daily") == bars
    assert "price_bars_daily" not in _tables(legacy_store)


def test_a_row_written_to_raw_during_the_move_stops_the_drop(legacy_store, monkeypatch, caplog):
    """A row that lands in raw after the copy must not be dropped with the table; the next call moves it."""
    _fill(legacy_store)
    real = rawstore._copy_into_prices
    late = ("CCC", "2026-10-07", 1.0, 1.0, 1.0, 1.0, None, 1.0, None, None, "uw", None, None, None)

    def racing(src, dst, cols, parquet, expect):
        n = real(src, dst, cols, parquet, expect)
        if src == "price_bars_daily":
            with rawstore.connect(db="raw") as conn:                      # an old writer lands in the gap
                conn.execute("INSERT INTO price_bars_daily VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", late)
        return n

    monkeypatch.setattr(rawstore, "_copy_into_prices", racing)
    with caplog.at_level(logging.WARNING, logger="RevenueChaser.core.rawstore"):
        rawstore.ensure_all_tables()
    assert len(_dump(legacy_store, "price_bars_daily")) == 4             # raw kept, the late row with it
    assert any("changed during the move" in r.getMessage() for r in caplog.records)
    monkeypatch.setattr(rawstore, "_copy_into_prices", real)
    _fresh()
    rawstore.ensure_all_tables()
    assert "price_bars_daily" not in _tables(legacy_store)
    assert len(_dump(rawstore.prices_db_path(), "price_bars_daily")) == 4


def test_a_non_empty_raw_table_with_a_column_the_manifest_lacks_is_kept(legacy_store, caplog):
    _fill(legacy_store)
    con = _con(legacy_store, read_only=False)
    try:
        con.execute("ALTER TABLE price_actions ADD COLUMN legacy TEXT")
        con.execute("UPDATE price_actions SET legacy = 'x'")
    finally:
        con.close()
    with caplog.at_level(logging.WARNING, logger="RevenueChaser.core.rawstore"):
        rawstore.ensure_all_tables()
    assert len(_dump(legacy_store, "price_actions")) == 2
    assert _dump(rawstore.prices_db_path(), "price_actions") == []
    assert any("price_actions" in r.getMessage() and "legacy" in r.getMessage() for r in caplog.records)
    assert "price_bars_daily" not in _tables(legacy_store)


# ── review round (2026-10-09): the guards each get a test, two processes, the marker ─────────────────────────────

def _move() -> list[str]:
    """One run of the move as ``ensure_all_tables`` makes it: the prices tables exist, then the move runs."""
    with rawstore.connect(db="prices") as conn:
        rawstore._ensure_db_tables(conn, "prices")
    return rawstore._migrate_price_tables()


_LATE = ("CCC", "2026-10-07", 1.0, 1.0, 1.0, 1.0, None, 1.0, None, None, "uw", None, None, None)


def test_a_row_written_to_an_empty_raw_table_before_its_drop_keeps_the_table(legacy_store, monkeypatch):
    """The live path: the probe saw the table empty, then a row landed; the DROP must not take it with it."""
    real = rawstore._move_to_prices

    def racing(src, where, dst, have, cols, before):
        if src == "price_bars_daily":
            with rawstore.connect(db="raw") as conn:                      # an old writer lands in the gap
                conn.execute("INSERT INTO price_bars_daily VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", _LATE)
        return real(src, where, dst, have, cols, before)

    monkeypatch.setattr(rawstore, "_move_to_prices", racing)
    _move()
    assert len(_dump(legacy_store, "price_bars_daily")) == 1             # kept, with its row
    assert "price_actions" not in _tables(legacy_store)
    monkeypatch.setattr(rawstore, "_move_to_prices", real)
    _move()
    assert "price_bars_daily" not in _tables(legacy_store)
    assert [r[0] for r in _dump(rawstore.prices_db_path(), "price_bars_daily")] == ["CCC"]


def test_a_row_written_between_the_probe_and_the_copy_moves_with_the_others(legacy_store, monkeypatch, caplog):
    """The copy reads raw afresh, in one snapshot with its fingerprint: a row that landed after the probe moves too."""
    _fill(legacy_store)
    real = rawstore._move_to_prices

    def late_row(src, where, dst, have, cols, before):
        if src == "price_bars_daily":
            with rawstore.connect(db="raw") as conn:
                conn.execute("INSERT INTO price_bars_daily VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", _LATE)
        return real(src, where, dst, have, cols, before)

    monkeypatch.setattr(rawstore, "_move_to_prices", late_row)
    with caplog.at_level(logging.WARNING, logger="RevenueChaser.core.rawstore"):
        assert _move() == ["price_bars_daily:4", "price_actions:2", "source_checks:2"]
    assert [r.getMessage() for r in caplog.records] == []
    assert "price_bars_daily" not in _tables(legacy_store)
    assert len(_dump(rawstore.prices_db_path(), "price_bars_daily")) == 4


def test_a_short_parquet_copy_is_refused_and_raw_keeps_every_row(legacy_store, monkeypatch):
    """The parquet fingerprint is the only guard against a short copy: EXCEPT cannot see rows the parquet lacks."""
    _fill(legacy_store)
    bars = _dump(legacy_store, "price_bars_daily")
    real = rawstore._copy_into_prices

    def short(src, dst, cols, parquet, expect):
        if src == "price_bars_daily":
            cut = parquet.with_name("cut.parquet")
            duckdb.execute(f"COPY (SELECT * FROM read_parquet('{parquet.as_posix()}') LIMIT 2) "
                           f"TO '{cut.as_posix()}' (FORMAT parquet)")
            cut.replace(parquet)
        return real(src, dst, cols, parquet, expect)

    monkeypatch.setattr(rawstore, "_copy_into_prices", short)
    with pytest.raises(rawstore.RawStoreError, match="parquet copy does not match"):
        _move()
    assert _dump(legacy_store, "price_bars_daily") == bars
    assert _dump(rawstore.prices_db_path(), "price_bars_daily") == []    # the copy rolled back


def test_a_changed_value_with_the_same_row_count_stops_the_drop(legacy_store, monkeypatch, caplog):
    """The fingerprint compares content, not only the count: an UPDATE in the gap keeps the raw table."""
    _fill(legacy_store)
    real = rawstore._copy_into_prices

    def racing(src, dst, cols, parquet, expect):
        n = real(src, dst, cols, parquet, expect)
        if src == "price_bars_daily":
            with rawstore.connect(db="raw") as conn:
                conn.execute("UPDATE price_bars_daily SET close = 7.0 WHERE ticker = 'BBB'")
        return n

    monkeypatch.setattr(rawstore, "_copy_into_prices", racing)
    with caplog.at_level(logging.WARNING, logger="RevenueChaser.core.rawstore"):
        _move()
    assert "price_bars_daily" in _tables(legacy_store)
    assert ("BBB", "7.0") in [(r[0], r[5]) for r in _dump(legacy_store, "price_bars_daily")]
    assert any("changed during the move" in r.getMessage() for r in caplog.records)


def _peer_runs_the_whole_move_first(monkeypatch, hook: str):
    """Another process (the dashboard and a job started together) runs the whole move in the gap before this
    process's first ``hook`` call returns: after this process probed raw and before it acts on what it saw."""
    real = getattr(rawstore, hook)
    state = {"peer": False}

    def racing(*args):
        if hook == "_copy_into_prices":
            n = real(*args)
        if not state["peer"]:
            state["peer"] = True
            monkeypatch.setattr(rawstore, hook, real)
            _move()
            monkeypatch.setattr(rawstore, hook, racing)
        return n if hook == "_copy_into_prices" else real(*args)

    monkeypatch.setattr(rawstore, hook, racing)


@pytest.mark.parametrize("hook,filled", [("_move_to_prices", False), ("_move_to_prices", True),
                                         ("_copy_into_prices", True)],
                         ids=["live_empty_before_the_drop", "rows_before_the_copy", "rows_before_the_drop"])
def test_two_processes_moving_at_once_raise_nothing_warn_nothing_and_lose_nothing(
        legacy_store, monkeypatch, caplog, hook, filled):
    if filled:
        _fill(legacy_store)
    bars, acts = _dump(legacy_store, "price_bars_daily"), _dump(legacy_store, "price_actions")
    _peer_runs_the_whole_move_first(monkeypatch, hook)
    with caplog.at_level(logging.INFO, logger="RevenueChaser.core.rawstore"):
        _move()
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []
    assert not (set(MOVED) & _tables(legacy_store))
    prices = rawstore.prices_db_path()
    assert (_dump(prices, "price_bars_daily"), _dump(prices, "price_actions")) == (bars, acts)
    assert len(_dump(legacy_store, "source_checks")) == (1 if filled else 0)
    assert _marker()[0]["source_hash"] == ("price_bars_daily:3,price_actions:2,source_checks:2" if filled
                                           else "price_bars_daily:0,price_actions:0")


class _Killed(BaseException):
    """The process stops here (a SIGKILL, a power cut): nothing after it runs."""


@pytest.mark.parametrize("filled,killed_after,marker", [
    (False, "price_bars_daily", ("price_bars_daily:0,price_actions:0", 0)),
    (False, "source_checks", ("price_bars_daily:0,price_actions:0", 0)),
    (True, "price_actions", ("price_bars_daily:3,price_actions:2,source_checks:2", 7)),
])
def test_the_marker_names_every_table_moved_even_when_a_call_was_cut_off(
        legacy_store, monkeypatch, filled, killed_after, marker):
    if filled:
        _fill(legacy_store)
    real = rawstore._move_to_prices

    def killed(src, *args):
        n = real(src, *args)
        if src == killed_after:
            raise _Killed
        return n

    monkeypatch.setattr(rawstore, "_move_to_prices", killed)
    with pytest.raises(_Killed):
        _move()
    monkeypatch.setattr(rawstore, "_move_to_prices", real)
    _move()
    assert not (set(MOVED) & _tables(legacy_store))
    assert [(m["source_hash"], m["source_count"]) for m in _marker()] == [marker]


# ── D03 (#293, avo 2026-10-10): index_levels_daily moves the same way ───────────────────────────────────────────────

D03 = "index_levels_daily"
LIVE_156_MARKER = "price_bars_daily:0,price_actions:0"        # what the #156 move left on the live store (case above)
_LEVELS = [
    ("^GSPC", "2026-10-06", 1.0, 2.0, 0.5, 1.5, None, "yfinance", 1.7e9, "r1", 1.7e9),
    ("^GSPC", "2026-10-07", 1.5, 2.5, 1.0, float("nan"), None, None, None, None, None),
]


def _store_with_in_raw(tables: tuple[str, ...], *, before_156: bool) -> Path:
    """Build a store with ``tables`` declared in raw, the way an older MANIFEST made it."""
    saved = dict(rawstore.MANIFEST)
    try:
        for t in tables:
            rawstore.MANIFEST[t] = {k: v for k, v in saved[t].items() if k != "db"}
        if before_156:
            rawstore.MANIFEST.pop("price_checks", None)
        _fresh()
        rawstore.ensure_all_tables()
    finally:
        rawstore.MANIFEST.clear()
        rawstore.MANIFEST.update(saved)
        _fresh()
    return Path(rawstore.DB_PATH)


@pytest.fixture
def d03_store(isolate_rawstore):
    """A store as main 319591b left it: the #156 tables already in prices.duckdb (marker written), the empty
    ``index_levels_daily`` still in raw."""
    raw = _store_with_in_raw((D03,), before_156=False)
    rawstore.write_rows("migration_manifest", [{
        "dataset": MARKER, "migrated_at": "2026-10-09T16:00:00Z", "schema_version": "11", "source_count": 0,
        "source_bytes": 0, "source_hash": LIVE_156_MARKER, "verify": "PASS"}])
    assert D03 in _tables(raw)
    assert _tables(rawstore.prices_db_path()) == set(PRICE_TABLES) - {D03}
    return raw


def test_the_live_case_d03_the_empty_index_levels_table_leaves_raw_for_prices(d03_store):
    rawstore.ensure_all_tables()
    assert D03 not in _tables(d03_store)
    assert _tables(rawstore.prices_db_path()) == set(PRICE_TABLES)
    assert rawstore.count(D03) == 0
    assert _marker() == [{"dataset": MARKER, "source_count": 0, "verify": "PASS",
                          "source_hash": f"{LIVE_156_MARKER},index_levels_daily:0"}]


def test_index_level_rows_in_raw_move_whole(d03_store):
    """No writer exists, so the live table is empty; were a row there, it moves with the table, not lost in raw."""
    con = _con(d03_store, read_only=False)
    try:
        con.executemany("INSERT INTO index_levels_daily VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", _LEVELS)
    finally:
        con.close()
    levels = _dump(d03_store, D03)
    rawstore.ensure_all_tables()
    assert _dump(rawstore.prices_db_path(), D03) == levels                 # NULL and NaN kept
    assert D03 not in _tables(d03_store)
    assert _marker() == [{"dataset": MARKER, "source_count": 2, "verify": "PASS",
                          "source_hash": f"{LIVE_156_MARKER},index_levels_daily:2"}]


def test_the_d03_move_runs_once(d03_store):
    rawstore.ensure_all_tables()
    marker = rawstore.query("SELECT * FROM migration_manifest WHERE dataset = ?", [MARKER])
    _fresh()
    assert rawstore._migrate_price_tables() == []
    rawstore.ensure_all_tables()
    assert rawstore.query("SELECT * FROM migration_manifest WHERE dataset = ?", [MARKER]) == marker
    assert D03 not in _tables(d03_store)


def test_a_store_from_before_both_moves_moves_all_three_tables_in_one_call(isolate_rawstore):
    """A raw backup from before 2026-10-09 restored under this code: #156's tables and D03's leave raw together."""
    raw = _store_with_in_raw(MOVED + (D03,), before_156=True)
    rawstore.prices_db_path().unlink()
    assert set(MOVED + (D03,)) <= _tables(raw)
    rawstore.ensure_all_tables()
    assert not (set(MOVED + (D03,)) & _tables(raw))
    assert _tables(rawstore.prices_db_path()) == set(PRICE_TABLES)
    assert _marker() == [{"dataset": MARKER, "source_count": 0, "verify": "PASS",
                          "source_hash": "price_bars_daily:0,price_actions:0,index_levels_daily:0"}]
