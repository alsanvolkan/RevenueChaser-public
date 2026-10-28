"""The dataset registry claims every MANIFEST table once (#124, S3).

Four locks, each able to fail on its own:

- every MANIFEST table is claimed exactly once, and the allowlist is empty;
- a writer is an ``automations.REGISTRY`` name, or absent with a reason;
- a freshness pair names a real column of a real MANIFEST table;
- no table is listed on two datasets.

G11 (#124) adds two more: a dataset whose measured coverage is below 100% must carry a gap kind,
and a dataset that declares coverage at all must carry one on a store where nothing can be measured.
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


# ── G11 of the GUI redesign (#124): a measured hole carries its explanation ───────────────────
# Two additive fields, ``gap_kind`` and ``next_step``. The rule they enforce: a dataset whose
# measured coverage is below 100% must carry a ``gap_kind``, and ``fixable`` / ``in_build`` /
# ``needs_decision`` must carry a ``next_step``. A ``ceiling`` carries its REASON in ``next_step``
# (a fact about the source), so it needs one too.
from RevenueChaser.core.datahealth import Health  # noqa: E402


def _probe(**over) -> datasets.DatasetSpec:
    """A hand-made spec for the rule's own test: every field the constructor needs, nothing else."""
    fields = dict(key="probe", label="Probe", tables=("quote_snapshot",), db="raw",
                  source=datasets.Source.store, writer="RC-Daily-Bars", freshness=None)
    fields.update(over)
    return datasets.DatasetSpec(**fields)


def _gap_problems(spec: datasets.DatasetSpec, health: Health) -> list[str]:
    """G11's CI rule: a measured coverage below 100% with no ``gap_kind`` is a defect.

    ``None`` coverage is not a measurement (no population, or no summary), so the rule stays
    silent there; ``MissingDeclaredCoverage`` below covers those over the real registry.
    """
    ratio = health.coverage
    if ratio is not None and ratio < 1 and spec.gap_kind is None:
        return [f"{spec.key}: coverage {ratio:.0%} is below 100% and gap_kind is None"]
    return []


def test_the_ci_rule_fires_on_a_measured_hole_with_no_gap_kind():
    """Red first: a dataset below 100% coverage and no explanation fails the rule."""
    assert _gap_problems(_probe(), Health(covered=9, expected=10, newest=None,
                                          expected_as_of=None)), \
        "a measured hole with no gap_kind must fail the rule"


def test_the_rule_is_silent_when_the_hole_carries_its_kind():
    spec = _probe(gap_kind=datasets.GapKind.fixable, next_step="Join the two counts.")
    assert _gap_problems(spec, Health(covered=9, expected=10, newest=None,
                                      expected_as_of=None)) == []


def test_the_rule_is_silent_at_full_coverage_and_where_nothing_is_measured():
    hole = Health(covered=10, expected=10, newest=None, expected_as_of=None)
    no_population = Health(covered=5, expected=None, newest=None, expected_as_of=None)
    no_summary = Health(covered=None, expected=None, newest=None, expected_as_of=None)
    for health in (hole, no_population, no_summary):
        assert _gap_problems(_probe(), health) == [], health


def test_the_rule_reports_covered_above_expected_as_no_hole():
    """More rows than the population is a consistency problem elsewhere, not a missing row."""
    assert _gap_problems(_probe(), Health(covered=11, expected=10, newest=None,
                                          expected_as_of=None)) == []


def _spec_error(**over) -> str:
    """The ValueError message a bad pair raises."""
    import pytest

    with pytest.raises(ValueError) as caught:
        _probe(**over)
    return str(caught.value)


def test_a_gap_kind_outside_the_six_is_refused():
    said = _spec_error(gap_kind="someday")
    assert "gap_kind" in said and "someday" in said


def test_a_gap_kind_written_as_a_bare_string_is_refused():
    """The enum is the one home of the six kinds; a bare string would dodge the type."""
    said = _spec_error(gap_kind="fixable", next_step="Fix it.")
    assert "gap_kind" in said and "fixable" in said


def test_fixable_in_build_and_needs_decision_all_require_a_next_step():
    for kind in (datasets.GapKind.fixable, datasets.GapKind.in_build,
                 datasets.GapKind.needs_decision):
        said = _spec_error(gap_kind=kind)
        assert "next_step" in said, (kind, said)


def test_a_ceiling_needs_its_reason():
    """The brief: a ceiling with no reason is a defect, not a state."""
    said = _spec_error(gap_kind=datasets.GapKind.ceiling)
    assert "next_step" in said and "reason" in said.lower()


def test_a_self_healing_or_complete_dataset_needs_no_next_step():
    for kind in (datasets.GapKind.self_heals, datasets.GapKind.none):
        spec = _probe(gap_kind=kind)
        assert spec.gap_kind is kind and spec.next_step is None


def test_a_blank_next_step_is_refused():
    said = _spec_error(gap_kind=datasets.GapKind.fixable, next_step="   ")
    assert "next_step" in said


def test_a_ceiling_carries_its_reason_in_next_step():
    said = "OpenFIGI maps from a CUSIP, so a name with no CUSIP cannot get one on this route."
    spec = _probe(gap_kind=datasets.GapKind.ceiling, next_step=said)
    assert spec.next_step == said


def test_every_dataset_that_declares_coverage_carries_a_gap_kind():
    """On a test store we cannot measure completeness, so any declared coverage needs its kind.

    A dataset with no coverage declares nothing and needs none: the rule above covers the
    measured case; this covers the unmeasurable one.
    """
    missing = [spec.key for spec in datasets.REGISTRY
               if spec.coverage is not None and spec.gap_kind is None]
    assert not missing, (
        "these datasets declare coverage but no gap_kind, so a hole has no explanation beside "
        f"it: {missing}")


def test_no_dataset_carries_a_gap_kind_without_the_step_its_kind_needs():
    """The constructor enforces this; the registry test states it for the real rows."""
    needs_step = {datasets.GapKind.fixable, datasets.GapKind.in_build,
                  datasets.GapKind.needs_decision, datasets.GapKind.ceiling}
    for spec in datasets.REGISTRY:
        if spec.gap_kind in needs_step:
            assert spec.next_step and spec.next_step.strip(), spec.key


def test_the_inputs_freshness_no_longer_reads_the_unwritten_watchlist_history():
    """PR 4: the watch lists live in ops.sqlite; the raw ``watchlist`` tables stay claimed (the manifest still has
    them) but nothing writes them, so the inputs freshness reads the board settings' history only."""
    spec = datasets.by_key("inputs")
    assert spec.freshness == ("board_settings_changes", "run_at")
    assert "watchlist" in spec.tables and "ops.sqlite" in spec.writer_note
