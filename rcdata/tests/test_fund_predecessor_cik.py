"""A company that got a new SEC CIK keeps its older statements (#749).

A reorganisation or a redomicile gives the new registrant a new CIK, and the SEC serves everything filed before the
change only under the old one: Exxon Mobil's successor file carries no annual year at all, the predecessor's
(CIK 34088) carries twenty (measured 12 Nov 2026, #749). The predecessor is fetched as a company of its own and its
older periods are served under the company's own CIK, with a guard per field and period type so the company's own
numbers are never replaced.

The pairs come from the seed file ``rcdata/build/successor_ciks.csv`` -- edgartools'
``edgar/reference/data/successor_registrants.csv`` at upstream ``479d350``, MIT -- read by ``build/cik_history.py``.
"""
from __future__ import annotations

from datetime import date

import duckdb
import pytest

from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.rcdata.build import cik_history
from RevenueChaser.rcdata.build.fundamentals import common, companyfacts, facts, tables

from . import fund_fixture as fx

XOM_OLD, XOM_NEW = "34088", "2115436"
XOM = {"ticker": "XOM", "cik": XOM_NEW, "name": "EXXON MOBIL CORP", "exchange": "NYSE", "tier": "A",
       "gvkey": "004503", "gsector": "10", "ggroup": "1010", "gind": "101020", "gsubind": "10102010",
       "sector": "Energy", "industry": "Energy"}


# ── The pairs ─────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_seed_holds_the_hand_reviewed_pairs_as_predecessor_to_successor():
    pairs = cik_history.seed()
    assert pairs[XOM_OLD] == XOM_NEW
    assert len(pairs) == 10 and all(p.isdigit() and s.isdigit() for p, s in pairs.items())


def test_a_company_that_moved_twice_lists_every_cik_upstream_of_it():
    chain = {"1": "2", "2": "3"}
    assert cik_history.successor("1", chain) == "3"
    assert cik_history.predecessors(["3"], chain) == {"3": ["1", "2"]}
    assert cik_history.former_ciks(["3"], chain) == {"1": "3", "2": "3"}


def test_a_loop_in_the_pairs_is_cut_instead_of_followed_for_ever():
    assert cik_history.successor("1", {"1": "2", "2": "1"}) == "2"


def test_a_predecessor_with_two_successors_is_left_out(tmp_path, monkeypatch):
    """An ambiguous pair would move a company's history to the wrong company, so neither reading is taken."""
    seed = tmp_path / "successor_ciks.csv"
    seed.write_text("successor_cik,predecessor_cik\n2,1\n3,1\n5,4\n", encoding="utf-8")
    monkeypatch.setattr(cik_history, "SEED_FILE", seed)
    cik_history.seed.cache_clear()
    try:
        assert cik_history.seed() == {"4": "5"}
    finally:
        cik_history.seed.cache_clear()


def test_the_universes_companies_carry_their_predecessors():
    fx.spine(rows=[XOM, *fx.COMPANIES])
    assert common.predecessor_ciks() == {XOM_NEW: [XOM_OLD]}


# ── The fetch and the served rows ─────────────────────────────────────────────────────────────────────────────

S1, P1, P2 = "0002115436-26-000001", "0000034088-25-000001", "0000034088-24-000001"


def _annual(concept, pairs, cik, name):
    """A companyfacts body: ``pairs`` of (value, period end, accession, filed, fiscal year)."""
    units = [fx._fact(v, end, accn, filed, "10-K", "FY", fy, f"{fy}-01-01") for v, end, accn, filed, fy in pairs]
    return {"cik": int(cik), "entityName": name, "facts": {"us-gaap": {concept: {"units": {"USD": units}}}}}


def _facts_bodies() -> dict[str, dict]:
    """The successor files one year; the predecessor files two older ones, the same year again (a restatement the
    guard must not take), and a field the successor never reported."""
    successor = _annual("Revenues", [(400.0, "2025-12-31", S1, "2026-02-10", 2025)], XOM_NEW, "EXXON MOBIL CORP")
    old = _annual("Revenues", [(300.0, "2024-12-31", P2, "2025-02-12", 2024),
                               (200.0, "2023-12-31", P2, "2025-02-12", 2023),
                               (999.0, "2025-12-31", P1, "2026-01-05", 2025)], XOM_OLD, "EXXON MOBIL CORP")
    old["facts"]["us-gaap"]["NetIncomeLoss"] = {"units": {"USD": [
        fx._fact(50.0, "2024-12-31", P2, "2025-02-12", "10-K", "FY", 2024, "2024-01-01")]}}
    return {XOM_NEW: successor, XOM_OLD: old}


@pytest.fixture
def sec(monkeypatch, http_mock):
    monkeypatch.setattr(sec_edgar, "SEC_WWW_BASE", "https://sec.test")
    monkeypatch.setattr(sec_edgar, "SEC_DATA_BASE", "https://data.sec.test")
    fx.spine(rows=[XOM])
    fx.sec_routes(http_mock, _facts_bodies())
    return http_mock


