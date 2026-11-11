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

from RevenueChaser.rcdata import needs
from RevenueChaser.rcdata.needs import boards


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
            assert name in needs.NEEDS, f"{board} declares {name!r}, which the catalog does not have"


def test_a_declaration_is_sorted_and_without_a_repeat():
    for board, reads in boards.all_reads().items():
        assert isinstance(reads, tuple) and all(isinstance(n, str) for n in reads)
        assert list(reads) == sorted(set(reads)), f"{board}: READS is sorted and each name appears once"


def test_the_migrated_board_declares_the_seven_needs_it_reads():
    # 11 Nov 2026: R6's rate events read market.economic_event (market.economic_calendar), not raw.
    assert boards.all_reads()["module_earnings_radar"] == (
        "earnings.next", "earnings.options", "fundamentals.estimates", "macro.dgs10", "market.economic_calendar",
        "ownership.short_interest", "ownership.shortable")


def test_spx_gamma_is_the_second_migrated_board_and_leaves_the_plan():
    """#330: SPX Gamma declares the needs it reads and is neither "not migrated" nor "planned" any more (its plan
    named three option needs it never read). Since #291 the DTE-band walls read the stored greek grid too."""
    reads = boards.all_reads()
    assert reads["module_spx_gamma"] == ("market.cot", "market.cot_release", "market.spx_close",
                                         "market.spx_expiry_strike", "market.spx_greek_expiry_strike",
                                         "market.vix")
    assert "module_spx_gamma" not in boards.UNDECLARED and "module_spx_gamma" not in boards.PLANNED_READS
    assert boards.reads_of("module_spx_gamma") == {"declared": True, "needs": reads["module_spx_gamma"]}
    assert boards.readers_of("market.spx_close") == ("module_spx_gamma", "module_welcome")   # #643 PR 5


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
    assert boards.readers_of("prices.bars") == ("module_comovement",)
    with pytest.raises(needs.UnknownNeed):
        boards.readers_of("no.such.need")


def test_the_flow_chart_rows_come_from_the_record_and_carry_what_it_draws():
    rows = boards.flow_rows()
    row = next(r for r in rows if r["need"] == "earnings.next")
    need = needs.NEEDS["earnings.next"]
    assert row["boards"] == ("module_earnings_radar",)
    assert row["dataset"] == need.dataset and row["kind"] == need.kind
    assert row["max_lag"] == need.max_lag and row["max_lag_unit"] == need.max_lag_unit
    assert {r["need"] for r in rows} == set(needs.NEEDS)
    counted_in_days = next(r for r in rows if r["need"] == "fundamentals.estimates")
    assert counted_in_days["max_lag_unit"] == "days", "the chart carries each need's own unit, not one for all"



# ── The review's m6: the record is read from code, not from prose ──

def test_a_need_named_only_in_a_comment_is_not_a_use(tmp_path, monkeypatch):
    """``need_literals`` matched a need name in any string, docstrings and comments included, so a board that only
    mentions a need in prose read as an undeclared use - and the CI guard then asks for a declaration of something
    the board does not read."""
    pkg = tmp_path / "module_fake"
    pkg.mkdir()
    (pkg / "__init__.py").write_text('READS = ("identity.universe",)\n', encoding="utf-8")
    (pkg / "notes.py").write_text(
        '"""This board once read \'prices.bars\' and no longer does."""\n'
        "# see also 'identity.security' for why\n"
        'NEEDS_READ = ("identity.universe",)\n', encoding="utf-8")
    monkeypatch.setattr(boards, "root", lambda: tmp_path)
    assert boards.need_literals("module_fake") == ("identity.universe",)
    assert boards.undeclared_uses("module_fake") == ()


