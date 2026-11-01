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


#: Served tables the control board's "Refresh the example rows" cannot answer: it draws the button for every table
#: a dataset serves (``module_settings/newdata.py``: ``samples`` comes from ``ds.tables``), but the route behind it
#: needs the table's written column and its kind, and these seven are in neither or only one map. Measured
#: 30 Oct 2026. The set is the record of a gap that predates the test, not a target: a table added to a dataset
#: must be answerable, not join this list.
REFRESH_GAPS = {"adj_factor", "gics_history", "peer_group", "sec13f_cusip_map", "sec13f_filer_period",
                "sec13f_filings", "security_history"}


def test_every_served_table_can_answer_the_boards_refresh_or_is_a_named_gap():
    from RevenueChaser.rcdata.build import DATASETS
    written = screen.written_columns()
    served = {t for ds in DATASETS.values() for t in ds.tables}
    assert served, "no dataset serves a table: the registry did not load"
    assert {t for t in served if t not in written or t not in read.KIND_OF_TABLE} == REFRESH_GAPS


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


# ── The control board's own lists (design v28, 30 Oct 2026) ──────────────────────────────────────────────────────

def test_every_card_names_its_build_step_and_its_role():
    for name, card in screen.DATASETS.items():
        assert 1 <= card.step <= 11, f"{name}: step {card.step}"
        assert card.role in screen.ROLES, f"{name}: role {card.role!r}"


def test_a_kinds_steps_sit_inside_its_files_steps():
    from RevenueChaser.rcdata.build import DATASETS
    spans = {"spine": range(1, 8), "prices": (8,), "fundamentals": (9,), "ownership": (10,), "options": (11,),
             "market": (11,)}
    for name, ds in DATASETS.items():
        assert screen.DATASETS[name].step in spans[ds.kind], f"{name} ({ds.kind}): step {screen.DATASETS[name].step}"


def test_the_files_are_every_serving_kind_in_build_order():
    from RevenueChaser.rcdata import serving
    assert set(screen.FILES) == set(serving.KINDS)
    assert list(screen.FILES) == ["spine", "prices", "fundamentals", "ownership", "options", "market"]


def test_a_dataset_that_does_not_fill_by_tier_has_words_for_why():
    from RevenueChaser.rcdata.build import DATASETS, base
    for name, ds in DATASETS.items():
        if not base.honours_scope(ds):
            assert screen.NO_TIER_DATASET_WORDS.get(name) or screen.NO_TIER_WORDS.get(ds.kind), name


def test_the_universe_offers_a_and_b_in_three_sets_and_shows_c_and_d_unoffered():
    offered = [s for s in screen.UNIVERSE_SETS if s.offered]
    assert [s.key for s in offered] == ["A", "E", "R"]
    assert {s.tier for s in offered} == {"A", "B"}
    assert [s.key for s in screen.UNIVERSE_SETS if not s.offered] == ["C", "D"]
    from RevenueChaser.core.identity.types import EXCLUDED_TYPES
    assert set(screen.FUND_TYPES) <= EXCLUDED_TYPES


def test_the_include_words_are_the_builders_own_groups():
    from RevenueChaser.rcdata.build import base
    assert tuple(screen.INCLUDE_WORDS) == base.SCOPE_GROUPS


@pytest.mark.parametrize("value,on", [(None, False), ("", False), ("0", False), ("no", False), ("1", True),
                                      ("true", True), ("ON", True)])
def test_the_fill_switch_is_off_unless_set(monkeypatch, value, on):
    if value is None:
        monkeypatch.delenv(screen.FILL_ENV, raising=False)
    else:
        monkeypatch.setenv(screen.FILL_ENV, value)
    assert screen.fill_enabled() is on


# ── The control board's steps and providers (design v28) ───────────────────────────────────────────────────────────

def test_every_card_step_is_a_step_the_board_names():
    from RevenueChaser.rcdata.api import screen
    assert set(screen.STEPS) == set(range(1, 12))
    assert {c.step for c in screen.DATASETS.values()} <= set(screen.STEPS)


def test_every_provider_a_dataset_names_has_its_words():
    from RevenueChaser.rcdata.api import screen
    from RevenueChaser.module_settings import newdata
    named = {p for ds in newdata._datasets().values() for p in getattr(ds, "providers", ())}
    assert named, "no provider found at all: the search is broken"
    assert named <= set(screen.PROVIDERS), f"no words for {sorted(named - set(screen.PROVIDERS))}"
    assert all(w.name and w.what for w in screen.PROVIDERS.values())
