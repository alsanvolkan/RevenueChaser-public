"""The Settings / Data screen's lists live beside the catalog, not in the screen.

Before this the screen carried six hand-written maps — labels, cadence, coverage target, the newest-date column,
the status order, the retirement list — and a need added to the catalog meant a second edit in a second file, which
is how the mock came to draw lines the code does not read (Implied Probability drawn against 21 tables while the
board reads nothing from the new store). The maps move here, the screen reads them, and a write to the screen's
copy is refused so a second copy cannot be introduced by accident.

**The row's day and the written column are two different columns, on purpose.** ``Need.day_column`` is the day the
board asks about (a quarter's ``period_end``, an earnings ``report_date``); ``WRITTEN_COLUMN`` is the column that
says how recently the dataset was *written* (``filed``, ``source_as_of``), which is what a freshness card shows.
Seven tables differ, and ``test_the_rows_day_and_the_written_column_are_allowed_to_differ`` pins that so the two
are not merged later by someone who reads them as a duplicate.
"""
from __future__ import annotations

import pytest

from RevenueChaser.rcdata import read
from RevenueChaser.rcdata.api import catalog, screen

DIFFER = {"index_membership", "fundamental_fact", "earnings_event", "segment_revenue", "customer_share",
          "fundamental_revision", "sec13f_periods"}


def test_every_dataset_a_need_names_has_a_card():
    for need in catalog.NEEDS.values():
        if need.dataset is not None:
            assert need.dataset in screen.DATASETS, f"{need.name}: dataset {need.dataset} has no card"


def test_every_dataset_the_store_knows_has_a_card():
    known = {d for d in read.DATASET_OF_TABLE.values() if d is not None}
    assert known <= set(screen.DATASETS)


def test_a_card_carries_a_label_a_cadence_and_a_share_or_nothing():
    for name, card in screen.DATASETS.items():
        assert card.label and card.label != name, f"{name}: a card needs a label in plain words"
        assert card.cadence in ("daily", "weekly", "monthly"), f"{name}: {card.cadence!r}"
        assert card.coverage_target is None or 0 < card.coverage_target <= 1, f"{name}: {card.coverage_target!r}"


def test_the_screens_maps_are_the_cards():
    assert screen.labels() == {n: c.label for n, c in screen.DATASETS.items()}
    assert screen.cadences() == {n: c.cadence for n, c in screen.DATASETS.items()}
    assert screen.coverage_targets()["alpaca/bars"] == pytest.approx(0.99)
    assert screen.coverage_targets()["market_series"] is None
    assert "market_series" in screen.coverage_targets(), "a card with no target is still in the map, as None"


def test_the_screen_reads_the_cards_and_refuses_a_copy_of_its_own():
    from RevenueChaser.module_settings import newdata
    assert newdata.LABELS == screen.labels()
    assert newdata.DATE_COLUMN == screen.written_columns()
    assert newdata.COVERAGE_TARGET == screen.coverage_targets()
    assert newdata.RETIRING == screen.RETIRING
    for target, key in ((newdata.LABELS, "market_series"), (newdata.DATE_COLUMN, "market_series"),
                        (newdata.COVERAGE_TARGET, "market_series")):
        with pytest.raises(TypeError):
            target[key] = "whatever"


def test_a_label_changed_in_one_place_is_what_the_screen_shows(monkeypatch):
    from RevenueChaser.module_settings import newdata
    card = screen.DATASETS["market_series"]
    monkeypatch.setitem(screen.DATASETS, "market_series", type(card)(**{**card.__dict__, "label": "Changed"}))
    assert newdata.LABELS["market_series"] == "Changed"
    assert newdata._card_label("market_series") == "Changed"
    monkeypatch.setitem(screen.WRITTEN_COLUMN, "market_series", "written_on")
    assert newdata.DATE_COLUMN["market_series"] == "written_on", "the screen reads the column, it does not copy it"


def test_every_table_a_need_names_has_a_written_column_or_no_date_at_all():
    for need in catalog.NEEDS.values():
        if need.day_column is None:
            continue
        assert need.table in screen.written_columns(), f"{need.name}: {need.table} has no written column"


def test_the_rows_day_and_the_written_column_are_allowed_to_differ():
    written = screen.written_columns()
    differ = {n.table for n in catalog.NEEDS.values()
              if n.day_column and n.table in written and written[n.table] != n.day_column}
    assert differ == DIFFER, ("the board's day and the write's day are two questions; this set is the record of "
                             "where they differ, not a list of mistakes")


def test_the_screens_own_numbers_live_here_too():
    assert screen.CYCLE_DAYS == {"weekly": 8, "monthly": 35}
    assert screen.RETIREMENT_DAYS == 14
    assert screen.STATUS_ORDER == ("stale", "never", "late", "unchanged", "fresh")
    assert screen.STATUS_ORDER[0] == "stale", "the worst status sorts first"


def test_a_retiring_table_names_the_one_that_replaces_it():
    for old, new in screen.RETIRING.items():
        assert old not in screen.written_columns(), f"{old} is retiring, so the screen no longer dates it"
        assert new in screen.written_columns(), f"{old} is replaced by {new}, which the screen does not know"