def test_a_declaration_is_read_whole_even_with_a_bracket_in_a_comment(tmp_path, monkeypatch):
    """The regex stopped at the first ``)``, so a comment carrying one inside the tuple truncated the declaration
    silently - the board then looked as if it had never declared the needs below that line."""
    pkg = tmp_path / "module_fake"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        'READS = (\n'
        '    "identity.universe",   # the shared universe (see load_universe(), D6)\n'
        '    "prices.bars",\n'
        ')\n', encoding="utf-8")
    monkeypatch.setattr(boards, "root", lambda: tmp_path)
    assert boards.declared_reads("module_fake") == ("identity.universe", "prices.bars")


# ── Planned reads: what a board not migrated yet will read (the control board's dashed lines) ──────────────────────

def test_a_planned_read_belongs_to_a_board_not_migrated_yet():
    assert set(boards.PLANNED_READS) <= boards.UNDECLARED
    assert not set(boards.PLANNED_READS) & set(boards.all_reads()), "a declared board's reads are its own, not planned"


def test_every_planned_need_is_a_need_the_catalog_has():
    from RevenueChaser.rcdata.needs import catalog
    for board, needs in boards.PLANNED_READS.items():
        assert needs == tuple(sorted(set(needs), key=needs.index)), f"{board}: a need named twice"
        for name in needs:
            assert name in catalog.NEEDS, f"{board}: planned need {name!r} is not in the catalog"


def test_the_reads_of_a_board_say_whether_it_declared_them():
    declared = boards.reads_of("module_earnings_radar")
    assert declared["declared"] is True and "macro.dgs10" in declared["needs"]
    planned = boards.reads_of("module_fundamentals")
    assert planned["declared"] is False and "prices.bars" in planned["needs"]
    assert boards.reads_of("module_no_such_board") == {"declared": False, "needs": ()}


def test_comovement_declares_the_corporate_actions_its_bar_reads_join():
    """#489: ``ohlc.py`` calls ``read.changed_at`` and ``read.bar_facts``, and both query ``corporate_action``. Since
    #642 PR 6 the board declares its reads itself and has left the plan."""
    assert "prices.actions" in boards.all_reads()["module_comovement"]
    assert "module_comovement" not in boards.UNDECLARED and "module_comovement" not in boards.PLANNED_READS


def test_relatedness_plans_the_universe_and_the_fundamentals_needs_it_reads():
    """#490: ``engine.py`` calls ``read.fundamental_companies`` and ``read.filing_texts``, so the board has a plan
    entry now instead of none, and the control board draws its lines. #400: the v3 profile's segment note reads
    ``read.segments`` too, the B input (WP6) ``read.customers``, and the prompt's name, sector and industry
    ``read.identity`` (10 Nov 2026). #642: its run's ETF scan calls ``read.etf_holding_tickers``."""
    want = ("identity.universe", "identity.security", "identity.etf_holdings", "fundamentals.companies", "fundamentals.filings",
            "fundamentals.segments", "fundamentals.customers")
    assert boards.PLANNED_READS["module_relatedness"] == want
    assert boards.reads_of("module_relatedness") == {"declared": False, "needs": want}


# ── #491: a board's planned reads are compared with the rcdata.read calls of its own code ─────────────────────────

def test_the_reader_map_names_the_catalog_tables_each_read_function_queries():
    tables = boards.reader_tables()
    assert tables["changed_at"] == {"price_bar_daily", "corporate_action"}, "the SQL text counts, not only _rows' table"
    assert tables["bar_facts"] == {"price_bar_daily", "corporate_action"}
    assert tables["filing_texts"] == {"filing_item"}
    assert tables["fundamental_companies"] == {"fundamental_company"}
    assert tables["market_series"] == {"market_series"}
    assert tables["watched"] == set(), "a watch list lives in ops.sqlite, which no need covers"


