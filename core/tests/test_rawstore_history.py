"""History tables: a MANIFEST ``"history": true`` makes rawstore refuse rewrites (#286, B11).

The docs ask callers not to ``update_columns`` / ``replace_all`` / ``delete`` / ``clear`` a table that
holds the past (the forecast ledger, the session snapshots of #278, the change history of #279), but
nothing enforced it: a cleanup script or a refactor could rewrite history. No real table is flagged yet
(the flags go on with the tables), so these tests flag a test-only MANIFEST entry.
"""
from __future__ import annotations

import contextlib
import json
import random
from pathlib import Path

import duckdb
import pytest

from RevenueChaser.core import rawstore

HIST = "t_history_demo"


def _spec(**over):
    return {
        "key": ["session", "ticker"],
        "columns": ["session TEXT", "ticker TEXT", "score DOUBLE", "PRIMARY KEY (session, ticker)"],
        "kind": "tabular", "write": "upsert", "history": True, "sources": [], **over,
    }


@pytest.fixture
def hist(rawstore_tmp_db, monkeypatch):
    """A flagged test-only table with two rows already in it."""
    monkeypatch.setitem(rawstore.MANIFEST, HIST, _spec())
    rawstore.write_rows(HIST, [{"session": "2026-10-01", "ticker": "AAPL", "score": 1.0},
                               {"session": "2026-10-01", "ticker": "MSFT", "score": 2.0}])
    return HIST


@pytest.fixture(autouse=True)
def _fresh_planner_cache(monkeypatch):
    """#308 A1(b): the canary is memoised per process; reset it so tests do not leak state."""
    monkeypatch.setattr(rawstore, "_PLANNER_OK", None, raising=False)


def _scores():
    return {(r["session"], r["ticker"]): r["score"]
            for r in rawstore.query(f"SELECT session, ticker, score FROM {HIST}")}


_BEFORE = {("2026-10-01", "AAPL"): 1.0, ("2026-10-01", "MSFT"): 2.0}


# ── the four rewrite calls raise ─────────────────────────────────────────────
class TestForbiddenCalls:
    def test_replace_all_raises(self, hist):
        with pytest.raises(rawstore.RawStoreError, match="history"):
            rawstore.replace_all(hist, [{"session": "2026-10-02", "ticker": "NVDA", "score": 3.0}])
        assert _scores() == _BEFORE

    def test_replace_all_raises_even_when_empty_is_allowed(self, hist):
        with pytest.raises(rawstore.RawStoreError, match="history"):
            rawstore.replace_all(hist, [], allow_empty=True)
        assert _scores() == _BEFORE

    def test_update_columns_raises(self, hist):
        with pytest.raises(rawstore.RawStoreError, match="history"):
            rawstore.update_columns(hist, "ticker",
                                    [{"ticker": "AAPL", "score": 99.0}], ["score"])
        assert _scores() == _BEFORE

    def test_delete_raises(self, hist):
        with pytest.raises(rawstore.RawStoreError, match="history"):
            rawstore.delete(hist, ("2026-10-01", "AAPL"))
        assert _scores() == _BEFORE

    def test_clear_raises(self, hist):
        with pytest.raises(rawstore.RawStoreError, match="history"):
            rawstore.clear(hist)
        assert _scores() == _BEFORE

    def test_the_message_names_the_table_and_the_call(self, hist):
        with pytest.raises(rawstore.RawStoreError) as err:
            rawstore.clear(hist)
        assert HIST in str(err.value) and "clear" in str(err.value)

    @pytest.mark.parametrize("call", ["replace_all", "update_columns", "delete", "clear"])
    def test_the_same_calls_work_on_a_table_without_the_flag(self, rawstore_tmp_db, monkeypatch, call):
        """The guard is the flag's, not a blanket refusal: the unflagged twin still rewrites."""
        monkeypatch.setitem(rawstore.MANIFEST, HIST, _spec(history=False))
        rawstore.write_rows(HIST, [{"session": "s", "ticker": "AAPL", "score": 1.0}])
        {"replace_all": lambda: rawstore.replace_all(
            HIST, [{"session": "s", "ticker": "NVDA", "score": 3.0}]),
         "update_columns": lambda: rawstore.update_columns(
             HIST, "ticker", [{"ticker": "AAPL", "score": 9.0}], ["score"]),
         "delete": lambda: rawstore.delete(HIST, ("s", "AAPL")),
         "clear": lambda: rawstore.clear(HIST)}[call]()
        assert _scores() != {("s", "AAPL"): 1.0}


