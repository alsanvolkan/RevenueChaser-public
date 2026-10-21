"""#289 (data backbone review, finding B15): a MANIFEST table that has no writer is marked ``"planned": True``.

The first store call creates every MANIFEST table, so a table nothing ever writes reads in the store as an
empty table with 0 rows — indistinguishable from a table whose writer broke. The flag is data, like
``history_mode``: rawstore does not read it, so it changes no DDL and no write. Nothing renders it yet (the
Settings inventory lists its own dataset rows, and none of these tables is among them); a listing added there
reads the flag to say "planned" instead of "0 rows".

``PLANNED`` below is the thing to update when a writer lands: drop the name from the set and drop
``"planned": True`` from its MANIFEST entry in the same commit. It is an explicit set, not a grep, because a
grep for a table name in the tree cannot tell a writer from a comment, a doc line or a test.
"""
from __future__ import annotations

import pytest

from RevenueChaser.core import rawstore

# Verified against the tree on 2026-10-21 (#289): nothing outside the tests writes these five.
#   catalyst_events      writers arrive with #186-#192 (docs/data_architecture.md, Stage 2)
#   orphan_designations  the OOPD writer is #190's, not in the tree
#   orphan_lookups       the outcome table of that same #190 lookup
#   leaps_daily_scores   only scripts/migrate_rawdata_to_duckdb.py, the legacy one-time import, writes it
#   sec13f_edgar_backfill  same: the legacy import only; the backfill itself is retired
# NOT planned, though the issue's list named them — each has a writer since: price_bars_daily
# (core/prices.py), forecast_ledger (core/ledger.py) and job_runs (app/jobs.py, core/automations.py).
PLANNED = frozenset({
    "catalyst_events",
    "orphan_designations",
    "orphan_lookups",
    "leaps_daily_scores",
    "sec13f_edgar_backfill",
})


def test_only_the_tables_without_a_writer_are_marked_planned():
    """The flag says "no writer yet", so it is on exactly the tables that have none, and on nothing else."""
    flagged = {name for name, spec in rawstore.MANIFEST.items() if spec.get("planned") is True}
    assert flagged == set(PLANNED), (
        f"marked planned but has a writer (drop the flag from its MANIFEST entry): {sorted(flagged - set(PLANNED))}; "
        f"has no writer but is not marked planned (add it): {sorted(set(PLANNED) - flagged)}")


def test_planned_is_a_true_flag_and_never_a_half_set_one():
    """One spelling of "no writer yet": ``True`` on the marked entries, the key absent everywhere else."""
    bad = {name: spec["planned"] for name, spec in rawstore.MANIFEST.items()
           if "planned" in spec and spec["planned"] is not True}
    assert not bad, f"planned must be True or absent: {bad}"


# A fresh store's columns for the planned tables, pinned 2026-10-21 before the flag was added: the flag must
# not change the DDL, so these stay exactly as the create path made them.
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
}


@pytest.mark.parametrize("name", sorted(PLANNED))
def test_the_flag_changes_no_ddl(name, rawstore_tmp_db):
    """A fresh store still creates the table, with the same columns and types, as it did without the flag."""
    assert name in FROZEN_DDL, f"{name} is planned but its DDL is not pinned here"
    with rawstore.connect(db=rawstore._db_of(name)) as conn:
        got = dict(conn.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'main' AND table_name = ? ORDER BY ordinal_position", [name]).fetchall())
    assert got == FROZEN_DDL[name], f"the planned flag changed {name}'s DDL"
