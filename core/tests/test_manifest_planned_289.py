"""#289 (data backbone review, finding B15): a MANIFEST table with no writer is marked.

The first store call creates every MANIFEST table, so a table nothing ever writes reads in the store as an
empty table with 0 rows — indistinguishable from a table whose writer broke. Two keys, same shape, both
data like ``history_mode``: rawstore does not read either, so neither changes DDL or a write. They differ
in the word a future listing prints.

- ``"planned": True`` — a writer is still to come.
- ``"retired": True`` — the only writer is in the past.

Nothing renders either flag yet (the Settings inventory lists its own dataset rows, and none of these
tables is among them).

``PLANNED`` and ``RETIRED`` below are the thing to update when a writer lands, or when a retired table
leaves the manifest: drop the name from the set and drop the key from its MANIFEST entry in the same
commit. They are explicit sets, not a grep, because a grep for a table name in the tree cannot tell a
writer from a comment, a doc line or a test.
"""
from __future__ import annotations

import pytest

from RevenueChaser.core import rawstore

# Verified against the production tree on 2026-10-22 (#289), tests / module_testing / conftest.py /
# core/rawstore.py excluded:
#   planned — a writer is still to come
#   catalyst_events      writers arrive with #186-#192 (docs/data_architecture.md, Stage 2)
#   orphan_designations  the OOPD writer is #190's, not in the tree
#   orphan_lookups       the outcome table of that same #190 lookup
#   sec13f_filers        never named in production code; docs/data_architecture.md: G1 (#292), manifest only
#   edgar_companyfacts   same: never named in production code; G1 (#292), manifest only
#   retired — the only writer is in the past
#   leaps_daily_scores     only scripts/migrate_rawdata_to_duckdb.py, the legacy one-time import
#   sec13f_edgar_backfill  same, and the backfill itself is retired (leaves the manifest at the next bump)
#   sec13f_ticker_names    named only by that same legacy import (scripts/migrate_rawdata_to_duckdb.py)
#   sec_company_tickers    #301 D21: `sec_exchange_file` replaced it; its readers now read the file's rows
# NOT marked, though the issue's list named them — each has a writer since: price_bars_daily
# (core/prices.py), forecast_ledger (core/ledger.py) and job_runs (app/jobs.py, core/automations.py).
# migration_manifest is legacy-import-only in that script, but the store writes it itself, so it stays unmarked.
PLANNED = frozenset({
    "catalyst_events",
    "orphan_designations",
    "orphan_lookups",
    "sec13f_filers",
    "edgar_companyfacts",
})
RETIRED = frozenset({
    "leaps_daily_scores",
    "sec13f_edgar_backfill",
    "sec13f_ticker_names",
    "sec_company_tickers",
})
# An empty table is expected for either word. The invariant tests run over this union.
WRITERLESS = PLANNED | RETIRED


def _marked(key: str) -> set[str]:
    return {name for name, spec in rawstore.MANIFEST.items() if spec.get(key) is True}


def test_only_the_tables_without_a_writer_are_marked_planned():
    """planned or retired is on exactly the tables that have no writer, and on nothing else."""
    flagged = _marked("planned") | _marked("retired")
    assert flagged == set(WRITERLESS), (
        f"marked but has a writer (drop the flag from its MANIFEST entry): {sorted(flagged - set(WRITERLESS))}; "
        f"has no writer but is not marked (add it): {sorted(set(WRITERLESS) - flagged)}")


def test_retired_marks_the_tables_whose_only_writer_is_in_the_past():
    """``retired`` is its own word: on the past-writer tables, not on a writer still to come."""
    assert not (PLANNED & RETIRED), f"a table is one word, not both: {sorted(PLANNED & RETIRED)}"
    assert _marked("retired") == set(RETIRED), (
        f"marked retired but a writer is still to come, or it has one: {sorted(_marked('retired') - set(RETIRED))}; "
        f"only writer is in the past but not marked retired: {sorted(set(RETIRED) - _marked('retired'))}")
    assert _marked("planned") == set(PLANNED), (
        f"marked planned but the writer is not still to come: {sorted(_marked('planned') - set(PLANNED))}; "
        f"writer still to come but not marked planned: {sorted(set(PLANNED) - _marked('planned'))}")
    both = {name for name, spec in rawstore.MANIFEST.items()
            if spec.get("planned") is True and spec.get("retired") is True}
    assert not both, f"a table carries one word, not both: {sorted(both)}"