# ── write_rows: the first write for a key wins ──────────────────────────────
class TestFirstWriteWins:
    @pytest.mark.parametrize("mode", ["upsert", "replace", "append"])
    def test_a_duplicate_key_leaves_the_stored_row_unchanged(self, rawstore_tmp_db, monkeypatch, mode):
        monkeypatch.setitem(rawstore.MANIFEST, HIST, _spec(write=mode))
        first = {"session": "2026-10-01", "ticker": "AAPL", "score": 1.0}
        rawstore.write_rows(HIST, [first])
        rawstore.write_rows(HIST, [{**first, "score": 99.0}])
        assert _scores() == {("2026-10-01", "AAPL"): 1.0}

    def test_new_keys_in_the_same_batch_are_still_written(self, hist):
        n = rawstore.write_rows(HIST, [
            {"session": "2026-10-01", "ticker": "AAPL", "score": 99.0},     # duplicate: ignored
            {"session": "2026-10-02", "ticker": "AAPL", "score": 3.0}])     # new session: kept
        assert n == 2
        assert _scores() == {**_BEFORE, ("2026-10-02", "AAPL"): 3.0}

    def test_a_key_twice_in_one_batch_keeps_the_first(self, hist):
        rawstore.write_rows(HIST, [{"session": "2026-10-03", "ticker": "AMD", "score": 5.0},
                                   {"session": "2026-10-03", "ticker": "AMD", "score": 6.0}])
        assert _scores()[("2026-10-03", "AMD")] == 5.0

    def test_a_large_batch_takes_the_bulk_path_with_the_same_rule(self, hist):
        rows = [{"session": "2026-10-04", "ticker": f"T{i}", "score": float(i)} for i in range(100)]
        rawstore.write_rows(HIST, rows)
        rawstore.write_rows(HIST, [{**r, "score": -1.0} for r in rows]
                            + [{"session": "2026-10-01", "ticker": "AAPL", "score": 99.0}])
        got = _scores()
        assert got[("2026-10-04", "T7")] == 7.0 and got[("2026-10-01", "AAPL")] == 1.0
        assert len(got) == 102

    def test_the_transaction_handle_follows_the_same_rule(self, hist):
        with rawstore.transaction() as tx:
            tx.write_rows(HIST, [{"session": "2026-10-01", "ticker": "AAPL", "score": 99.0},
                                 {"session": "2026-10-05", "ticker": "AAPL", "score": 4.0}])
        assert _scores() == {**_BEFORE, ("2026-10-05", "AAPL"): 4.0}

    def test_atomic_follows_the_same_rule(self, hist):
        rawstore.atomic(lambda tx: tx.write_rows(
            HIST, [{"session": "2026-10-01", "ticker": "MSFT", "score": 99.0}]))
        assert _scores() == _BEFORE

    def test_a_history_table_without_a_key_cannot_be_written(self, rawstore_tmp_db, monkeypatch):
        monkeypatch.setitem(rawstore.MANIFEST, HIST, _spec(key=[]))
        with pytest.raises(rawstore.RawStoreError, match="key"):
            rawstore.write_rows(HIST, [{"session": "s", "ticker": "AAPL", "score": 1.0}])


# ── raw SQL: the four rewrite statements are refused too ─────────────────────
# Six of the 18 non-ASCII characters DuckDB's parser turns into a blank before it lexes, outside what it takes
# for quotes (Parser::StripUnicodeSpaces,
# https://github.com/duckdb/duckdb/blob/v1.5.6/src/parser/parser.cpp#L69-L131); test_no_character_lets_a_rewrite_through
# samples the others.
_SPACES = ["\u00a0", "\u2003", "\u200b", "\u2060", "\u3000", "\ufeff"]

