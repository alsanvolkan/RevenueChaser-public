"""Which board reads which need, declared in the board's own package and checked against its source.

The flow chart on the Settings / Data screen is drawn from this record, so the record has to be the truth rather
than a second list that drifts. Two rules carry that, and this file is where they are enforced:

- **A board declares what it reads** (``READS`` in its package ``__init__``), or it is named in
  ``boards.UNDECLARED`` as not migrated yet. A new board package can be neither, so it cannot be forgotten.
- **A board names no need it has not declared.** The check reads the board's own source for need names as string
  literals, which is exact — a name is either in ``NEEDS`` or it is not — rather than guessing from table names.

Only the mechanism and this test land with this PR (avo's plan, 29 Oct 2026); the declarations fill in board by
board as each board migrates to the API.
"""
from __future__ import annotations

import pytest

from RevenueChaser.rcdata import api
from RevenueChaser.rcdata.api import boards


def test_every_board_package_is_either_declared_or_named_as_not_migrated():
    found = set(boards.board_packages())
    assert found, "no board packages found at all: the search is broken, not the tree"
    declared = set(boards.all_reads())
    assert declared | boards.UNDECLARED == found
    assert not (declared & boards.UNDECLARED)


def test_the_boards_search_finds_the_ones_that_exist_and_leaves_out_what_is_not_a_board():
    found = boards.board_packages()
    assert "module_earnings_radar" in found
    assert "module_spx_gamma" in found
    assert "module_testing" not in found, "the e2e tier is not a dashboard"
    assert found == tuple(sorted(found))


def test_every_declared_need_is_a_need_the_catalog_has():
    for board, reads in boards.all_reads().items():
        for name in reads:
            assert name in api.NEEDS, f"{board} declares {name!r}, which the catalog does not have"


def test_a_declaration_is_sorted_and_without_a_repeat():
    for board, reads in boards.all_reads().items():
        assert isinstance(reads, tuple) and all(isinstance(n, str) for n in reads)
        assert list(reads) == sorted(set(reads)), f"{board}: READS is sorted and each name appears once"


def test_the_migrated_board_declares_the_four_needs_it_reads():
    assert boards.all_reads()["module_earnings_radar"] == (
        "earnings.next", "fundamentals.estimates", "macro.dgs10", "ownership.short_interest")


def test_no_board_names_a_need_it_has_not_declared():
    offenders = {b: boards.undeclared_uses(b) for b in boards.board_packages()}
    assert {b: u for b, u in offenders.items() if u} == {}


def test_the_source_search_sees_a_need_named_in_the_board_and_not_one_named_in_its_tests(tmp_path, monkeypatch):
    pkg = tmp_path / "module_fake"
    (pkg / "tests").mkdir(parents=True)
    (pkg / "__init__.py").write_text('READS = ("macro.dgs10",)\n', encoding="utf-8")
    (pkg / "board.py").write_text('NEED = "macro.dgs10"\nOTHER = "prices.bars"\n', encoding="utf-8")
    (pkg / "tests" / "test_board.py").write_text('X = "market.vix"\n', encoding="utf-8")
    monkeypatch.setattr(boards, "root", lambda: tmp_path)
    assert boards.need_literals("module_fake") == ("macro.dgs10", "prices.bars")
    assert boards.undeclared_uses("module_fake") == ("prices.bars",)


def test_which_boards_read_a_need_is_answered_from_the_same_record():
    assert "module_earnings_radar" in boards.readers_of("earnings.next")
    assert boards.readers_of("prices.bars") == ()
    with pytest.raises(api.UnknownNeed):
        boards.readers_of("no.such.need")


def test_the_flow_chart_rows_come_from_the_record_and_carry_what_it_draws():
    rows = boards.flow_rows()
    row = next(r for r in rows if r["need"] == "earnings.next")
    need = api.NEEDS["earnings.next"]
    assert row["boards"] == ("module_earnings_radar",)
    assert row["dataset"] == need.dataset and row["kind"] == need.kind
    assert row["max_lag"] == need.max_lag and row["max_lag_unit"] == need.max_lag_unit
    assert {r["need"] for r in rows} == set(api.NEEDS)
    counted_in_days = next(r for r in rows if r["need"] == "fundamentals.estimates")
    assert counted_in_days["max_lag_unit"] == "days", "the chart carries each need's own unit, not one for all"

