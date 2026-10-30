"""``rcdata.scaffold``: create a serving file with its tables and no rows.

Four of the seven serving files have never been built on the server, and "never created" looks exactly like
"created and empty" to a board and to the Settings screen: both answer no rows. A file that exists with its tables
and zero rows lets the screen say "built, nothing in it yet" instead of guessing, and lets a reader open the file
at all.

Creating one is a write to the live store, so the command refuses to touch a file that already exists: it only
ever fills a gap, never replaces data.
"""
from __future__ import annotations

import duckdb
import pytest

from RevenueChaser.rcdata import scaffold, serving


def test_every_serving_kind_has_a_declared_schema():
    """A kind with no schema here cannot be created empty, and the screen would be guessing about it."""
    assert set(scaffold.SCHEMA_OF_KIND) == set(serving.KINDS)
    for kind, schemas in scaffold.SCHEMA_OF_KIND.items():
        assert schemas, f"{kind}: no tables"
        for table, columns in schemas.items():
            assert columns and all(len(c) == 2 for c in columns), f"{kind}.{table}: not (name, type) pairs"


def test_every_served_table_a_reader_knows_is_in_the_scaffolds_schema():
    """``create_empty`` exists to close the gap between "never built" and "built and empty", so a served table the
    scaffold does not declare reopens it — and the panel's file card, which reads the same map, never lists it.
    The assertion below is a subset, so it passes with a table missing; this one is the equality."""
    from RevenueChaser.rcdata import read
    declared = {t for tables in scaffold.SCHEMA_OF_KIND.values() for t in tables}
    assert set(read.KIND_OF_TABLE) <= declared, sorted(set(read.KIND_OF_TABLE) - declared)


@pytest.mark.parametrize("kind", sorted(serving.KINDS))
def test_an_empty_file_holds_every_table_of_its_kind_with_no_rows(kind):
    made = scaffold.create_empty(kind)
    assert made == serving.live_path(kind) and made.exists()
    con = duckdb.connect(str(made), read_only=True)
    try:
        have = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
        assert set(scaffold.SCHEMA_OF_KIND[kind]) <= have
        for table in scaffold.SCHEMA_OF_KIND[kind]:
            assert con.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        version = con.execute(f"SELECT version FROM {serving.VERSION_TABLE}").fetchone()[0]
        assert version == serving.SERVING_SCHEMA_VERSION[kind]
    finally:
        con.close()


def test_the_columns_are_the_manifests_own():
    scaffold.create_empty("prices")
    con = duckdb.connect(str(serving.live_path("prices")), read_only=True)
    try:
        cols = [r[0] for r in con.execute("DESCRIBE price_bar_daily").fetchall()]
    finally:
        con.close()
    assert cols == [c for c, _ in scaffold.SCHEMA_OF_KIND["prices"]["price_bar_daily"]]


def test_an_existing_file_is_never_replaced():
    scaffold.create_empty("prices")
    before = serving.live_path("prices").stat().st_mtime_ns
    with pytest.raises(scaffold.AlreadyBuilt):
        scaffold.create_empty("prices")
    assert serving.live_path("prices").stat().st_mtime_ns == before


def test_create_missing_fills_only_the_gaps():
    scaffold.create_empty("prices")
    made = scaffold.create_missing()
    assert "prices" not in made and set(made) == set(serving.KINDS) - {"prices"}
    assert all(serving.live_path(k).exists() for k in serving.KINDS)
    assert scaffold.create_missing() == {}


def test_a_read_of_an_empty_file_is_no_rows_never_an_error():
    from RevenueChaser.rcdata import read
    scaffold.create_empty("prices")
    assert read.bars(["sec:1"]) == []
    summary = read.table_summary("price_bar_daily", day_column="session_date", key_column="security_id")
    assert summary["row_count"] == 0 and summary["newest_day"] is None


# ── The command avo runs on the server ──────────────────────────────────────────────────────────────────────────

@pytest.fixture
def backup_taken(monkeypatch):
    """The verified backup, stubbed: a test has no backup folder, and what is under test here is the scaffold."""
    from RevenueChaser.rcdata import backup
    monkeypatch.setattr(backup, "run_backup", lambda: {"ops.sqlite": "copied"})
    return backup


def test_the_command_reports_what_it_created_and_what_it_left_alone(capsys, backup_taken):
    backup = backup_taken
    scaffold.create_empty("prices")
    assert backup.main(["create-empty-stores", "--apply"]) == 0
    out = capsys.readouterr().out
    assert "prices: already built" in out
    for kind in sorted(set(serving.KINDS) - {"prices"}):
        assert f"{kind}: created" in out
    assert all(serving.live_path(k).exists() for k in serving.KINDS)


def test_the_command_says_so_when_there_is_nothing_to_do(capsys):
    scaffold.create_missing()
    from RevenueChaser.rcdata import backup
    assert backup.main(["create-empty-stores"]) == 0
    assert "nothing to create" in capsys.readouterr().out


def test_the_ownership_file_holds_every_providers_table_too():
    """The 13F serve module names four tables; the other six are one per provider module, and a reader of one of
    those would raise on a file built from the four alone."""
    tables = set(scaffold.SCHEMA_OF_KIND["ownership"])
    assert {"finra_short_interest", "ibkr_shortable", "regsho_threshold", "sec_ftd", "insider_transactions",
            "beneficial_ownership"} <= tables
    assert "ticker" in {c for c, _ in scaffold.SCHEMA_OF_KIND["ownership"]["finra_short_interest"]}


def test_every_table_a_need_names_is_in_some_kinds_schema():
    """A need over a table no empty file would hold is a need no screen can ever show as 'built, empty'."""
    from RevenueChaser.rcdata import api
    known = {t for tables in scaffold.SCHEMA_OF_KIND.values() for t in tables}
    missing = sorted({n.table for n in api.catalog.NEEDS.values()} - known)
    assert missing == [], f"needs over tables no serving file declares: {missing}"


# ── The review's I3: a write to the live store needs its three protections ──

def test_without_apply_the_command_only_says_what_it_would_do(capsys):
    """CLAUDE.md: a write to the live store runs behind a verified backup, with an idle queue and a named rollback.
    The command's own comment promised all three and then wrote straight away. The default is a dry run now, as
    ``clean-scratch`` has been all along for far less."""
    from RevenueChaser.rcdata import backup
    assert backup.main(["create-empty-stores"]) == 0
    out = capsys.readouterr().out
    assert "would create" in out and "--apply" in out
    assert not any(serving.live_path(k).exists() for k in serving.KINDS), "a dry run wrote to the store"


def test_apply_prints_the_rollback_for_every_file_it_creates(capsys, backup_taken):
    assert backup_taken.main(["create-empty-stores", "--apply"]) == 0
    out = capsys.readouterr().out
    assert "rollback:" in out
    for kind in serving.KINDS:
        assert str(serving.live_path(kind)) in out, f"{kind}: no rollback names the file it created"


def test_it_refuses_while_the_builder_has_work_queued(capsys, monkeypatch, backup_taken):
    """An idle queue is one of the three: the builder is the serving files' writer, and a file appearing under a
    running build is the one case the swap guard would have to catch."""
    monkeypatch.setattr(backup_taken, "_queue_is_idle", lambda: (False, "1 queued, 0 running"))
    assert backup_taken.main(["create-empty-stores", "--apply"]) == 2
    assert "queue" in capsys.readouterr().out.lower()
    assert not any(serving.live_path(k).exists() for k in serving.KINDS)