_RAW_REWRITES = [
    "DELETE FROM t_history_demo WHERE ticker = 'AAPL'",
    "delete  from  t_history_demo",
    'DELETE FROM "t_history_demo" WHERE 1 = 1',
    "DELETE FROM main.t_history_demo",
    "UPDATE t_history_demo SET score = 99",
    "DROP TABLE t_history_demo",
    "DROP TABLE IF EXISTS t_history_demo",
    "TRUNCATE t_history_demo",
    "TRUNCATE TABLE t_history_demo",
    "SELECT 1; DELETE FROM t_history_demo",
    "/* tidy up */ DELETE FROM t_history_demo -- all of it",
    # names that still name the table (review of 2026-10-04, finding 1)
    "DELETE FROM raw.main.t_history_demo",
    "DELETE FROM raw.t_history_demo",
    "DELETE FROM main . t_history_demo",
    'DELETE FROM "raw"."main"."t_history_demo"',
    'DELETE\nFROM\n"main"."t_history_demo"',
    "UPDATE raw.t_history_demo SET score = 0",
    "DROP TABLE IF EXISTS raw.main.t_history_demo",
    "TRUNCATE raw.t_history_demo",
    # any case (finding 3): the error must be a RawStoreError, not a KeyError
    "DELETE FROM T_HISTORY_DEMO",
    'UPDATE "T_History_Demo" SET score = 0',
    # a list of tables, the history table not first (DuckDB 1.5.6 itself refuses a drop list; the guard
    # does not depend on that)
    "DROP TABLE watchlist, t_history_demo",
    # other statements that delete, update or recreate rows (finding 2)
    "MERGE INTO t_history_demo USING (SELECT '2026-10-01' AS session, 'AAPL' AS ticker) s "
    "ON (t_history_demo.session = s.session AND t_history_demo.ticker = s.ticker) WHEN MATCHED THEN DELETE",
    "CREATE OR REPLACE TABLE t_history_demo AS SELECT * FROM t_history_demo WHERE 1 = 0",
    "CREATE OR REPLACE TEMP TABLE t_history_demo AS SELECT 1 AS x",
    "INSERT INTO t_history_demo VALUES ('2026-10-01', 'AAPL', 42.0) "
    "ON CONFLICT (session, ticker) DO UPDATE SET score = excluded.score",
    "insert into raw.main.t_history_demo select * from t_history_demo "
    "on conflict (session, ticker) do update set score = 0",
    "MERGE INTO t_history_demo USING (SELECT '2026-10-01' AS session, 'AAPL' AS ticker, 9.0 AS score) s "
    "ON (t_history_demo.session = s.session AND t_history_demo.ticker = s.ticker) "
    "WHEN MATCHED THEN UPDATE SET score = s.score",
    # a comment that ends at the line break must not swallow the next line
    "-- tidy up\nDELETE FROM t_history_demo",
    "SELECT 'x' AS \"a\"\"\"; DELETE FROM t_history_demo",
    # literal forms the old blanker did not know (finding 5): a quote inside $$..$$ or E'..' must not
    # hide the statement that follows it
    "SELECT $$it's$$; DELETE FROM t_history_demo; SELECT 'x'",
    "SELECT $q$ it's $q$; DELETE FROM t_history_demo",
    r"SELECT E'\''; DELETE FROM t_history_demo; SELECT 'x'",
    r"SELECT e'\''; DELETE FROM t_history_demo",
    # ONLY and ONLY (..) in front of the name: DuckDB 1.5.6 runs every one of these (re-review N1)
    "UPDATE ONLY t_history_demo SET score = 0",
    "UPDATE ONLY (t_history_demo) SET score = 0",
    "DELETE FROM ONLY t_history_demo",
    "DELETE FROM ONLY (t_history_demo)",
    "DELETE FROM only main.t_history_demo",
    "TRUNCATE ONLY t_history_demo",
    "TRUNCATE ONLY (t_history_demo)",
    "TRUNCATE TABLE ONLY raw.main.t_history_demo",
    "TRUNCATE TABLE ONLY (t_history_demo)",
    "MERGE INTO ONLY t_history_demo USING (SELECT 1 AS x) s ON false WHEN NOT MATCHED BY SOURCE THEN DELETE",
    "MERGE INTO ONLY (t_history_demo) USING (SELECT 1 AS x) s ON false WHEN NOT MATCHED BY SOURCE THEN DELETE",
    # DuckDB reads every character from U+0080 up as part of a name, except the unicode spaces its parser
    # strips (next group), so these are one word before the $$ and the next statement is real (re-review N2)
    "SELECT 1 AS x\u00b7$$; DELETE FROM t_history_demo; --$$",
    "SELECT 1 AS \u00b7$$; DELETE FROM t_history_demo; --$$",
    "SELECT 1 AS x\u0085$$; DELETE FROM t_history_demo; --$$",
    "SELECT 1 AS x\u1680$$; DELETE FROM t_history_demo; --$$",
    "SELECT 1 AS x\u2028$$; DELETE FROM t_history_demo; --$$",
    "SELECT 1 AS x\u2029$$; DELETE FROM t_history_demo; --$$",
    "SELECT 1 AS x\U0001f600$$; DELETE FROM t_history_demo; --$$",
    "SELECT $\u00e9$it's$\u00e9$; DELETE FROM t_history_demo",
    "SELECT $\u00b7$it's$\u00b7$; DELETE FROM t_history_demo",
    # and these are spaces to DuckDB although str.isspace() says no, so they still split the keywords
    "DELETE\u200bFROM\u200bt_history_demo",
    "UPDATE\u2060t_history_demo SET score = 0",
    "DELETE\ufeffFROM\ufefft_history_demo",
    "DELETE\u00a0FROM\u3000t_history_demo",
    # DuckDB's unicode-space pre-pass reads these regions as quoted, so the space stays a name character and
    # no literal opens: the next statement is real (#284 third review N5, #308)
    *[f"SELECT 1 AS x$$, 2 AS y{s}$$; DELETE FROM t_history_demo; --$$" for s in _SPACES],
    *[rf"SELECT E'\'' AS a, 1 AS y{s}$$; DELETE FROM t_history_demo; --$$" for s in _SPACES],
    *[f"/* ' */ SELECT 1 AS y{s}$$; DELETE FROM t_history_demo; --$$" for s in _SPACES],
    *[f"SELECT 1 AS x$a$, 2 AS y{s}$$; UPDATE t_history_demo SET score = 0; --$$" for s in _SPACES],
    "SELECT $a\u00a0$$ x $a\u00a0$; DELETE FROM t_history_demo; --$$",
    # a carriage return ends a -- comment for DuckDB (N6)
    "SELECT 1 -- x\r; UPDATE t_history_demo SET score = 0",
    "-- note\rTRUNCATE t_history_demo",
    "-- note\rDROP TABLE t_history_demo",
    # DuckDB plans INSERT OR REPLACE as the same MERGE ... UPDATE as ON CONFLICT DO UPDATE (#308)
    "INSERT OR REPLACE INTO t_history_demo VALUES ('2026-10-01', 'AAPL', 77.0)",
    "insert or replace into main.t_history_demo select * from t_history_demo",
    # statements that run a rewrite through another statement type
    "EXPLAIN ANALYZE DELETE FROM t_history_demo",
    "PREPARE p AS DELETE FROM t_history_demo; EXECUTE p",
    "WITH s AS (SELECT 1) DELETE FROM t_history_demo",
    # the DELETE sits under the RETURNING projection in DuckDB's plan, not at its root
    "DELETE FROM t_history_demo RETURNING *",
]