def test_the_call_scan_sees_every_import_form_and_leaves_tests_out(tmp_path, monkeypatch):
    pkg = tmp_path / "module_fake"
    (pkg / "tests").mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "a.py").write_text("from RevenueChaser.rcdata import read\n\ndef f():\n    return read.ticker_bars([])\n",
                              encoding="utf-8")
    (pkg / "b.py").write_text("def g():\n    from RevenueChaser.rcdata import read as rc\n    return rc.filing_texts([])\n",
                              encoding="utf-8")
    (pkg / "c.py").write_text("from RevenueChaser.rcdata.read import actions as acts\n\nX = acts\n", encoding="utf-8")
    (pkg / "d.py").write_text("read = object()\n\ndef h():\n    return read.peers(1)\n", encoding="utf-8")
    (pkg / "tests" / "test_x.py").write_text("from RevenueChaser.rcdata import read\nread.customers(1)\n",
                                             encoding="utf-8")
    monkeypatch.setattr(boards, "root", lambda: tmp_path)
    assert boards.read_calls("module_fake") == ("actions", "filing_texts", "ticker_bars")


def test_a_read_the_code_makes_and_the_plan_leaves_out_is_named_with_its_function(tmp_path, monkeypatch):
    pkg = tmp_path / "module_fake"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "a.py").write_text("from RevenueChaser.rcdata import read\nread.changed_at([])\nread.market_series([])\n",
                              encoding="utf-8")
    monkeypatch.setattr(boards, "root", lambda: tmp_path)
    monkeypatch.setattr(boards, "PLANNED_READS", {"module_fake": ("prices.bars", "macro.dgs10", "options.daily")})
    assert boards.unplanned_reads("module_fake") == (("changed_at", "corporate_action", ("prices.actions",)),)
    assert boards.unread_plans("module_fake") == ("options.daily",)


def test_a_needs_own_reader_is_covered_only_by_that_need_not_by_a_sibling_over_its_table(tmp_path, monkeypatch):
    """The review's 1: ``prices.market_cap`` and ``prices.latest`` share ``market_cap_daily``, so a table match let
    a board call ``read.latest_market`` with only ``prices.market_cap`` planned, and the diagram drew no line."""
    pkg = tmp_path / "module_fake"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "a.py").write_text("import RevenueChaser.rcdata.read as rd\nrd.latest_market([])\n", encoding="utf-8")
    monkeypatch.setattr(boards, "root", lambda: tmp_path)
    monkeypatch.setattr(boards, "PLANNED_READS", {"module_fake": ("prices.market_cap",)})
    assert ("latest_market", "market_cap_daily", ("prices.latest",)) in boards.unplanned_reads("module_fake")
    monkeypatch.setattr(boards, "PLANNED_READS", {"module_fake": ("prices.market_cap", "prices.latest", "prices.bars",
                                                                  "prices.security_ids")})
    assert boards.unplanned_reads("module_fake") == ()


def test_the_call_scan_sees_the_real_boards_reads_so_an_empty_scan_cannot_pass():
    """The review's 2: a scan that found nothing would make the board-wide test pass on every board."""
    assert {"changed_at", "bar_facts", "ticker_bars"} <= set(boards.read_calls("module_comovement"))
    assert {"fundamental_companies", "filing_texts"} <= set(boards.read_calls("module_relatedness"))


def test_every_board_not_migrated_yet_plans_every_need_its_code_reads():
    """#491: the plan is written by hand and drifted from the code (#483 found three boards with lines missing).
    Only the board's own package is scanned: a read made inside a shared ``core`` module is not counted here,
    because a board importing a core module may use none of that module's reads."""
    missing = {b: boards.unplanned_reads(b) for b in sorted(boards.UNDECLARED)}
    assert {b: m for b, m in missing.items() if m} == {}


def test_a_planned_need_the_code_never_reads_is_a_warning_not_a_failure():
    import warnings
    for board in sorted(boards.UNDECLARED):
        unread = boards.unread_plans(board)
        if unread:
            warnings.warn(f"{board}: planned but not read by its own package (maybe through core): "
                          f"{', '.join(unread)}", stacklevel=1)