def _annual_revenue(cik: str = XOM_NEW) -> list[tuple]:
    return fx.q("SELECT period_end, value, accn FROM fundamental_fact WHERE cik = ? AND field = 'revenue' "
                "AND period_type = 'annual' ORDER BY period_end", cik)


def test_the_predecessors_file_is_read_as_well(sec):
    assert fx.run(companyfacts.NAME, mode="backfill", to="2026-10-28")["result"] == "changed"
    asked = sorted(str(c.url).rsplit("/", 1)[1] for c in sec.calls("companyfacts"))
    assert asked == ["CIK0000034088.json", "CIK0002115436.json"]


def test_the_older_years_are_served_under_the_companys_own_cik(sec):
    fx.run(companyfacts.NAME, mode="backfill", to="2026-10-28")
    assert _annual_revenue() == [(date(2023, 12, 31), 200.0, P2), (date(2024, 12, 31), 300.0, P2),
                                 (date(2025, 12, 31), 400.0, S1)]


def test_a_field_the_company_never_filed_itself_comes_whole_from_the_predecessor(sec):
    fx.run(companyfacts.NAME, mode="backfill", to="2026-10-28")
    assert fx.q("SELECT period_end, value FROM fundamental_fact WHERE cik = ? AND field = 'net_income' "
                "AND period_type = 'annual'", XOM_NEW) == [(date(2024, 12, 31), 50.0)]


def test_the_predecessors_rows_stay_under_its_own_cik_too(sec):
    """Nothing is moved: the old CIK's own rows are untouched, so the copy can be dropped by building again."""
    fx.run(companyfacts.NAME, mode="backfill", to="2026-10-28")
    assert [r[0] for r in _annual_revenue(XOM_OLD)] == [date(2023, 12, 31), date(2024, 12, 31), date(2025, 12, 31)]


def test_a_second_build_does_not_double_the_copied_rows(sec):
    fx.run(companyfacts.NAME, mode="backfill", to="2026-10-28")
    fx.run(companyfacts.NAME, mode="backfill", to="2026-10-29")
    assert len(_annual_revenue()) == 3


# ── The guard, on its own ─────────────────────────────────────────────────────────────────────────────────────

def _con(served: list[dict], staged: list[dict]) -> duckdb.DuckDBPyConnection:
    """A connection holding a served ``fundamental_fact`` and the companyfacts staging, both typed."""
    from RevenueChaser.rcdata.build import base
    blank = {c: None for c, _ in facts.FACT}
    con = duckdb.connect()
    tables.load(con, "fundamental_fact", [{**blank, **r} for r in served])
    base.rows_table(con, companyfacts.STAGING["facts"], facts.FACT, [{**blank, **r} for r in staged])
    return con


def _row(cik, field, end, value, period_type="annual"):
    return {"cik": cik, "field": field, "period_type": period_type, "period_end": end, "value": value,
            "derived": False, "preliminary": False, "source": facts.SOURCE}


def test_the_guard_takes_only_periods_before_the_companys_own_earliest(monkeypatch):
    monkeypatch.setattr(common, "predecessor_ciks", lambda *a, **k: {XOM_NEW: [XOM_OLD]})
    con = _con(served=[_row(XOM_NEW, "revenue", date(2025, 12, 31), 400.0)],
               staged=[_row(XOM_OLD, "revenue", date(2025, 12, 31), 999.0),
                       _row(XOM_OLD, "revenue", date(2024, 12, 31), 300.0)])
    try:
        assert tables._predecessor_facts(con) == 1
        assert con.execute("SELECT period_end, value FROM fundamental_fact WHERE cik = ? ORDER BY period_end",
                           [XOM_NEW]).fetchall() == [(date(2024, 12, 31), 300.0), (date(2025, 12, 31), 400.0)]
    finally:
        con.close()


def test_a_quarter_never_fills_an_annual_gap(monkeypatch):
    """The guard is per field *and* period type: a predecessor quarter is judged against the company's quarters."""
    monkeypatch.setattr(common, "predecessor_ciks", lambda *a, **k: {XOM_NEW: [XOM_OLD]})
    con = _con(served=[_row(XOM_NEW, "revenue", date(2025, 3, 31), 90.0, "quarterly")],
               staged=[_row(XOM_OLD, "revenue", date(2024, 12, 31), 300.0),
                       _row(XOM_OLD, "revenue", date(2024, 12, 31), 80.0, "quarterly")])
    try:
        assert tables._predecessor_facts(con) == 2
        assert con.execute("SELECT period_type, count(*) FROM fundamental_fact WHERE cik = ? GROUP BY 1 ORDER BY 1",
                           [XOM_NEW]).fetchall() == [("annual", 1), ("quarterly", 2)]
    finally:
        con.close()


def test_a_company_with_no_pair_is_left_exactly_as_it_was(monkeypatch):
    monkeypatch.setattr(common, "predecessor_ciks", lambda *a, **k: {})
    con = _con(served=[_row("789019", "revenue", date(2025, 6, 30), 281_000.0)], staged=[])
    try:
        assert tables._predecessor_facts(con) == 0
        assert con.execute("SELECT count(*) FROM fundamental_fact").fetchone()[0] == 1
    finally:
        con.close()