class TestRawSql:
    @pytest.mark.parametrize("sql", _RAW_REWRITES)
    def test_rawstore_execute_refuses_it(self, hist, sql):
        with pytest.raises(rawstore.RawStoreError, match="history"):
            rawstore.execute(sql)
        assert _scores() == _BEFORE

    @pytest.mark.parametrize("sql", _RAW_REWRITES)
    def test_the_transaction_handle_refuses_it(self, hist, sql):
        with pytest.raises(rawstore.RawStoreError, match="history"):
            with rawstore.transaction() as tx:
                tx.execute(sql)
        assert _scores() == _BEFORE

    def test_atomic_refuses_it(self, hist):
        with pytest.raises(rawstore.RawStoreError, match="history"):
            rawstore.atomic(lambda tx: tx.execute("DELETE FROM t_history_demo"))
        assert _scores() == _BEFORE

    @pytest.mark.parametrize("read_only", [True, False])
    def test_query_refuses_it_whatever_read_only_says(self, hist, read_only):
        # every handle is opened read-write (docs/data_architecture.md), so read_only=True is no shield
        with pytest.raises(rawstore.RawStoreError, match="history"):
            rawstore.query("DELETE FROM t_history_demo", read_only=read_only)
        assert _scores() == _BEFORE

    def test_the_message_names_the_table_and_the_statement(self, hist):
        with pytest.raises(rawstore.RawStoreError) as err:
            rawstore.execute("truncate table t_history_demo")
        assert HIST in str(err.value) and "TRUNCATE" in str(err.value)

    def test_reads_of_a_history_table_are_untouched(self, hist):
        assert _scores() == _BEFORE
        assert rawstore.query("SELECT count(*) AS n FROM t_history_demo")[0]["n"] == 2
        with rawstore.transaction() as tx:
            assert tx.execute("SELECT count(*) FROM t_history_demo").fetchone()[0] == 2

    def test_other_tables_and_look_alikes_are_untouched(self, hist, monkeypatch):
        monkeypatch.setitem(rawstore.MANIFEST, HIST + "_archive", _spec(history=False))
        rawstore.write_rows(HIST + "_archive", [{"session": "s", "ticker": "AAPL", "score": 1.0}])
        rawstore.write_rows("watchlist", [{"ticker": "AAPL"}])
        rawstore.execute("DELETE FROM t_history_demo_archive")        # same prefix, not flagged
        rawstore.execute("DELETE FROM watchlist WHERE ticker = 'AAPL'")
        assert rawstore.count("watchlist") == 0 and rawstore.count(HIST + "_archive") == 0
        assert _scores() == _BEFORE

    def test_the_table_name_inside_a_literal_or_comment_is_not_a_statement(self, hist):
        rawstore.write_rows("watchlist", [{"ticker": "X"}])
        rawstore.execute("DELETE FROM watchlist WHERE ticker = 'DELETE FROM t_history_demo' "
                         "OR ticker = 'X' -- UPDATE t_history_demo SET score = 0")
        assert rawstore.count("watchlist") == 0
        assert _scores() == _BEFORE

    def test_an_upsert_clause_on_another_table_is_not_an_update_of_the_history_table(self, hist):
        rawstore.execute("INSERT INTO watchlist VALUES ('AAPL') ON CONFLICT (ticker) DO UPDATE SET ticker = 'AAPL'")
        assert rawstore.count("watchlist") == 1

    def test_the_error_names_the_table_as_the_manifest_spells_it(self, hist):
        with pytest.raises(rawstore.RawStoreError) as err:
            rawstore.execute("DELETE FROM T_HISTORY_DEMO")
        assert HIST in str(err.value)

    def test_a_mixed_case_manifest_name_is_matched_in_any_case(self, hist, monkeypatch):
        monkeypatch.setitem(rawstore.MANIFEST, "T_Mixed", {"history": True})
        with rawstore.connect() as conn:
            conn.execute('CREATE TABLE "T_Mixed" (x INT)')
            with pytest.raises(rawstore.RawStoreError) as err:
                rawstore._refuse_history_sql(conn, "delete from t_mixed")
        assert "T_Mixed" in str(err.value) and "could not be checked" not in str(err.value)

    @pytest.mark.parametrize("sql", [
        # another table's name (a doubled quote stays inside the identifier) that does not exist
        'DELETE FROM "t_history_demo""x"',
        # DuckDB plans every statement before any runs, so a table the text creates first is unknown
        "CREATE TABLE scratch AS SELECT 9.0 AS s; INSERT INTO t_history_demo SELECT '2026-10-09', 'X', s FROM scratch",
        # DuckDB cannot parse it
        "SELECT 1 FROM t_history_demo WHERE",
    ])
    def test_text_duckdb_cannot_read_is_refused_when_it_names_a_history_table(self, hist, sql):
        """Fail closed (#308): text that spells a history table's name and that DuckDB cannot parse or plan
        could do anything, so the guard refuses it rather than let DuckDB run the part it can."""
        with pytest.raises(rawstore.RawStoreError, match="history") as err:
            rawstore.execute(sql)
        assert HIST in str(err.value) and "could not be checked" in str(err.value)
        assert _scores() == _BEFORE

    def test_the_same_unreadable_text_without_a_history_name_is_left_to_duckdb(self, hist):
        with pytest.raises(duckdb.CatalogException):
            rawstore.execute('DELETE FROM "watchlist""x"')

    @pytest.mark.parametrize("node, refused", [
        ({"type": "LOGICAL_DELETE", "table_info": {}}, True),                          # no target name
        ({"type": "LOGICAL_INSERT", "action_type": "UPDATE", "table_info": {"table": HIST}}, True),
        ({"type": "LOGICAL_MERGE_INTO", "actions": [], "table_info": {"table": HIST}}, True),
        ({"type": "LOGICAL_MERGE_INTO", "actions": [["WHEN_MATCHED", ["MERGE_UPDATE"]]], "table_info": {"table": HIST}},
         True),                                                                       # actions in another shape
        ({"type": "LOGICAL_DROP", "info": HIST}, True),                               # info in another shape
        ({"type": "LOGICAL_MERGE_INTO", "table_info": {"table": HIST},                # one branch readable
          "actions": [{"key": "WHEN_NOT_MATCHED", "value": [{"action_type": "MERGE_INSERT"}]},
                      ["WHEN_MATCHED", ["MERGE_UPDATE"]]]}, True),
        ({"type": "LOGICAL_INSERT", "action_type": "THROW", "table_info": {"table": HIST}}, False),
    ], ids=["write-without-target", "insert-conflict-update", "merge-without-actions", "merge-actions-as-lists",
            "drop-info-as-text", "merge-one-branch-unreadable", "plain-insert"])
    def test_a_plan_shape_the_guard_does_not_know_is_refused(self, hist, monkeypatch, node, refused):
        """Another DuckDB version may serialize its plan differently: what the guard cannot read as safe, it
        refuses (#308). The planner call is swapped for one that returns this plan."""
        monkeypatch.setattr(rawstore, "_PLANNER_OK", True)   # exercise the walker on this canned plan
        canned = json.dumps({"error": False, "plans": [node]}).replace("'", "''")
        monkeypatch.setattr(rawstore, "_PLAN_SQL", f"SELECT '{canned}' || substr(?, 1, 0)")
        sql = "INSERT INTO t_history_demo VALUES ('2026-10-09', 'X', 1.0)"
        if refused:
            with pytest.raises(rawstore.RawStoreError, match="history"):
                rawstore.execute(sql)
        else:
            rawstore.execute(sql)
        assert _scores()[("2026-10-01", "AAPL")] == 1.0

    def test_the_guard_fails_closed_when_duckdb_cannot_plan_at_all(self, hist, monkeypatch):
        """If the installed DuckDB lacks the planner call, a rewrite and an append are both refused."""
        monkeypatch.setattr(rawstore, "_PLAN_SQL", "SELECT no_such_planner(?)")
        for sql in ("DELETE FROM t_history_demo", "INSERT INTO t_history_demo VALUES ('2026-10-09', 'X', 1.0)"):
            with pytest.raises(rawstore.RawStoreError, match="could not be checked"):
                rawstore.execute(sql)
        assert _scores() == _BEFORE

    @pytest.mark.parametrize("sql", [
        "INSERT INTO t_history_demo VALUES ('2026-10-09', 'AAPL', 5.0)",
        "INSERT INTO t_history_demo VALUES ('2026-10-01', 'AAPL', 99.0) ON CONFLICT (session, ticker) DO NOTHING",
        "SELECT $$DELETE FROM t_history_demo$$ AS note",
        r"SELECT E'DELETE FROM t_history_demo\'s rows' AS note",
        "SELECT 'x' AS note /* a /* nested */ DELETE FROM t_history_demo */",
        "SELECT 1 AS note -- DELETE FROM t_history_demo",
        "SELECT 1 AS x\u00b7$$ /* DELETE FROM t_history_demo */",
        "SELECT\u00a0$$DELETE FROM t_history_demo$$ AS note",
        "MERGE INTO t_history_demo USING (SELECT '2026-10-09' AS session, 'AAPL' AS ticker, 5.0 AS score) s "
        "ON (t_history_demo.session = s.session AND t_history_demo.ticker = s.ticker) "
        "WHEN NOT MATCHED THEN INSERT VALUES (s.session, s.ticker, s.score)",
        'SELECT "t_history_demo".* FROM "t_history_demo"',
        "DESCRIBE t_history_demo",
        "PRAGMA table_info('t_history_demo')",
        "INSERT OR IGNORE INTO t_history_demo VALUES ('2026-10-01', 'AAPL', 99.0)",
        "CREATE TABLE copy_of_history AS SELECT * FROM t_history_demo",
        "CREATE TABLE IF NOT EXISTS t_history_demo (session TEXT, ticker TEXT, score DOUBLE)",
        "INSERT INTO t_history_demo SELECT '2026-10-09', ticker, score FROM t_history_demo",
        "UPDATE watchlist SET ticker = 'X' WHERE ticker IN (SELECT ticker FROM t_history_demo)",
        "BEGIN; SELECT count(*) FROM t_history_demo; COMMIT",
    ])
    def test_adding_to_and_reading_a_history_table_still_works(self, hist, sql):
        rawstore.execute(sql)
        assert _scores()[("2026-10-01", "AAPL")] == 1.0 and _scores()[("2026-10-01", "MSFT")] == 2.0

    def test_a_parameterised_append_still_works(self, hist):
        rawstore.execute("INSERT INTO t_history_demo VALUES (?, ?, ?)", ["2026-10-09", "AAPL", 5.0])
        assert _scores()[("2026-10-09", "AAPL")] == 5.0 and _scores()[("2026-10-01", "AAPL")] == 1.0

    def test_alter_is_the_documented_exception(self, hist):
        """Named in the docs as outside the guard. If it is ever blocked, move this case to the refusals."""
        rawstore.execute("ALTER TABLE t_history_demo ALTER score SET DATA TYPE DOUBLE USING 0")
        assert _scores()[("2026-10-01", "AAPL")] == 0.0

    def test_an_empty_update_columns_still_raises_on_a_history_table(self, hist):
        with pytest.raises(rawstore.RawStoreError, match="history"):
            rawstore.update_columns(hist, "ticker", [], ["score"])


