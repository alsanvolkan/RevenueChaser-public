"""The dataset registry claims every MANIFEST table once (#124, S3).

Four locks, each able to fail on its own:

- every MANIFEST table is claimed exactly once, and the allowlist is empty;
- a writer is an ``automations.REGISTRY`` name, or absent with a reason;
- a freshness pair names a real column of a real MANIFEST table;
- no table is listed on two datasets.
"""
from __future__ import annotations

from RevenueChaser.core import automations, datasets, rawstore

# Keys ``module_settings/inventory.py`` ``_collect`` builds today (19 row builders).
# The plan text said 22; this worktree's ``_collect`` has these.
SEEDED = (
    "universe", "universe_provenance", "identity", "corpus", "sec_submissions",
    "sec_files", "companyfacts", "ohlc", "price_bars", "index_levels", "vendor",
    "quotes", "index", "sec13f", "oipd", "etf_holdings", "relatedness",
    "result_history", "inputs",
)

# The seven datasets S4 adapted: ``coverage`` is a callable returning ``Health``.
ADAPTED = (
    "price_bars", "index_levels", "vendor", "quotes", "etf_holdings",
    "sec_submissions", "sec_files",
)


def test_every_manifest_table_is_claimed_once_and_the_allowlist_is_empty():
    """A new MANIFEST table fails here until a dataset owns it. The allowlist stays empty."""
    assert datasets.UNCLAIMED_ALLOWLIST == frozenset(), (
        f"unclaimed allowlist must be empty, not {sorted(datasets.UNCLAIMED_ALLOWLIST)}")
    keys = set(rawstore.MANIFEST)
    missing = datasets.unclaimed(keys)
    assert not missing, f"MANIFEST tables no dataset claims: {sorted(missing)}"
    claimed = [t for spec in datasets.REGISTRY for t in spec.tables]
    extra = set(claimed) - keys
    assert not extra, f"claimed but not in MANIFEST: {sorted(extra)}"
    assert len(claimed) == len(set(claimed)), "a table is claimed more than once (see the duplicate test)"
    assert set(claimed) == keys


def test_writer_is_a_registry_name_or_has_a_reason():
    """``writer`` is an ``automations.REGISTRY`` name, or None with a non-empty ``writer_note``."""
    names = {e.name for e in automations.REGISTRY}
    assert names, "automations.REGISTRY is empty; the writer check would pass vacuously"
    for spec in datasets.REGISTRY:
        if spec.writer is None:
            assert spec.writer_note and spec.writer_note.strip(), (
                f"{spec.key}: writer is None but writer_note is empty")
        else:
            assert spec.writer in names, (
                f"{spec.key}: writer {spec.writer!r} is not an automations.REGISTRY name")


def test_freshness_pairs_name_real_manifest_columns():
    """A pair is a column ``_max_date`` can read. Markers carry no pair."""
    for spec in datasets.REGISTRY:
        pairs = spec.freshness_pairs()
        if spec.freshness in (datasets.FROM_RUN_LOG, datasets.NO_CLAIM, None):
            assert pairs == (), f"{spec.key}: {spec.freshness!r} must not also name a column"
        else:
            assert pairs, f"{spec.key}: freshness {spec.freshness!r} names no column"
        for table, column in pairs:
            assert table in spec.tables, (
                f"{spec.key}: freshness reads {table}.{column}, which this dataset does not own")
            assert table in rawstore.MANIFEST, f"{spec.key}: {table} is not in MANIFEST"
            cols = rawstore._columns_of(rawstore.MANIFEST[table])
            assert column in cols, f"{spec.key}: {table}.{column} is not a MANIFEST column"
        if spec.freshness == datasets.FROM_RUN_LOG:
            note = spec.freshness_note or ""
            assert "#82" in note and "#83" in note, (
                f"{spec.key}: a from_run_log row must carry the #82/#83 caveat, not {note!r}")


def test_no_table_is_claimed_twice():
    """One table, one dataset. A second listing fails here and names both owners."""
    seen: dict[str, str] = {}
    for spec in datasets.REGISTRY:
        for table in spec.tables:
            assert table not in seen, f"{table} is claimed by {seen[table]} and {spec.key}"
            seen[table] = spec.key


def test_dataset_db_matches_the_manifest():
    """A dataset's ``db`` is the store file ``_db_of`` would pick for every table it owns."""
    for spec in datasets.REGISTRY:
        for table in spec.tables:
            db = (rawstore.MANIFEST[table].get("db") or "raw")
            assert spec.db == db, f"{spec.key} says db={spec.db} but {table} is in {db}"


def test_changes_tables_sit_with_their_base():
    """A ``*_changes`` table is claimed by the dataset that owns ``changes_of``."""
    for name, entry in rawstore.MANIFEST.items():
        base = entry.get("changes_of")
        if not base:
            continue
        assert datasets.dataset_of_table(name).key == datasets.dataset_of_table(base).key, (
            f"{name} (changes of {base}) is not on {base}'s dataset")


def test_history_file_tables_sit_on_result_history():
    """Every history.duckdb table is claimed by ``result_history``.

    ``earnings_history`` ends in ``_history`` but lives in raw: it is a change-history
    table of earnings prints, not a result-history mirror, and keeps its own dataset.
    """
    for name, entry in rawstore.MANIFEST.items():
        if entry.get("db") == "history":
            assert datasets.dataset_of_table(name).key == "result_history", name


def test_seeded_inventory_keys_keep_their_labels():
    """The rows ``_collect`` already shows stay addressable by the same key."""
    for key in SEEDED:
        spec = datasets.by_key(key)
        assert spec.label, key
        assert spec.tables, key


def test_the_seven_summaries_are_health_coverage_callables():
    """S4: each adapted dataset's ``coverage`` is a callable returning ``Health``, and the TODO is gone."""
    for key in ADAPTED:
        spec = datasets.by_key(key)
        assert callable(spec.coverage), f"{key}: coverage should be the health callable, not {spec.coverage!r}"
        assert not (spec.coverage_note and "TODO" in spec.coverage_note), (
            f"{key}: coverage is adapted, so the TODO note must go, not {spec.coverage_note!r}")


def test_every_adapted_coverage_returns_a_health_on_an_empty_store(rawstore_tmp_db):
    """The callable imports its summary when called and reads the store: the typed contract, empty store."""
    from RevenueChaser.core import datahealth

    for key in ADAPTED:
        got = datasets.by_key(key).coverage()
        assert isinstance(got, datahealth.Health), f"{key}: {got!r}"