def test_planned_is_a_true_flag_and_never_a_half_set_one():
    """One spelling each: ``True`` on the marked entries, the key absent everywhere else. Never a string."""
    bad = {}
    for key in ("planned", "retired"):
        bad.update({f"{name}.{key}": spec[key] for name, spec in rawstore.MANIFEST.items()
                    if key in spec and spec[key] is not True})
    assert not bad, f"planned and retired must be True or absent: {bad}"


# A fresh store's columns, read from information_schema before the flag was added: the flag must not change
# the DDL, so these stay exactly as the create path made them. The three added 2026-10-22 were read the same
# way, off a throwaway store, before their flags existed.
FROZEN_DDL = {
    "catalyst_events": {
        "ticker": "VARCHAR", "event_date": "DATE", "date_precision": "VARCHAR", "kind": "VARCHAR",
        "title": "VARCHAR", "source": "VARCHAR", "source_event_id": "VARCHAR", "source_url": "VARCHAR",
        "confidence": "VARCHAR", "nct_id": "VARCHAR", "fetched_at": "DOUBLE",
    },
    "orphan_designations": {
        "ticker": "VARCHAR", "generic_name": "VARCHAR", "designation": "VARCHAR", "date_designated": "DATE",
        "designation_status": "VARCHAR", "approval_status": "VARCHAR", "marketing_approval_date": "DATE",
        "trade_name": "VARCHAR", "approvals": "JSON", "sponsor": "VARCHAR", "fetched_at": "DOUBLE",
    },
    "orphan_lookups": {
        "ticker": "VARCHAR", "session_date": "DATE", "sec_name": "VARCHAR", "query": "VARCHAR", "match": "VARCHAR",
        "provider_status": "VARCHAR", "n_rows": "INTEGER", "candidates": "JSON", "error": "VARCHAR",
        "source": "VARCHAR", "fetched_at": "DOUBLE", "run_id": "VARCHAR", "run_at": "DOUBLE",
    },
    "leaps_daily_scores": {
        "ticker": "VARCHAR", "date": "VARCHAR", "frequency": "VARCHAR", "notional": "DOUBLE",
        "concentration": "DOUBLE", "concentration_mult": "DOUBLE", "final_score": "DOUBLE",
    },
    "sec13f_edgar_backfill": {
        "cik": "VARCHAR", "period": "VARCHAR", "accession": "VARCHAR", "fetched_at": "VARCHAR",
    },
    "sec13f_ticker_names": {
        "ticker": "VARCHAR", "entry": "JSON",
    },
    # #301 D21: retired (sec_exchange_file replaced its writer); the rows stay, so the DDL must not move.
    "sec_company_tickers": {
        "ticker": "VARCHAR", "cik": "VARCHAR", "name": "VARCHAR",
    },
    "sec13f_filers": {
        "accession": "VARCHAR", "cik": "VARCHAR", "period": "VARCHAR", "name": "VARCHAR",
        "source": "VARCHAR", "fetched_at": "DOUBLE", "run_id": "VARCHAR", "run_at": "DOUBLE",
    },
    "edgar_companyfacts": {
        "cik": "VARCHAR", "taxonomy": "VARCHAR", "concept": "VARCHAR", "unit": "VARCHAR",
        "period_start": "VARCHAR", "period_end": "DATE", "accn": "VARCHAR", "value": "DOUBLE",
        "form": "VARCHAR", "fy": "INTEGER", "fp": "VARCHAR", "filed": "DATE", "source": "VARCHAR",
        "fetched_at": "DOUBLE", "run_id": "VARCHAR", "run_at": "DOUBLE",
    },
}


@pytest.mark.parametrize("name", sorted(WRITERLESS))
def test_the_flag_changes_no_ddl(name, rawstore_tmp_db):
    """A fresh store still creates the table, with the same columns and types, as it did without the flag."""
    assert name in FROZEN_DDL, f"{name} is writer-less but its DDL is not pinned here"
    with rawstore.connect(db=rawstore._db_of(name)) as conn:
        got = dict(conn.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'main' AND table_name = ? ORDER BY ordinal_position", [name]).fetchall())
    assert got == FROZEN_DDL[name], f"the flag changed {name}'s DDL"