# ── whatever the character, no rewrite gets through ─────────────────────────
# The guard reads no characters itself (#308): DuckDB parses the text, so these are black-box. DuckDB runs
# each statement or refuses it, and either way the history rows stay. The code points sample DuckDB's
# classes (re-reviews N2, N5, N6): ASCII spaces and line ends, the 18 spaces of its pre-pass, name characters
# from U+0080 up, and the edges between them.
_SAMPLE_CODE_POINTS = [
    0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x1c, 0x1f, 0x20, 0x80, 0x85, 0x9f, 0xa0, 0xa1, 0xb7, 0xe9, 0x1680, 0x1fff,
    0x2000, 0x2003, 0x200a, 0x200b, 0x200c, 0x2028, 0x2029, 0x202e, 0x202f, 0x2030, 0x205e, 0x205f, 0x2060,
    0x2061, 0x3000, 0x3001, 0xfefe, 0xfeff, 0xff00, 0x1f600, 0x10ffff,
]


def _kept() -> bool:
    """The two rows written by the fixture are still there, unchanged (rows added since do not matter)."""
    got = _scores()
    return {k: got.get(k) for k in _BEFORE} == _BEFORE


@pytest.mark.parametrize("cp", _SAMPLE_CODE_POINTS, ids=lambda cp: f"U+{cp:04X}")
def test_no_character_lets_a_rewrite_through(hist, cp):
    c = chr(cp)
    for sql in (f"DELETE{c}FROM{c}t_history_demo",
                f"SELECT 1 AS x{c}$$; DELETE FROM t_history_demo; --$$",
                f"SELECT 1 AS x$$, 2 AS y{c}$$; DELETE FROM t_history_demo; --$$",
                rf"SELECT E'\'' AS a, 1 AS y{c}$$; UPDATE t_history_demo SET score = 0; --$$",
                f"SELECT $a{c}$$ x $a{c}$; DELETE FROM t_history_demo; --$$",
                f"SELECT 1 -- x{c}; UPDATE t_history_demo SET score = 0"):
        with contextlib.suppress(rawstore.RawStoreError, duckdb.Error):
            rawstore.execute(sql)
        assert _kept(), repr(sql)


