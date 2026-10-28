"""The ``rawstore_tmp_db`` fixture copies a per-session template instead of applying the schema per test.

The template is built once per pytest process by ``ensure_all_tables`` itself, and every test gets its OWN copy of
the files in its own ``tmp_path``; the template is never handed to a test. A test marked
``rawstore_schema_from_scratch``, or one whose schema inputs (``MANIFEST`` and the migration tables) differ from the
template's, still gets the apply run on its own file. See ``conftest.py`` and ``docs/testing_framework.md``.
"""
from __future__ import annotations

import pytest

from RevenueChaser.core import rawstore


def _stored_watchlist() -> set[str]:
    """The tickers of the store's ``watchlist`` table, read by name: since PR 4 ``rawstore.load_watchlist`` is the
    stock watch list in ops.sqlite, and this file uses the old table as its sample table."""
    return {r["ticker"] for r in rawstore.get_all("watchlist")}


@pytest.fixture
def ensure_calls(monkeypatch):
    """Every ``ensure_all_tables`` call from here on, by the raw file it applied to."""
    calls = []
    real = rawstore.ensure_all_tables

    def spy():
        calls.append(rawstore.DB_PATH)
        return real()

    monkeypatch.setattr(rawstore, "ensure_all_tables", spy)
    return calls


def _tables(db: str) -> set[str]:
    return {r["table_name"] for r in rawstore.query(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'", db=db)}


def test_an_unmarked_test_gets_its_own_copy_and_runs_no_apply(ensure_calls, rawstore_tmp_db, tmp_path,
                                                              rawstore_template):
    assert ensure_calls == []
    assert rawstore_tmp_db == tmp_path / "raw.duckdb" == rawstore.DB_PATH
    assert rawstore_template.dir not in rawstore_tmp_db.parents and rawstore_tmp_db.parent != rawstore_template.dir
    for path in (rawstore_tmp_db, rawstore.edgar_db_path(), rawstore.prices_db_path()):
        assert path.is_file()
    for db in ("raw", "edgar", "prices"):   # every MANIFEST table, in its own file, as the apply makes it
        assert {n for n in rawstore.MANIFEST if rawstore._db_of(n) == db} <= _tables(db)
    assert rawstore.get("store_schema", "schema_version")["value"] == rawstore.schema_version()
    assert not rawstore.history_db_path().exists()       # the apply does not make the history file; nor does the copy


@pytest.mark.rawstore_schema_from_scratch
def test_an_opt_out_test_runs_the_apply_on_its_own_file(ensure_calls, rawstore_tmp_db, tmp_path):
    assert ensure_calls == [tmp_path / "raw.duckdb"]
    assert rawstore.count("watchlist") == 0


@pytest.fixture
def extra_table(monkeypatch):
    """A schema input the template was not built from: a MANIFEST patched before the store is made."""
    monkeypatch.setitem(rawstore.MANIFEST, "template_probe", {
        "kind": "tabular", "key": ["k"], "write": "upsert", "columns": ["k TEXT", "v DOUBLE", "PRIMARY KEY (k)"]})


def test_a_manifest_patched_before_the_store_is_made_is_applied_from_scratch(ensure_calls, extra_table,
                                                                             rawstore_tmp_db, tmp_path):
    assert ensure_calls == [tmp_path / "raw.duckdb"]
    assert "template_probe" in _tables("raw")


@pytest.fixture
def reordered_manifest(monkeypatch):
    """The same MANIFEST in another key order, as monkeypatch.delitem's undo leaves it for the next test."""
    reordered = dict(rawstore.MANIFEST)
    reordered["price_actions"] = reordered.pop("price_actions")
    monkeypatch.setattr(rawstore, "MANIFEST", reordered)


def test_a_manifest_in_another_order_still_gets_the_copy(ensure_calls, reordered_manifest, rawstore_tmp_db):
    assert list(rawstore.MANIFEST)[-1] == "price_actions"
    assert ensure_calls == []
    assert "price_actions" in _tables("prices")


@pytest.fixture
def written_first():
    """A store written before ``rawstore_tmp_db`` runs, as ``core/identity/tests/test_identity_lock_pl6a.py``'s
    autouse ``universe`` fixture does: a copy over it would throw the row away."""
    rawstore.write_rows("watchlist", [{"ticker": "FIRST"}])


def test_a_store_written_before_the_fixture_keeps_its_rows_and_gets_the_apply(ensure_calls, written_first,
                                                                              rawstore_tmp_db, tmp_path):
    assert ensure_calls == [tmp_path / "raw.duckdb"]
    assert _stored_watchlist() == {"FIRST"}
    assert "store_schema" in _tables("raw")


@pytest.fixture
def other_name(monkeypatch, tmp_path):
    """A raw file not named ``raw.duckdb``: the template's raw file cannot simply be copied to it."""
    monkeypatch.setattr(rawstore, "DB_PATH", tmp_path / "other.duckdb")


def test_a_raw_file_with_another_name_gets_the_apply(ensure_calls, other_name, rawstore_tmp_db, tmp_path):
    assert ensure_calls == [tmp_path / "other.duckdb"]
    assert "watchlist" in _tables("raw")
    assert not (tmp_path / "raw.duckdb").exists()


def _changed(value):
    if isinstance(value, dict):
        return {**value, "fingerprint_probe": None}
    if isinstance(value, (list, tuple)):
        return type(value)([*value, "fingerprint_probe"])
    return value + "-probe"


@pytest.mark.parametrize("name", ["_SCHEMA_VERSION", "MANIFEST", "CHANGES", "_RENAMES", "_MERGES", "_VIEWS",
                                  "_KEY_REBUILDS", "_PRICE_MOVES", "_EDGAR_TABLES"])
def test_a_change_to_any_schema_input_moves_the_fingerprint(name, monkeypatch):
    """Each input the copy is checked against; a change to one sends the test to the apply (see the MANIFEST test)."""
    from RevenueChaser.conftest import _schema_fingerprint
    before = _schema_fingerprint()
    monkeypatch.setattr(rawstore, name, _changed(getattr(rawstore, name)))
    assert _schema_fingerprint() != before


# The two tests below run in file order on one worker (--dist loadfile): the first changes its store in every way
# it can, the second must see a store exactly as the apply makes it, and the template must be untouched.
def test_a_test_that_changes_its_store_a(rawstore_tmp_db, rawstore_template):
    rawstore.write_rows("watchlist", [{"ticker": "LEAK"}])
    rawstore.write_rows("edgar_documents", [{"accession": "A1", "cik": "1", "form": "10-K"}])
    rawstore.execute("ALTER TABLE board_settings ADD COLUMN leaked INTEGER")
    with rawstore.connect() as conn:                     # past the history guard, as a broken migration would
        conn.execute("DROP TABLE equity_universe_changes")
    rawstore.execute("CREATE TABLE leaked_table (a INTEGER)")
    assert _stored_watchlist() == {"LEAK"}


def test_a_test_that_changes_its_store_b_sees_none_of_it(rawstore_tmp_db, rawstore_template):
    assert rawstore.count("watchlist") == 0
    assert rawstore.count("edgar_documents") == 0
    cols = {r["column_name"] for r in rawstore.query(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'board_settings'")}
    assert "leaked" not in cols
    assert "equity_universe_changes" in _tables("raw")
    assert "leaked_table" not in _tables("raw")
    assert rawstore_template.unchanged()
