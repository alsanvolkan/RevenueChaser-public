"""The registry's facts on each inventory row (S5 of the Data Health plan, #124).

``_collect`` keeps building the rows it always built; ``_attach_registry`` adds the registry's
source, writer, coverage and freshness to them, and the collected rows are ordered worst
coverage first. Store only: the seeded store is a temp DuckDB, no provider is asked.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from RevenueChaser.core import datasets, rawstore
from RevenueChaser.core.datahealth import Health
from RevenueChaser.module_settings import inventory


def _spec(key: str, **over) -> datasets.DatasetSpec:
    """A copy of the registry's spec for ``key`` with some fields replaced."""
    return replace(datasets.by_key(key), **over)


def _row(key: str, **over) -> dict:
    """One row shaped the way ``_collect`` builds it."""
    row = {"key": key, "label": key, "tables": [], "rows": 1, "last_date": inventory.DASH,
           "holes": None, "hole_label": "", "fetch": None, "note": "kept"}
    row.update(over)
    return row


def _attach(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """``_attach_registry`` inside a call, so a recorded error is readable afterwards."""
    inventory._begin_call()
    try:
        out = inventory._attach_registry(rows)
        return out, list(inventory._CALL.errors)
    finally:
        inventory._end_call()


# ── the default sort ────────────────────────────────────────────────────────
def test_sort_key_puts_the_worst_coverage_first_and_no_coverage_last():
    # The labels run against the coverage on purpose: a key that ignored the fraction would
    # order these Alpha (100%) before Zulu (90%) and this assertion would catch it.
    rows = [
        {"key": "good", "label": "Alpha", "coverage": {"fraction": 1.0, "label": "905 / 905 (100%)"}},
        {"key": "none", "label": "None", "coverage": {"fraction": None, "label": "n/a"}},
        {"key": "bad", "label": "Zulu", "coverage": {"fraction": 0.9, "label": "812 / 905 (90%)"}},
    ]
    assert [r["key"] for r in sorted(rows, key=inventory.sort_key)] == ["bad", "good", "none"]


def test_sort_key_puts_error_and_unknown_rows_after_the_known_ones():
    rows = [
        {"key": "err", "label": "Error", "coverage": {"fraction": None, "label": "error"}},
        {"key": "unk", "label": "Unknown", "coverage": {"fraction": None, "label": "unknown"}},
        {"key": "na", "label": "n/a", "coverage": {"fraction": None, "label": "n/a"}},
        {"key": "half", "label": "Half", "coverage": {"fraction": 0.5, "label": "1 / 2 (50%)"}},
    ]
    assert [r["key"] for r in sorted(rows, key=inventory.sort_key)] == [
        "half", "err", "na", "unk"]


def test_sort_key_breaks_a_tie_on_the_known_fraction_by_label():
    rows = [
        {"key": "b", "label": "Bravo", "coverage": {"fraction": 0.5, "label": "1 / 2 (50%)"}},
        {"key": "a", "label": "Alpha", "coverage": {"fraction": 0.5, "label": "1 / 2 (50%)"}},
    ]
    assert [r["key"] for r in sorted(rows, key=inventory.sort_key)] == ["a", "b"]


def test_a_row_without_a_registry_spec_keeps_its_fields_and_gains_a_none_source():
    row = _row("not_in_the_registry", holes=3, note="kept")
    out, _errors = _attach([row])
    assert out[0] is row
    assert row["source"] is None
    assert row["holes"] == 3 and row["note"] == "kept"
    assert "coverage" not in row and "fresh" not in row, "no spec: nothing is invented"
    assert "db" not in row, "no spec: no store file to name, so the table draws no pill"


# ── source, writer, fresh ───────────────────────────────────────────────────
def test_attach_registry_takes_the_source_and_the_writer_from_the_spec(monkeypatch):
    monkeypatch.setitem(datasets._BY_KEY, "price_bars", _spec("price_bars", coverage=None))
    out, _errors = _attach([_row("price_bars")])
    assert out[0]["source"] == "uw"
    assert out[0]["writer"] == "RC-Daily-Bars"
    assert out[0]["writer_note"] is None


def test_attach_registry_carries_the_writer_note_a_spec_without_a_writer_has(monkeypatch):
    monkeypatch.setitem(datasets._BY_KEY, "sec13f", _spec("sec13f", coverage=None))
    out, _errors = _attach([_row("sec13f")])
    assert out[0]["writer"] is None
    assert "sec13f_rebuild" in out[0]["writer_note"]


def test_attach_registry_carries_the_store_file_the_spec_names(monkeypatch):
    """G2 (#124): ``DatasetSpec.db`` is the store file, so the table can pill every dataset."""
    for key, db in (("universe", "raw"), ("corpus", "edgar"), ("price_bars", "prices"),
                    ("result_history", "history"), ("discord", "discord")):
        monkeypatch.setitem(datasets._BY_KEY, key, _spec(key, coverage=None))
        out, _errors = _attach([_row(key)])
        assert out[0]["db"] == db, key


def test_fresh_is_the_max_date_of_the_specs_freshness_pair(rawstore_tmp_db):
    rawstore.write_rows("chain_filings", [{"ticker": "AAPL", "cik": "1", "form": "10-K",
                                           "filed": "2026-01-15"}])
    rawstore.write_rows("edgar_documents", [{"accession": "a", "form": "10-K",
                                             "filed": "2026-03-01"}])
    out, _errors = _attach([_row("corpus")])
    expected = inventory._max_date(("chain_filings", "filed"), ("edgar_documents", "filed"))
    assert out[0]["fresh"] == expected == "2026-03-01"


def test_a_spec_with_no_freshness_pair_has_no_fresh_date(rawstore_tmp_db):
    out, _errors = _attach([_row("universe")])       # freshness None (no fetch stamp)
    assert out[0]["fresh"] is None


# ── coverage ────────────────────────────────────────────────────────────────
def test_coverage_over_an_expected_population_names_both_counts_and_the_percentage(monkeypatch):
    healthy = Health(covered=812, expected=905, newest="2026-09-20", expected_as_of="2026-09-20")
    monkeypatch.setitem(datasets._BY_KEY, "quotes", _spec("quotes", coverage=lambda: healthy))
    out, _errors = _attach([_row("quotes")])
    cov = out[0]["coverage"]
    assert cov["label"] == "812 / 905 (90%)"
    assert cov["covered"] == 812 and cov["expected"] == 905
    assert cov["fraction"] == pytest.approx(812 / 905)
    assert cov["newest"] == "2026-09-20"
    assert cov["problems"] == []


def test_no_expected_population_is_n_a_never_zero_percent(monkeypatch):
    for index, healthy in enumerate((
        Health(covered=0, expected=None, newest=None, expected_as_of=None),
        Health(covered=0, expected=0, newest=None, expected_as_of=None),
        Health(covered=None, expected=905, newest=None, expected_as_of=None),
    )):
        monkeypatch.setitem(datasets._BY_KEY, "quotes", _spec("quotes", coverage=lambda h=healthy: h))
        out, _errors = _attach([_row("quotes")])
        assert out[0]["coverage"]["label"] == "n/a", index
        assert out[0]["coverage"]["fraction"] is None, index


def test_a_coverage_that_raises_reads_error_and_is_recorded_instead_of_raising(monkeypatch):
    def boom() -> Health:
        raise RuntimeError("the summary could not read the store")

    monkeypatch.setitem(datasets._BY_KEY, "quotes", _spec("quotes", coverage=boom))
    out, errors = _attach([_row("quotes", tables=["quote_snapshot"])])
    assert out[0]["coverage"]["label"] == "error"
    assert out[0]["coverage"]["problems"] == ["the summary could not read the store"]
    assert out[0]["coverage"]["fraction"] is None
    assert [e["where"] for e in errors] == ["_coverage"]
    assert errors[0]["table"] == "quote_snapshot", "the error lands on the row it belongs to"


def test_a_coverage_failure_the_row_already_reported_is_not_recorded_twice(monkeypatch):
    """One cause, one entry: the row builder's summary and the coverage callable are the same
    failing read, so the error list must not say it twice."""
    def boom() -> Health:
        raise RuntimeError("bad read")

    monkeypatch.setitem(datasets._BY_KEY, "quotes", _spec("quotes", coverage=boom))
    inventory._begin_call()
    try:
        inventory._record("quote_snapshot", "_quotes_row", RuntimeError("bad read"))
        out = inventory._attach_registry([_row("quotes", tables=["quote_snapshot"])])
        errors = [e["where"] for e in inventory._CALL.errors]
    finally:
        inventory._end_call()
    assert out[0]["coverage"]["label"] == "error"
    assert errors == ["_quotes_row"]


def test_a_coverage_error_message_is_cut_to_a_renderable_length(monkeypatch):
    def boom() -> Health:
        raise RuntimeError("x" * 400)

    monkeypatch.setitem(datasets._BY_KEY, "quotes", _spec("quotes", coverage=boom))
    out, _errors = _attach([_row("quotes")])
    assert len(out[0]["coverage"]["problems"][0]) == 120


def test_a_coverage_callable_that_returns_no_health_reads_unknown(monkeypatch):
    monkeypatch.setitem(datasets._BY_KEY, "quotes", _spec("quotes", coverage=lambda: None))
    out, _errors = _attach([_row("quotes")])
    assert out[0]["coverage"]["label"] == "unknown"
    assert out[0]["coverage"]["fraction"] is None


def test_a_two_query_coverage_spec_is_not_evaluated_and_reads_n_a(monkeypatch):
    """S5 has no population/presence evaluator for the two-SQL form; it must say n/a, never guess."""
    monkeypatch.setitem(datasets._BY_KEY, "quotes", _spec("quotes", coverage=datasets.CoverageSpec(
        population="SELECT ticker FROM equity_universe",
        presence="SELECT ticker FROM quote_snapshot")))
    out, errors = _attach([_row("quotes")])
    assert out[0]["coverage"]["label"] == "n/a"
    assert out[0]["coverage"]["fraction"] is None
    assert errors == []


# ── G5 (#124): the seconds the collect took, so the board can print them ─────────────────────
def test_collect_reports_a_non_negative_number_of_seconds(rawstore_tmp_db):
    seconds = inventory.collect()["timing"]["collect_seconds"]
    assert isinstance(seconds, (int, float)) and not isinstance(seconds, bool)
    assert seconds >= 0


def test_collect_measures_the_clock_around_the_collect(monkeypatch, rawstore_tmp_db):
    """The timing is the clock across ``_collect`` and nothing else: a fixed clock proves it."""
    class _Clock:
        def __init__(self):
            self.values = [10.0, 12.5]       # 2.5 s pass while the collect runs, then time stops

        def perf_counter(self):
            return self.values.pop(0) if len(self.values) > 1 else self.values[0]

    monkeypatch.setattr(inventory, "time", _Clock())
    assert inventory.collect()["timing"] == {"collect_seconds": 2.5}

# ── S6 of the Data Health plan (#124): "Other store tables" ─────────────────────────────────
# The backstop for a MANIFEST table no dataset claims. Empty today (the registry claims every
# table); a registry hole must be reported with the store file it lives in, its row count and its
# newest date — and an empty set must cost the store no query at all.
_NOW = datetime(2026, 9, 25, 20, 0, tzinfo=timezone.utc)


def _leave_unclaimed(monkeypatch, *tables: str) -> None:
    """Act as if the registry forgot ``tables`` (the real registry claims every one of them)."""
    claimed = datasets.tables_claimed() - set(tables)
    monkeypatch.setattr(datasets, "tables_claimed", lambda: claimed)


def test_unclaimed_tables_asks_the_store_nothing_when_every_table_is_claimed(monkeypatch):
    """Empty is the normal state, and it must not cost a query."""
    def boom(*_args, **_kwargs):
        raise AssertionError("unclaimed_tables read the store although no table is unclaimed")

    assert datasets.unclaimed(rawstore.MANIFEST) == frozenset(), \
        "core.datasets must keep claiming every MANIFEST table"
    # The read helpers this function would use, not only rawstore.query: a failing query is caught
    # and recorded inside them, so a swallowed raise there would let a read through unnoticed.
    for name in ("_count", "_existing_columns", "_max_date"):
        monkeypatch.setattr(inventory, name, boom)
    monkeypatch.setattr(rawstore, "query", boom)
    assert inventory.unclaimed_tables() == []


def test_an_unclaimed_table_is_reported_with_its_store_file_rows_and_newest_date(rawstore_tmp_db,
                                                                                monkeypatch):
    rawstore.write_rows("chain_filings", [
        {"ticker": "AAPL", "cik": "1", "form": "10-K", "filed": "2026-03-01"},
        {"ticker": "MU", "cik": "2", "form": "10-K", "filed": "2026-01-15"},
    ])
    _leave_unclaimed(monkeypatch, "chain_filings")
    assert inventory.unclaimed_tables() == [
        {"table": "chain_filings", "db": "edgar", "rows": 2, "newest": "2026-03-01"}]


def test_a_table_with_no_time_column_reports_no_newest_date(rawstore_tmp_db, monkeypatch):
    """``watchlist`` carries only a ticker: there is no date to read, so none is invented."""
    rawstore.write_rows("watchlist", [{"ticker": "AAPL"}, {"ticker": "MU"}])
    _leave_unclaimed(monkeypatch, "watchlist")
    assert inventory.unclaimed_tables() == [
        {"table": "watchlist", "db": "raw", "rows": 2, "newest": None}]


def test_several_unclaimed_tables_are_listed_by_name_and_an_empty_one_counts_zero(rawstore_tmp_db,
                                                                                 monkeypatch):
    rawstore.write_rows("watchlist", [{"ticker": "AAPL"}])
    _leave_unclaimed(monkeypatch, "watchlist", "store_meta")
    got = inventory.unclaimed_tables()
    assert [r["table"] for r in got] == ["store_meta", "watchlist"]
    assert got[0]["rows"] == 0 and got[0]["newest"] is None


def test_the_payload_carries_the_other_tables_list(rawstore_tmp_db):
    assert inventory.collect(now=_NOW)["other_tables"] == []


def test_the_payload_carries_an_unclaimed_table(rawstore_tmp_db, monkeypatch):
    rawstore.write_rows("watchlist", [{"ticker": "AAPL"}])
    _leave_unclaimed(monkeypatch, "watchlist")
    assert inventory.collect(now=_NOW)["other_tables"] == [
        {"table": "watchlist", "db": "raw", "rows": 1, "newest": None}]