# ── differential fuzz: DuckDB runs what the guard lets through ────────────────
# Random text from the classes every review round used against the hand-written scanner (quotes, E'' escapes,
# dollar tags, comments, CR/LF, names ending in $, non-ASCII spaces and name characters), around one rewrite
# or one append. Ground truth is DuckDB itself: whatever the guard lets through runs, and the rows must stay.
# scripts/research/308_sql_guard_parser.py runs the same idea at 60,000 cases.
_FUZZ_CHARS = ["\u00a0", "\u2003", "\u200b", "\u2060", "\u3000", "\ufeff", "\u00b7", "\u0085", "\u2028", "\u00e9",
               " ", "\t", "\r", "\n", "x", "$", "'", '"', "\\"]
_FUZZ_OPENERS = ["SELECT 1 AS x{c}$$, 2 AS y{s}$$", "SELECT E'\\'' AS a, 1 AS y{s}$$", "/* ' */ SELECT 1 AS y{s}$$",
                 "SELECT 1 AS x$a$, 2 AS y{s}$$", "SELECT $a{c}$$ x $a{c}$", "SELECT 1 -- x{c}", "-- note{c}",
                 "SELECT 1 AS x{c}$$", "SELECT $$it's{c}$$", "SELECT $q$ {c} $q$", "SELECT e'{c}\\''",
                 "SELECT '{c}'", 'SELECT 1 AS "{c}"', "/* {c} /* */ {s} */ SELECT 1", "SELECT 1{c}"]
_FUZZ_SEPS = [";", "; ", ";\n", "\n", "\r", " ", "\u00a0", "\u2003", "*/;", "$$;", "';", '";', "--\r"]
_FUZZ_REWRITES = [
    "DELETE FROM t_history_demo", "UPDATE t_history_demo SET score = 0", "TRUNCATE t_history_demo",
    "DROP TABLE t_history_demo", "DELETE FROM ONLY main.t_history_demo",
    "INSERT OR REPLACE INTO t_history_demo VALUES ('2026-10-01', 'AAPL', 9.0)",
    "INSERT INTO t_history_demo VALUES ('2026-10-01', 'AAPL', 9.0) ON CONFLICT DO UPDATE SET score = 9",
    "CREATE OR REPLACE TABLE t_history_demo AS SELECT 1 AS x",
    "MERGE INTO t_history_demo USING (SELECT 'AAPL' AS ticker) s ON t_history_demo.ticker = s.ticker "
    "WHEN MATCHED THEN DELETE",
]
_FUZZ_APPENDS = ["INSERT INTO t_history_demo VALUES ('2026-10-09', 'X', 1.0)", "SELECT * FROM t_history_demo",
                 "INSERT INTO t_history_demo VALUES ('2026-10-01', 'AAPL', 9.0) ON CONFLICT DO NOTHING"]


def _fuzz_case(rng: random.Random) -> str:
    pre = rng.choice(_FUZZ_OPENERS).format(c=rng.choice(_FUZZ_CHARS), s=rng.choice(_FUZZ_CHARS))
    body = rng.choice(_FUZZ_APPENDS if rng.random() < 0.3 else _FUZZ_REWRITES)
    tail = rng.choice(["", " --$$", " --$a$", "; SELECT 'x'", " -- '", "'", "*/"])
    return pre + rng.choice(_FUZZ_SEPS) + body + rng.choice(_FUZZ_SEPS) + tail


def test_no_generated_statement_rewrites_history(hist):
    """Guard then DuckDB on one handle, as ``transaction().execute`` runs them."""
    rng = random.Random(308)
    outcomes = {"refused": 0, "ran": 0, "duckdb error": 0}
    with rawstore.connect() as conn:
        for _ in range(400):
            sql = _fuzz_case(rng)
            try:
                rawstore._refuse_history_sql(conn, sql)
            except rawstore.RawStoreError:
                outcomes["refused"] += 1
                continue
            try:
                conn.execute(sql)
                outcomes["ran"] += 1
            except duckdb.Error:
                outcomes["duckdb error"] += 1
            rows = {(s, t): v for s, t, v in conn.execute("SELECT session, ticker, score FROM t_history_demo").fetchall()}
            assert {k: rows.get(k) for k in _BEFORE} == _BEFORE, repr(sql)
    # both outcomes are reached, so a guard that refuses everything or nothing cannot pass
    assert outcomes["refused"] >= 20 and outcomes["ran"] >= 20, outcomes


# ── the manifest itself ──────────────────────────────────────────────────────
def test_every_flagged_table_declares_a_primary_key_over_its_key_columns():
    """First-write-wins is ``ON CONFLICT (key) DO NOTHING``, which needs the key to be a PRIMARY KEY."""
    for name, spec in rawstore.MANIFEST.items():
        if not spec.get("history"):
            continue
        key = spec.get("key") or []
        assert key, f"{name}: a history table needs a key"
        declared = {c.strip().strip('"') for c in key}
        pk_clause = next((c for c in spec["columns"] if c.strip().upper().startswith("PRIMARY KEY")), None)
        if pk_clause:
            in_pk = {c.strip().strip('"') for c in pk_clause[pk_clause.index("(") + 1:pk_clause.rindex(")")].split(",")}
        else:
            in_pk = {c.split()[0].strip('"') for c in spec["columns"] if "PRIMARY KEY" in c.upper()}
        assert in_pk == declared, f"{name}: key {sorted(declared)} is not its PRIMARY KEY {sorted(in_pk)}"


# ── #308 review fixes: typed parameter, fail-closed plan shape, cast hint ────
def _swap_plan(monkeypatch, plan_obj):
    """Feed ``_history_rewrite`` a canned ``json_serialize_plan`` result (the file's established pattern)."""
    canned = json.dumps(plan_obj).replace("'", "''")
    monkeypatch.setattr(rawstore, "_PLAN_SQL", f"SELECT '{canned}' || substr(?, 1, 0)")


class TestTypedPlanParameter:
    def test_the_plan_query_casts_its_parameter(self):
        """A2: the untyped ``?`` raised "first argument must be a VARCHAR" on DuckDB 1.0 and 1.1."""
        assert "?::VARCHAR" in rawstore._PLAN_SQL

    def test_a_parameterised_append_to_a_flagged_table_still_runs(self, hist):
        rawstore.execute("INSERT INTO t_history_demo VALUES (?, ?, ?)", ["2026-10-09", "AAPL", 5.0])
        assert _scores()[("2026-10-09", "AAPL")] == 5.0 and _scores()[("2026-10-01", "AAPL")] == 1.0


class TestFailClosedPlanShape:
    def test_an_unknown_plan_operator_is_refused(self, hist, monkeypatch):
        """A1: a node whose ``type`` the walker does not know is refused, not silently allowed."""
        _swap_plan(monkeypatch, {"error": False, "plans": [{"type": "LOGICAL_DELETE_V2", "table_info": {"table": HIST}}]})
        with pytest.raises(rawstore.RawStoreError, match="history"):
            rawstore.execute("DELETE FROM t_history_demo")
        assert _scores() == _BEFORE

    def test_a_plan_with_no_statement_list_is_refused(self, hist, monkeypatch):
        """A1(a): ``"plans": []`` (or no list) is refused rather than read as nothing to do."""
        _swap_plan(monkeypatch, {"error": False, "plans": []})
        with pytest.raises(rawstore.RawStoreError, match="history"):
            rawstore.execute("DELETE FROM t_history_demo")
        assert _scores() == _BEFORE

    def test_an_empty_statement_list_is_refused_even_when_the_canary_passed(self, hist, monkeypatch):
        """A1(a) on its own: the canary passing does not make ``"plans": []`` read as nothing to rewrite."""
        monkeypatch.setattr(rawstore, "_PLANNER_OK", True)
        _swap_plan(monkeypatch, {"error": False, "plans": []})
        with pytest.raises(rawstore.RawStoreError, match="history"):
            rawstore.execute("DELETE FROM t_history_demo")
        assert _scores() == _BEFORE

    def test_the_canary_reads_this_duckdbs_plans(self, monkeypatch):
        """A1(b): with the real planner call, every rewrite shape is understood, once per process."""
        assert rawstore._planner_understood() is True
        monkeypatch.setattr(rawstore, "_PLAN_SQL", "SELECT 'not json' || substr(?, 1, 0)")
        assert rawstore._planner_understood() is True     # memoised: the planner is not asked again


class TestSelectListParameter:
    """A3: an untyped ``?`` in an ``INSERT ... SELECT`` select list makes ``json_serialize_plan`` fail."""

    def _source(self):
        rawstore.execute("CREATE TABLE t_history_src (ticker TEXT, score DOUBLE)")
        rawstore.execute("INSERT INTO t_history_src VALUES ('NVDA', 5.0)")

    def test_a_cast_parameter_in_the_select_list_plans(self, hist):
        self._source()
        with rawstore.transaction() as tx:
            tx.execute("INSERT INTO t_history_demo (session, ticker, score) "
                       "SELECT ?::VARCHAR, ticker, score FROM t_history_src", ["2026-10-09"])
        assert _scores()[("2026-10-09", "NVDA")] == 5.0

    def test_an_uncast_parameter_in_the_select_list_is_refused_with_a_hint(self, hist):
        self._source()
        with pytest.raises(rawstore.RawStoreError, match=r"\?::VARCHAR"):
            with rawstore.transaction() as tx:
                tx.execute("INSERT INTO t_history_demo (session, ticker, score) "
                           "SELECT ?, ticker, score FROM t_history_src", ["2026-10-09"])
        assert _scores() == _BEFORE


def test_the_doc_says_a_select_list_parameter_must_be_cast():
    doc = (Path(__file__).resolve().parents[2] / "docs" / "data_architecture.md").read_text(encoding="utf-8")
    para = doc.split("History tables refuse rewrites (#286)", 1)[1].split("\n- ", 1)[0]
    assert "?::VARCHAR" in para


# ── #308 review round 2: the canary covers every rewriting shape ────────────
_CANARY_SHAPES = ("DELETE FROM", "TRUNCATE", "UPDATE", "DROP TABLE", "CREATE OR REPLACE TABLE",
                  "INSERT OR REPLACE", "INSERT INTO")   # the INSERT INTO one is ON CONFLICT DO UPDATE


@pytest.mark.parametrize("shape", _CANARY_SHAPES)
def test_the_canary_plans_each_rewriting_shape(shape):
    """Dropping a shape from ``_CANARY_SQL`` would let a DuckDB that serializes that shape differently pass the
    canary; each shape must stay in the list."""
    assert sum(sql.startswith(shape + " ") for sql in rawstore._CANARY_SQL) == 1
    assert any("ON CONFLICT" in sql and "DO UPDATE" in sql for sql in rawstore._CANARY_SQL)


@pytest.mark.parametrize("template", rawstore._CANARY_SQL)
def test_each_canary_statement_plans_as_a_rewrite_of_the_scratch_table(template):
    with duckdb.connect(":memory:") as conn:
        conn.execute("CREATE TABLE t_planner_canary (id INTEGER PRIMARY KEY, v VARCHAR)")
        plan = json.loads(conn.execute(rawstore._PLAN_SQL, [template.format(t="t_planner_canary")]).fetchone()[0])
    assert "t_planner_canary" in {(t or "").casefold() for _, t in rawstore._plan_rewrites(plan.get("plans"))}
