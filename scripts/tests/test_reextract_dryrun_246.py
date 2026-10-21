"""#246 dry-run script ``scripts/research/246_reextract_dryrun.py``, offline.

The script re-extracts every EDGAR filer twice from ONE companyfacts payload (the pick alone, then #246's
backfill) and reports what changed. These tests feed it synthetic companyfacts through a fake fetcher: no
SEC request, no store write, no key. The real fetch (``sec_edgar._fetch_facts``) is replaced by a function
that fails the test if it is ever called.

``CONCEPTS["revenue"]`` lists, in order: ``Revenues``, ``RevenueFromContractWithCustomerExcludingAssessedTax``,
``SalesRevenueNet``. Series values are ``base + year``, so a value names the tag that supplied it.
"""
from __future__ import annotations

import copy
import csv
import importlib.util
import json
import os
import random
import runpy
import sys
import types
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.core.providers import sec_edgar as se

_SCRIPT = Path(__file__).resolve().parents[1] / "research" / "246_reextract_dryrun.py"

pytestmark = pytest.mark.skipif(not _SCRIPT.exists(),
                                reason=f"{_SCRIPT.name} is not in this tree")

REV = "Revenues"
RFC = "RevenueFromContractWithCustomerExcludingAssessedTax"
SALES = "SalesRevenueNet"
GP = se.CONCEPTS["gross_profit"][0][0]
CASH = se.CONCEPTS["cash"][0][0]
ASSETS = se.CONCEPTS["total_assets"][0][0]
LT_DEBT = se.CONCEPTS["lt_debt"][0][0]
LT_DEBT_CURRENT = se.CONCEPTS["lt_debt_current"][0][0]
LT_DEBT_CURRENT_ALT = se.CONCEPTS["lt_debt_current"][0][1]
CONVERTIBLE = se.CONCEPTS["convertible_debt_noncurrent"][0][0]
OP_LEASE = se.CONCEPTS["operating_lease_noncurrent"][0][0]
STI = se._STI_TOTAL_TAGS[0]


@pytest.fixture(scope="module")
def dry():
    # The file name starts with a digit, so it cannot be imported by name.
    spec = importlib.util.spec_from_file_location("dryrun_246", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def real_contact(monkeypatch):
    """The script refuses the placeholder User-Agent; every test but the refusal ones has a real-looking contact."""
    monkeypatch.setenv("RC_SEC_USER_AGENT", "RevenueChaser dry-run tests (contact: tests@revenuechaser.test)")


@pytest.fixture(autouse=True)
def no_sec(monkeypatch):
    """The only egress of the script is ``sec_edgar._fetch_facts`` (and the CIK map): never reached."""
    def boom(*_a, **_k):
        raise AssertionError("the dry-run tests must not reach SEC")
    monkeypatch.setattr(se, "_fetch_facts", boom)
    monkeypatch.setattr(se, "_cik_map", boom)


def _flow(end: str, val: float) -> dict:
    start = (date.fromisoformat(end) - timedelta(days=364)).isoformat()
    return {"start": start, "end": end, "val": val, "form": "10-K", "fp": "FY",
            "filed": f"{int(end[:4]) + 1}-03-01"}


def _instant(end: str, val: float, fp: str = "FY") -> dict:
    return {"end": end, "val": val, "form": "10-K", "fp": fp, "filed": f"{int(end[:4]) + 1}-03-01"}


def _facts(flow: dict | None = None, inst: dict | None = None) -> dict:
    """``{concept: {end: val}}`` -> companyfacts (flow facts carry a ``start``, instants do not)."""
    gaap = {name: {"units": {"USD": [_flow(e, v) for e, v in series.items()]}}
            for name, series in (flow or {}).items()}
    gaap.update({name: {"units": {"USD": [_instant(e, v) for e, v in series.items()]}}
                 for name, series in (inst or {}).items()})
    return {"entityName": "Synthetic Co", "facts": {"us-gaap": gaap}}


def _s(years, base: float) -> dict:
    return {f"{y}-12-31": float(base + y) for y in years}


# One shape per fixture: the lost history and why it was (not) restored.
CHAIN = _facts(flow={SALES: _s(range(2017, 2026), 10000), REV: _s([2026], 20000)})
HOLE = _facts(flow={REV: _s([2023, 2024, 2026], 20000), SALES: _s([2025], 10000)})
GAP = _facts(flow={SALES: _s(range(2017, 2025), 10000), REV: _s([2026], 20000)})      # KTCC: 2025 in neither
GUARD = _facts(flow={REV: _s([2025, 2026], 20000),
                     SALES: {"2024-06-30": 10024.0, "2023-06-30": 10023.0}})          # year end moved
TIE = _facts(flow={REV: _s([2025, 2026], 10000), RFC: _s([2025, 2026], 20000)})        # equal series: list order
LONGER = _facts(flow={REV: _s([2026], 20000), RFC: _s([2025, 2026], 10000)})           # same newest year: the longer
NEWER_LATER = _facts(flow={REV: _s(range(2017, 2026), 10000), RFC: _s([2026], 20000)})  # newest year wins over length
PLAIN = _facts(flow={REV: _s(range(2020, 2027), 10000)})
FIXTURES = {"chain": CHAIN, "hole": HOLE, "gap": GAP, "guard": GUARD, "tie": TIE, "longer": LONGER,
            "newer_later": NEWER_LATER, "plain": PLAIN}


def _run(dry, facts_by_ticker: dict, tmp_path: Path, *, put_row=None, census_names=None, **kw) -> dict:
    return dry.run(sorted(facts_by_ticker), fetch=lambda t: facts_by_ticker[t], put_row=put_row,
                   out_dir=tmp_path / "out", census_names=census_names if census_names is not None else {}, **kw)


def _rows(tmp_path: Path, name: str) -> list[dict]:
    with (tmp_path / "out" / name).open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


# ── the guard ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/home/user/RevenueChaser/.store", r"C:\projects\Github\RevenueChaser\.store",
                                  "/tmp/rc246", "/tmp/rc246_other/.store"])
def test_the_guard_refuses_anything_but_a_marked_copy(dry, path, capsys):
    with pytest.raises(SystemExit) as exc:
        dry.assert_store_copy(path)
    assert exc.value.code == 2
    assert "REFUSING" in capsys.readouterr().err


@pytest.mark.parametrize("path", ["/tmp/rc246_dry/.store", r"C:\Users\x\AppData\RC246_DRY\.store",
                                  "/data/copy_rc246_dry_2026-10-03"])
def test_the_guard_accepts_a_marked_copy_in_any_case(dry, path):
    assert dry.assert_store_copy(path) is None


def test_main_refuses_the_live_store_before_any_fetch(dry, monkeypatch, tmp_path):
    monkeypatch.setattr(rawstore, "DB_PATH", tmp_path / ".store" / "raw.duckdb")
    out = tmp_path / "out"
    with pytest.raises(SystemExit) as exc:
        dry.main(["--tickers", "AAA", "--out", str(out)])
    assert exc.value.code == 2
    assert not out.exists()                                    # nothing was started, nothing written


def test_main_on_a_copy_fetches_each_cik_once_and_writes_no_store_row_with_no_write(dry, monkeypatch, tmp_path):
    monkeypatch.setattr(rawstore, "DB_PATH", tmp_path / "rc246_dry" / ".store" / "raw.duckdb")
    fetched = []
    monkeypatch.setattr(se, "_cik_map", lambda: {"AAA": {"cik": 11}, "BBB": {"cik": 22}})
    monkeypatch.setattr(se, "_fetch_facts", lambda cik: fetched.append(cik) or CHAIN)
    monkeypatch.setattr(dry.rawstore, "write_rows",
                        lambda *a, **k: pytest.fail("--no-write must not write the store"))
    summary = dry.main(["--tickers", "bbb, aaa,zzz", "--no-write", "--out", str(tmp_path / "out")])
    assert fetched == [11, 22]                                 # sorted tickers, one companyfacts each
    assert summary["status"] == {"reextracted": 2, "no_cik": 1}
    assert summary["sec"]["calls"] == 2                        # a ticker without a CIK is no SEC call
    assert (tmp_path / "out" / "summary.json").exists()


def test_main_limit_keeps_the_first_tickers_in_sorted_order(dry, monkeypatch, tmp_path):
    monkeypatch.setattr(rawstore, "DB_PATH", tmp_path / "rc246_dry" / ".store" / "raw.duckdb")
    monkeypatch.setattr(se, "_cik_map", lambda: {"AAA": {"cik": 11}, "BBB": {"cik": 22}, "CCC": {"cik": 33}})
    fetched = []
    monkeypatch.setattr(se, "_fetch_facts", lambda cik: fetched.append(cik) or PLAIN)
    dry.main(["--tickers", "CCC,AAA,BBB", "--limit", "2", "--no-write", "--out", str(tmp_path / "out")])
    assert fetched == [11, 22]


def test_main_writes_one_edgar_financials_row_per_ticker_into_the_copy(dry, monkeypatch, tmp_path):
    monkeypatch.setattr(rawstore, "DB_PATH", tmp_path / "rc246_dry" / ".store" / "raw.duckdb")
    monkeypatch.setattr(se, "_cik_map", lambda: {"AAA": {"cik": 11}})
    monkeypatch.setattr(se, "_fetch_facts", lambda cik: CHAIN)
    written = []
    monkeypatch.setattr(dry.rawstore, "write_rows", lambda name, rows: written.append((name, rows)))
    dry.main(["--tickers", "AAA", "--out", str(tmp_path / "out")])
    [(name, rows)] = written
    assert name == "edgar_financials"
    [row] = rows
    assert row["ticker"] == "AAA" and row["annual"] == se.extract_annual(CHAIN)
    assert row["meta"]["source"] == "sec_edgar" and row["meta"]["available"] is True
    assert row["fetched_at"] > 0                               # stamped: the row is a fresh fetch


# ── the two extractions ─────────────────────────────────────────────────────

def _revenues(annual: list[dict]) -> dict:
    return {r["fiscal_year"]: r["revenue"] for r in annual if r["revenue"] is not None}


def test_before_is_the_pick_alone_and_the_real_function_is_put_back(dry):
    real = se._annual_facts
    before = dry.extract_annual_before_246(CHAIN)
    assert se._annual_facts is real
    assert _revenues(before) == {"2026": 22026.0}                       # the pick: the newest tag only
    assert set(_revenues(se.extract_annual(CHAIN))) == {str(y) for y in range(2017, 2027)}


def test_the_real_function_is_put_back_when_extraction_raises(dry, monkeypatch):
    real = se._annual_facts
    def boom(facts):
        raise RuntimeError("boom")

    monkeypatch.setattr(se, "extract_annual", boom)
    with pytest.raises(RuntimeError):
        dry.extract_annual_before_246(CHAIN)
    assert se._annual_facts is real


@pytest.mark.parametrize("name, added", [
    ("chain", [str(y) for y in range(2017, 2026)]), ("hole", ["2025"]), ("gap", []), ("guard", []),
    ("tie", []), ("longer", []), ("newer_later", [str(y) for y in range(2017, 2026)]), ("plain", [])])
def test_after_only_adds_to_before_on_every_shape(dry, name, added):
    facts = FIXTURES[name]
    diff = dry.diff_extractions(dry.extract_annual_before_246(facts), se.extract_annual(facts))
    assert diff["changed"] == [] and diff["unexplained"] == []
    assert diff["added_years"] == added
    assert sorted(diff["added"].get("revenue", [])) == added


def test_the_longer_of_two_series_ending_in_the_same_year_is_the_pick_in_both_extractions(dry):
    before = dry.extract_annual_before_246(LONGER)
    assert _revenues(before) == {"2025": 12025.0, "2026": 12026.0}
    assert _revenues(se.extract_annual(LONGER)) == _revenues(before)


def test_an_equal_series_keeps_the_first_tag_in_both_extractions(dry):
    before = dry.extract_annual_before_246(TIE)
    assert _revenues(before) == {"2025": 12025.0, "2026": 12026.0}
    assert _revenues(se.extract_annual(TIE)) == _revenues(before)


def _random_filer(rng: random.Random) -> dict:
    """Every mapped field, with several tags per field, ragged and gappy years, 52/53-week and June year
    ends, and the odd negative figure."""
    gaap = {}
    instants = ({"total_assets", "total_equity", "cash", "lt_debt", "lt_debt_current", "receivables", "inventory",
                 "payables", "short_term_investments", "marketable_securities_noncurrent", "long_term_investments"}
                | se._DEBT_COMPONENT_FIELDS)
    for field, (tags, unit) in se.CONCEPTS.items():
        if rng.random() < 0.35:
            continue
        if field == "short_term_investments":
            tags = se._STI_TOTAL_TAGS + se._STI_AFS_TAGS + se._STI_HTM_TAGS
        for tag in tags:
            if rng.random() < 0.5:
                continue
            first, series = rng.randint(2012, 2020), []
            for year in range(first, first + rng.randint(1, 10)):
                if rng.random() < 0.15:
                    continue
                roll = rng.random()
                end = (f"{year}-06-30" if roll < 0.08 else f"{year}-01-03" if roll < 0.14
                       else f"{year}-12-27" if roll < 0.30 else f"{year}-12-31")
                val = float(rng.randint(1, 10 ** 6)) if rng.random() < 0.97 else -3.0
                series.append(_instant(end, val) if field in instants else _flow(end, val))
            if series:
                gaap.setdefault(tag, {"units": {unit: []}})["units"][unit].extend(series)
    return {"entityName": "Random Co", "facts": {"us-gaap": gaap}}


def test_property_the_after_extraction_changes_nothing_held_and_no_verdict_is_lost_or_contradicted(dry):
    extended = 0
    for seed in range(300):
        facts = _random_filer(random.Random(seed))
        before, after = dry.extract_annual_before_246(facts), se.extract_annual(facts)
        diff = dry.diff_extractions(before, after)
        assert diff["changed"] == [] and diff["unexplained"] == [], (seed, diff["changed"][:3], diff["unexplained"][:3])
        extended += bool(diff["added_years"] or diff["added"])
        if after:
            verdicts = dry.verdict_changes("X", before, after, se.extract_quarterly(facts))
            classes = {r["class"] for r in verdicts["rows"]}
            assert classes <= {"restoration", "hole_fill", "stale_margin", "stale_margin_shift", "record_insertion",
                               "zero_revenue_base"}, (seed, verdicts["rows"])
            # #386 F2: Quality reads the anchor year, which #246 may fill; only a restoration is allowed there.
            assert not [r for r in verdicts["rows"] if r["metric"].startswith("quality_")
                        and r["class"] != "restoration"], seed
    assert extended > 60            # the property is not vacuous: #246 extends a good share of these filers


# ── the diff ────────────────────────────────────────────────────────────────

def _tampered(dry, mutate, facts=HOLE):
    old = dry.extract_annual_before_246(facts)
    new = copy.deepcopy(se.extract_annual(facts))
    mutate(new)
    return dry.diff_extractions(old, new)


def _rec(annual: list[dict], year: str) -> dict:
    return next(r for r in annual if r["fiscal_year"] == year)


def test_a_moved_value_is_a_changed_existing(dry):
    diff = _tampered(dry, lambda a: _rec(a, "2024").update(revenue=1.0))
    assert diff["changed"] == [{"year": "2024", "field": "revenue", "old": 22024.0, "new": 1.0}]


def test_a_moved_end_date_with_the_same_value_is_a_changed_existing(dry):
    diff = _tampered(dry, lambda a: _rec(a, "2024")["period_ends"].update(revenue="2024-12-30"))
    assert diff["changed"] == [{"year": "2024", "field": "revenue.end", "old": "2024-12-31", "new": "2024-12-30"}]


def test_a_vanished_value_and_a_vanished_year_are_changed_existing(dry):
    gone = _tampered(dry, lambda a: _rec(a, "2024").update(revenue=None))
    assert gone["changed"] == [{"year": "2024", "field": "revenue", "old": 22024.0, "new": None}]
    dropped = _tampered(dry, lambda a: a.remove(_rec(a, "2023")))
    assert dropped["changed"] == [{"year": "2023", "field": "*", "old": "year record", "new": None}]


def test_a_short_term_investment_that_appears_with_a_restored_cash_is_an_addition_not_a_change(dry):
    # 2024 has a record (revenue) but no cash; the second cash tag restores it, and the investment follows.
    cash_first, cash_second = se.CONCEPTS["cash"][0][:2]
    facts = _facts(flow={REV: _s([2024, 2025, 2026], 10000)},
                   inst={cash_first: _s([2025, 2026], 100), cash_second: _s([2024], 100), STI: _s([2024, 2025, 2026], 10)})
    old, new = dry.extract_annual_before_246(facts), se.extract_annual(facts)
    assert _rec(old, "2024")["short_term_investments"] is None and _rec(new, "2024")["short_term_investments"] == 2034.0
    diff = dry.diff_extractions(old, new)
    assert diff["changed"] == [] and sorted(diff["added"]["short_term_investments"]) == ["2024"]


def test_a_moved_short_term_investments_flag_is_a_changed_existing(dry):
    facts = _facts(flow={REV: _s([2025, 2026], 10000)},
                   inst={CASH: _s([2025, 2026], 100), STI: _s([2025, 2026], 10)})
    diff = _tampered(dry, lambda a: _rec(a, "2026").update(short_term_investments_basis="parts"), facts)
    assert diff["changed"] == [{"year": "2026", "field": "short_term_investments_basis",
                                "old": "total", "new": "parts"}]


def test_a_total_debt_that_moved_without_a_new_component_is_unexplained(dry):
    facts = _facts(flow={REV: _s([2025, 2026], 10000)}, inst={LT_DEBT: _s([2025, 2026], 100)})
    diff = _tampered(dry, lambda a: _rec(a, "2026").update(total_debt=5.0), facts)
    assert diff["unexplained"] == [{"year": "2026", "field": "total_debt", "old": 2126.0, "new": 5.0}]


def test_a_total_debt_that_gained_a_newly_added_component_is_explained(dry):
    # The 2024 current portion is reported under the second tag only: #246 adds it to the existing record.
    facts = _facts(flow={REV: _s([2024, 2025, 2026], 10000)},
                   inst={LT_DEBT: _s([2024, 2025, 2026], 100),
                         LT_DEBT_CURRENT: _s([2025, 2026], 7), LT_DEBT_CURRENT_ALT: _s([2024], 3)})
    old, new = dry.extract_annual_before_246(facts), se.extract_annual(facts)
    assert _rec(old, "2024")["total_debt"] == 2124.0 and _rec(new, "2024")["total_debt"] == 2124.0 + 2027.0
    diff = dry.diff_extractions(old, new)
    assert diff["unexplained"] == [] and diff["changed"] == []         # the total moved, a new component explains it
    assert diff["added"]["lt_debt_current"] == ["2024"]
    assert diff["total_debt_moves"] == [{"year": "2024", "old": 2124.0, "new": 4151.0, "added": "lt_debt_current",
                                         "newest_record": False}]


def test_a_total_debt_that_moves_with_a_backfilled_current_portion_is_listed_and_the_newest_one_never_is(dry, tmp_path):
    facts = _facts(flow={REV: _s([2021, 2022, 2023, 2024], 10000)},
                   inst={LT_DEBT: _s([2021, 2022, 2023, 2024], 100),
                         LT_DEBT_CURRENT: _s([2023, 2024], 7), LT_DEBT_CURRENT_ALT: _s([2021, 2022], 3)})
    summary = _run(dry, {"DBT": facts}, tmp_path)
    assert [(r["year"], r["old"], r["new"], r["added"], r["newest_record"])
            for r in _rows(tmp_path, "total_debt_moves.csv")] == [
        ("2021", "2121.0", "4145.0", "lt_debt_current", "False"), ("2022", "2122.0", "4147.0", "lt_debt_current", "False")]
    data = summary["data"]
    assert (data["total_debt_moves"], data["total_debt_moves_tickers"], data["total_debt_moves_newest_record"]) == (2, 1, 0)
    assert data["acceptance"] == "PASS"               # a listed move is no failure; a move of the newest record is


def test_a_move_of_the_newest_records_total_debt_fails_the_acceptance(dry, monkeypatch, tmp_path):
    facts = _facts(flow={REV: _s([2025, 2026], 10000)},
                   inst={LT_DEBT: _s([2025, 2026], 100), LT_DEBT_CURRENT: _s([2025, 2026], 7)})
    real = dry.extract_annual_before_246

    def disagreeing(f):
        annual = real(f)
        annual[-1].update(lt_debt_current=None, total_debt=annual[-1]["lt_debt"])    # as if the newest gained it
        return annual

    monkeypatch.setattr(dry, "extract_annual_before_246", disagreeing)
    summary = _run(dry, {"AAA": facts}, tmp_path)
    assert summary["data"]["total_debt_moves_newest_record"] == 1 and summary["data"]["acceptance"] == "FAIL"
    assert [r["newest_record"] for r in _rows(tmp_path, "total_debt_moves.csv")] == ["True"]


# ── the verdicts ────────────────────────────────────────────────────────────

def test_classify_names_every_kind(dry):
    c = dry.classify
    assert c("rev_growth", None, 0.5) == "restoration"
    assert c("rev_growth", 0.1, 0.5) == "hole_fill"
    assert c("rev_growth", 0.5, None) == "lost"
    assert c("gross_margin", None, 0.4) == "stale_margin"
    assert c("gross_margin", 0.3, 0.4) == "unexpected_margin_change"
    assert c("gross_margin", 0.4, None) == "lost"
    assert c("rev_growth", 0.5, 0.5) is None and c("gross_margin", None, None) is None
    # the Fundamentals quality block reads the anchor year since #386 F2: #246 may fill a hole there (a restoration),
    # but a held value that moves or disappears is wrong
    assert c("quality_roic", 0.1, 0.2) == c("quality_roic", 0.1, None) == "unexpected_quality_change"
    assert c("quality_roic", None, 0.1) == "restoration"
    assert c("quality_roic", 0.1, 0.1) is None
    # the AG net debt reads the newest non-null total_debt, which a backfilled current portion never reaches
    assert c("net_debt_assets", 0.1, 0.2) == c("net_debt_assets", None, 0.1) == "unexpected_debt_change"


def test_a_restored_history_is_a_restoration_and_flips_the_growth_gates(dry, tmp_path):
    summary = _run(dry, {"AAA": CHAIN}, tmp_path)
    rows = {r["metric"]: r for r in _rows(tmp_path, "verdict_changes.csv")}
    assert {m: r["class"] for m, r in rows.items()} == {
        "rev_growth": "restoration", "rev_growth_last": "restoration", "rev_growth_ttm": "restoration"}
    assert float(rows["rev_growth"]["new"]) == pytest.approx(22026 / 12025 - 1) and rows["rev_growth"]["old"] == ""
    assert {(r["gate"], r["old"], r["new"]) for r in _rows(tmp_path, "gate_flips.csv")} == {
        ("rev_growth", "", "True"), ("accelerating", "", "True")}   # unknown before the history came back (#151)
    assert summary["verdicts"]["gate_flips"] == {"rev_growth": {"fail_to_pass": 1},
                                                 "accelerating": {"fail_to_pass": 1}}
    assert summary["verdicts"]["must_be_zero"] == {"lost": 0, "unexpected_margin_change": 0,
                                                   "unexpected_quality_change": 0, "unexpected_debt_change": 0}
    assert summary["verdicts"]["named_losses"] == {"record_insertion": 0, "zero_revenue_base": 0}


def test_a_filled_hole_gives_the_one_year_figure_back_where_the_two_year_figure_is_no_longer_shown(dry, tmp_path):
    _run(dry, {"AAA": HOLE}, tmp_path)
    rows = {r["metric"]: r for r in _rows(tmp_path, "verdict_changes.csv")}
    # 2026 against 2024 is no YoY figure (#150 U8): before the fill the board showed none, after it 2026 against 2025
    assert rows["rev_growth"]["old"] == "" and float(rows["rev_growth"]["new"]) == pytest.approx(22026 / 12025 - 1)
    assert rows["rev_growth"]["class"] == "restoration" and rows["rev_growth_last"]["class"] == "restoration"


def test_an_unaffected_filer_reports_nothing(dry, tmp_path):
    summary = _run(dry, {"AAA": PLAIN, "BBB": GAP, "CCC": GUARD}, tmp_path)
    for name in ("verdict_changes.csv", "gate_flips.csv", "added_values.csv", "changed_existing.csv",
                 "stale_margins.csv"):
        assert _rows(tmp_path, name) == [], name
    assert summary["data"]["acceptance"] == "PASS" and summary["data"]["tickers_with_added_years"] == 0


# A margin that appears only because the history came back: the newest year has no gross profit.
STALE = _facts(flow={SALES: _s(range(2017, 2026), 10000), REV: _s([2026], 20000),
                     GP: _s(range(2017, 2026), 9000)})


def test_a_margin_of_an_older_year_is_never_shown_so_a_restored_history_reports_no_stale_margin(dry, tmp_path):
    """#150 U8: the score reads one fiscal year, so a gross profit that only an older, restored year has changes
    nothing on the board (before, it appeared as a stale margin)."""
    summary = _run(dry, {"STL": STALE}, tmp_path)
    assert _rows(tmp_path, "stale_margins.csv") == []
    assert [r for r in _rows(tmp_path, "verdict_changes.csv") if r["metric"] == "gross_margin"] == []
    assert summary["verdicts"]["stale_margin_names"] == 0 and summary["verdicts"]["stale_margin_kinds"] == {}


# 2019 revenue comes back from the second tag, so its cost (same date) derives a gross profit for it: the margin
# AG shows (the newest year that has one) moves from FY2018 to FY2019, both long before the newest record FY2021.
SHIFT = _facts(flow={REV: _s([2018, 2020, 2021], 20000), RFC: _s([2019], 10000),
                     se.CONCEPTS["cost_of_revenue"][0][0]: _s([2018, 2019], 1000)})


def test_a_margin_that_moves_between_old_years_changes_nothing_on_the_board(dry, tmp_path):
    summary = _run(dry, {"SHF": SHIFT}, tmp_path)
    assert [r for r in _rows(tmp_path, "verdict_changes.csv") if r["metric"] == "gross_margin"] == []
    assert _rows(tmp_path, "stale_margins.csv") == []
    assert summary["verdicts"]["stale_margin_kinds"] == {}
    assert summary["verdicts"]["must_be_zero"]["unexpected_margin_change"] == 0


def _hand(year: str, gp: float | None, rev: float = 100.0, **more) -> dict:
    return {"fiscal_year": year, "revenue": rev, "gross_profit": gp, **more}


def test_a_margin_of_the_newest_record_is_a_restoration_and_a_changed_value_for_one_year_is_flagged(dry):
    appeared = dry.verdict_changes("X", [_hand("2025", None)], [_hand("2025", 50.0)], [])
    assert [(r["metric"], r["class"]) for r in appeared["rows"] if r["metric"] == "gross_margin"] == [
        ("gross_margin", "restoration")]
    moved = dry.verdict_changes("X", [_hand("2025", 40.0)], [_hand("2025", 50.0)], [])
    assert [(r["metric"], r["class"]) for r in moved["rows"] if r["metric"] == "gross_margin"] == [
        ("gross_margin", "unexpected_margin_change")]
    # the board no longer shows an older year's margin (#150 U8), so the newest record's own margin is a restoration
    newer = dry.verdict_changes("X", [_hand("2024", 40.0), _hand("2025", None)],
                                [_hand("2024", 40.0), _hand("2025", 60.0)], [])
    assert [r["class"] for r in newer["rows"] if r["metric"] == "gross_margin"] == ["restoration"]


# ── Fundamentals: growth reads the last two records, the quality block the newest one ───────────────

def test_ro40_follows_a_restored_growth(dry, tmp_path):
    ocf, capex = se.CONCEPTS["operating_cash_flow"][0][0], se.CONCEPTS["capex"][0][0]
    facts = copy.deepcopy(CHAIN)
    facts["facts"]["us-gaap"][ocf] = {"units": {"USD": [_flow("2026-12-31", 5026.0)]}}
    facts["facts"]["us-gaap"][capex] = {"units": {"USD": [_flow("2026-12-31", 3026.0)]}}
    _run(dry, {"AAA": facts}, tmp_path)
    rows = {r["metric"]: r for r in _rows(tmp_path, "verdict_changes.csv")}
    assert rows["ro40"]["class"] == "restoration" and rows["ro40"]["old"] == ""
    assert float(rows["ro40"]["new"]) == pytest.approx((22026 / 12025 - 1 + 2000 / 22026) * 100)


def test_a_quality_metric_that_moves_is_reported_and_counted_as_must_be_zero(dry, monkeypatch, tmp_path):
    facts = _facts(flow={REV: _s([2025, 2026], 10000)}, inst={se.CONCEPTS["total_equity"][0][0]: _s([2025, 2026], 100)})
    real = dry.extract_annual_before_246

    def disagreeing(f):
        annual = real(f)
        annual[-1]["total_equity"] = 1.0
        return annual

    monkeypatch.setattr(dry, "extract_annual_before_246", disagreeing)
    summary = _run(dry, {"AAA": facts}, tmp_path)
    assert [(r["metric"], r["class"]) for r in _rows(tmp_path, "verdict_changes.csv")
            if r["metric"].startswith("quality_")] == [("quality_total_equity", "unexpected_quality_change")]
    assert summary["verdicts"]["must_be_zero"]["unexpected_quality_change"] == 1


# ── #187: short-term investments ────────────────────────────────────────────

# 300 of debt against 1000 of assets: net debt 20% on cash alone, 5% once the 150 of investments count.
LIQUID = _facts(flow={REV: _s([2025, 2026], 10000)},
                inst={ASSETS: {"2025-12-31": 1000.0, "2026-12-31": 1000.0},
                      LT_DEBT: {"2025-12-31": 300.0, "2026-12-31": 300.0},
                      CASH: {"2025-12-31": 100.0, "2026-12-31": 100.0},
                      STI: {"2025-12-31": 150.0, "2026-12-31": 150.0}})


def test_short_term_investments_that_flip_the_net_debt_gate_are_counted_against_cash_only(dry, tmp_path):
    summary = _run(dry, {"LIQ": LIQUID}, tmp_path)
    assert _rows(tmp_path, "sti_flips.csv") == [
        {"ticker": "LIQ", "gate": "net_debt_assets", "cash_only": "False", "with_sti": "True"}]
    sti = summary["187_sti"]
    assert sti["cash_records"] == 1 and sti["with_sti"] == 1 and sti["basis"] == {"total": 1}
    assert sti["liquidity_basis"] == {"cash+sti": 1}
    assert sti["gate_flips_vs_cash_only"] == {"net_debt_assets:fail_to_pass": 1}


def test_a_negative_investment_is_suspect_and_never_counted(dry, tmp_path):
    facts = copy.deepcopy(LIQUID)
    for fact in facts["facts"]["us-gaap"][STI]["units"]["USD"]:
        fact["val"] = -150.0
    summary = _run(dry, {"NEG": facts}, tmp_path)
    sti = summary["187_sti"]
    assert sti["with_sti"] == 0 and sti["suspect"] == 1 and sti["liquidity_basis"] == {"cash_only": 1}
    assert _rows(tmp_path, "sti_flips.csv") == []


# F1: a non-revenue field fills a year that had no record. Revenue 2024 and 2026, cash 2024 and 2026, and the
# 2025 cash only under the second tag: the new 2025 record has no revenue, and Fundamentals compares the last two RECORDS.
INSERTION = _facts(flow={REV: {"2024-12-31": 100.0, "2026-12-31": 130.0}},
                   inst={se.CONCEPTS["cash"][0][0]: {"2024-12-31": 5.0, "2026-12-31": 6.0},
                         se.CONCEPTS["cash"][0][1]: {"2025-12-31": 7.0}})
# F2: the filled year reports a revenue of 0 (a pre-revenue filer), and growth divides by it.
ZERO_BASE = _facts(flow={REV: {"2024-12-31": 100.0, "2026-12-31": 130.0}, SALES: {"2025-12-31": 0.0}})


def test_a_record_inserted_between_the_last_two_changes_no_verdict_now_that_growth_needs_adjacent_years(dry, tmp_path):
    """#148/#150: growth is the newest year with revenue against the year exactly before it. 2026 against 2024 was
    shown before (and a record for 2025 without revenue used to take that figure away); now neither is shown."""
    summary = _run(dry, {"INS": INSERTION}, tmp_path)
    assert _rows(tmp_path, "verdict_changes.csv") == []
    assert summary["verdicts"]["named_losses"] == {"record_insertion": 0, "zero_revenue_base": 0}
    assert summary["verdicts"]["must_be_zero"]["lost"] == 0


def test_a_filled_year_with_zero_revenue_gives_only_the_last_growth_and_no_loss(dry, tmp_path):
    """The two-year growth was shown before the fill (2026 against 2024); that figure is gone with #150 U8, so the
    zero base costs no board a figure it had, and the last growth (0 against 100 is -100%) is a restoration."""
    summary = _run(dry, {"ZER": ZERO_BASE}, tmp_path)
    rows = {r["metric"]: r for r in _rows(tmp_path, "verdict_changes.csv")}
    assert {m: r["class"] for m, r in rows.items()} == {"rev_growth_last": "restoration"}
    assert float(rows["rev_growth_last"]["new"]) == pytest.approx(-1.0)     # 0 against 100: the base is the 2024 figure
    assert summary["verdicts"]["named_losses"] == {"record_insertion": 0, "zero_revenue_base": 0}
    assert summary["verdicts"]["must_be_zero"]["lost"] == 0


def test_a_zero_revenue_year_in_the_middle_changes_no_verdict(dry, tmp_path):
    facts = _facts(flow={REV: {"2023-12-31": 100.0, "2025-12-31": 110.0, "2026-12-31": 130.0},
                         SALES: {"2024-12-31": 0.0}})
    summary = _run(dry, {"MID": facts}, tmp_path)
    assert _rows(tmp_path, "verdict_changes.csv") == []                     # 2026 against 2025 is untouched
    assert summary["verdicts"]["named_losses"] == {"record_insertion": 0, "zero_revenue_base": 0}


def test_a_value_to_none_with_no_such_cause_stays_a_lost_verdict(dry):
    old = [_hand("2024", 40.0, rev=100.0), _hand("2025", 40.0, rev=120.0)]
    new = [_hand("2024", 40.0, rev=100.0), _hand("2025", 40.0, rev=None)]          # revenue vanished from a held year
    rows = {r["metric"]: r["class"] for r in dry.verdict_changes("X", old, new, [])["rows"]}
    assert rows["rev_growth_ttm"] == "lost"
    # a record that was already there and lost its revenue is no insertion either
    vanished = [_hand("2024", 40.0, rev=None), _hand("2025", 40.0, rev=120.0)]
    rows = {r["metric"]: r["class"] for r in dry.verdict_changes("X", old, vanished, [])["rows"]}
    assert rows["rev_growth_ttm"] == "lost"


def test_a_moved_net_debt_is_an_unexpected_debt_change_and_counted_as_must_be_zero(dry, monkeypatch, tmp_path):
    real = dry.extract_annual_before_246

    def disagreeing(f):
        annual = real(f)
        annual[-1]["total_debt"] = 999.0
        return annual

    monkeypatch.setattr(dry, "extract_annual_before_246", disagreeing)
    summary = _run(dry, {"LIQ": LIQUID}, tmp_path)
    assert [(r["metric"], r["class"]) for r in _rows(tmp_path, "verdict_changes.csv")] == [
        ("net_debt_assets", "unexpected_debt_change")]
    assert summary["verdicts"]["must_be_zero"]["unexpected_debt_change"] == 1


def test_ag_is_judged_at_the_run_day_anchor_not_at_the_last_revenue_year(dry):
    """A filer whose newest record has no revenue: the live card reads the newest record, so a debt component the
    backfill fills in its OLD last-revenue year must not show up as a moved net debt (#246 dry run 2026-10-17)."""
    assets = {"total_assets": 1000.0, "cash": 100.0}
    year = dry.AS_OF.year
    old = [_hand("2017", None, total_debt=None, period_ends={"revenue": "2017-12-31"}, **assets),
           {"fiscal_year": str(year - 1), "revenue": None, "total_debt": 10.0, "total_assets": 1000.0, "cash": 100.0,
            "period_ends": {"total_debt": f"{year - 1}-12-31"}}]
    new = [_hand("2017", None, total_debt=300.0, period_ends={"revenue": "2017-12-31"}, **assets), dict(old[1])]
    rows = dry.verdict_changes("X", old, new, [])["rows"]
    assert [r for r in rows if r["metric"] == "net_debt_assets"] == []


def test_a_ratio_that_moves_because_its_own_year_gained_a_component_is_a_component_fill(dry):
    old = [_hand("2025", 60.0, ebit=10.0, total_debt=50.0, cash=20.0, depreciation=None)]
    new = [_hand("2025", 60.0, ebit=10.0, total_debt=50.0, cash=20.0, depreciation=5.0)]
    rows = {r["metric"]: r for r in dry.verdict_changes("X", old, new, [])["rows"]}
    assert rows["quality_net_debt_ebitda"]["class"] == "component_fill"
    assert "depreciation" in rows["quality_net_debt_ebitda"]["detail"]


def test_a_ratio_that_moves_with_no_filled_component_stays_unexpected(dry):
    old = [_hand("2025", 60.0, ebit=10.0, total_debt=50.0, cash=20.0, depreciation=5.0)]
    new = [_hand("2025", 60.0, ebit=10.0, total_debt=50.0, cash=20.0, depreciation=9.0)]
    rows = {r["metric"]: r["class"] for r in dry.verdict_changes("X", old, new, [])["rows"]}
    assert rows["quality_net_debt_ebitda"] == "unexpected_quality_change"


# ── #239: debt components and the tag census ───────────────────────────────

def test_debt_view_separates_unknown_with_a_borrowing_from_only_an_operating_lease(dry):
    borrowing = dry.debt_view([{"fiscal_year": "2026", "total_debt": None, "convertible_debt_noncurrent": 50.0}])
    assert borrowing["unknown_with_borrowing"] is True and borrowing["unknown_only_operating_lease"] is False
    lease = dry.debt_view([{"fiscal_year": "2026", "total_debt": None, "operating_lease_noncurrent": 9.0}])
    assert lease["unknown_with_borrowing"] is False and lease["unknown_only_operating_lease"] is True
    known = dry.debt_view([{"fiscal_year": "2026", "total_debt": 100.0, "lt_debt_total": 100.0}])
    assert known["total_unknown"] is False and known["understated"] is False
    assert dry.debt_view([{"fiscal_year": "2026", "total_debt": 100.0, "lt_debt_total": 150.0}])["understated"] is True
    assert dry.debt_view([])["total_unknown"] is True


def test_the_census_counts_components_unknown_totals_and_unmapped_tags(dry, tmp_path):
    convertible_only = _facts(flow={REV: _s([2025, 2026], 10000)},
                              inst={CONVERTIBLE: _s([2026], 50), "BridgeLoanPayableFooBar": _s([2026], 5)})
    lease_only = _facts(flow={REV: _s([2025, 2026], 10000)}, inst={OP_LEASE: _s([2026], 9)})
    summary = _run(dry, {"CVT": convertible_only, "LSE": lease_only, "LIQ": LIQUID}, tmp_path)
    debt = summary["239_debt"]
    assert (debt["tickers"], debt["total_unknown"]) == (3, 2)
    assert (debt["unknown_with_borrowing"], debt["unknown_only_operating_lease"]) == (1, 1)
    assert debt["components_at_newest_record"] == {"convertible_debt_noncurrent": 1,
                                                   "operating_lease_noncurrent": 1}
    assert debt["mapped_tags_present_tickers"] == {CONVERTIBLE: 1, OP_LEASE: 1, STI: 1, LT_DEBT: 1}
    assert "DebtCurrent" in debt["mapped_tags_absent"] and LT_DEBT not in debt["mapped_tags_absent"]
    assert json.loads((tmp_path / "out" / "unmapped_tags.json").read_text()) == [
        {"tag": "BridgeLoanPayableFooBar", "tickers": 1}]


def test_the_tag_census_lists_only_fiscal_year_balance_sheet_concepts_the_app_does_not_read(dry):
    facts = _facts(inst={LT_DEBT: {"2026-12-31": 1.0}, "NewDebtFacilityNoteX": {"2026-12-31": 1.0},
                         "Goodwill": {"2026-12-31": 1.0}})
    gaap = facts["facts"]["us-gaap"]
    gaap["DebtFlowOnly"] = {"units": {"USD": [_flow("2026-12-31", 1.0)]}}             # a flow: no balance
    gaap["QuarterOnlyLoan"] = {"units": {"USD": [_instant("2026-09-30", 1.0, fp="Q3")]}}
    mapped, unmapped = dry.tag_census(facts)
    assert mapped == {LT_DEBT}
    assert unmapped == {"NewDebtFacilityNoteX"}


# ── the census of the ten ───────────────────────────────────────────────────

@pytest.mark.parametrize("facts, status, shape, restored, stop_reason", [
    (CHAIN, "fully_restored", "chain", 9, None),
    (HOLE, "fully_restored", "hole", 1, None),
    (GAP, "unchanged", "gap", 0, "gap"),
    (GUARD, "unchanged", "guard_stop", 0, "guard_stop"),
    (PLAIN, "unchanged", "none", 0, None),
])
def test_explain_field_names_the_status_and_the_shape(dry, facts, status, shape, restored, stop_reason):
    out = dry.explain_field(facts, "revenue")
    assert (out["status"], out["shape"], len(out["restored_years"])) == (status, shape, restored)
    assert (out["stop"] or {}).get("reason") == stop_reason


def test_a_chain_that_stops_at_a_missing_year_is_partly_restored(dry):
    facts = _facts(flow={REV: _s([2025, 2026], 20000), SALES: _s([2020, 2021, 2023, 2024], 10000)})
    out = dry.explain_field(facts, "revenue")
    assert (out["status"], out["shape"]) == ("partly_restored", "chain+gap")
    assert out["restored_years"] == ["2023", "2024"] and out["not_restored"] == ["2020", "2021"]
    assert out["stop"] == {"next_year": "2022", "reason": "gap"}


def test_a_chain_that_stops_at_a_year_end_change_names_the_guard(dry):
    facts = _facts(flow={REV: _s([2025, 2026], 20000),
                         SALES: {"2024-12-31": 10024.0, "2023-12-31": 10023.0,
                                 "2022-06-30": 10022.0, "2021-06-30": 10021.0}})
    out = dry.explain_field(facts, "revenue")
    assert (out["status"], out["shape"]) == ("partly_restored", "chain+guard_stop")
    assert out["stop"] == {"next_year": "2022", "reason": "guard_stop", "end": "2022-06-30",
                           "newer_end": "2023-12-31"}


def test_the_census_of_the_ten_reports_ktcc_unchanged_with_a_gap_and_lists_the_names_not_run(dry, tmp_path):
    summary = _run(dry, {"KTCC": GAP, "ARAY": CHAIN}, tmp_path, census_names=dry.TEN)
    assert summary["census_ten"] == [
        {"ticker": "ARAY", "field": "net_income", "status": "unchanged", "shape": "none"},
        {"ticker": "KTCC", "field": "revenue", "status": "unchanged", "shape": "gap"}]
    assert summary["census_ten_missing"] == sorted(set(dry.TEN) - {"KTCC", "ARAY"})
    assert [c["ticker"] for c in json.loads((tmp_path / "out" / "census_ten.json").read_text())] == ["ARAY", "KTCC"]


# ── the run ─────────────────────────────────────────────────────────────────

def test_every_status_is_logged_and_only_a_real_fetch_counts_as_an_sec_call(dry, tmp_path):
    def fetch(ticker):
        if ticker == "NOC":
            raise dry.NoCik(ticker)
        if ticker == "ERR":
            raise RuntimeError("429 Too Many Requests")
        if ticker == "NON":
            return None
        if ticker == "EMP":
            return {}
        return PLAIN

    tickers = ["NOC", "ERR", "NON", "EMP", "OKK"]
    summary = dry.run(tickers, fetch=fetch, put_row=None, out_dir=tmp_path / "out", census_names={})
    assert summary["status"] == {"no_cik": 1, "fetch_error": 1, "no_facts": 2, "reextracted": 1}
    assert summary["sec"] == {"calls": 5, "errors": 2, "http_429": 2}          # ERR is asked for once more at the end
    log = [json.loads(line) for line in (tmp_path / "out" / "reextract_log.jsonl").read_text().splitlines()]
    assert [(r["ticker"], r["status"]) for r in log] == [
        ("NOC", "no_cik"), ("ERR", "fetch_error"), ("NON", "no_facts"), ("EMP", "no_facts"), ("OKK", "reextracted"),
        ("ERR", "fetch_error")]
    assert "429" in log[1]["error"] and not log[1].get("retry") and log[5]["retry"] is True


def test_the_time_budget_stops_the_run_early(dry, monkeypatch, tmp_path):
    ticks = iter(range(0, 1000, 10))
    monkeypatch.setattr(dry, "time", types.SimpleNamespace(monotonic=lambda: next(ticks), time=lambda: 1.0))
    seen, said = [], []
    summary = dry.run(["AAA", "BBB", "CCC"], fetch=lambda t: seen.append(t) or PLAIN, put_row=None,
                      out_dir=tmp_path / "out", max_seconds=15, census_names={}, progress=said.append)
    assert seen == ["AAA"] and summary["n_attempted"] == 1 and summary["stopped"] == "budget"
    assert said == ["stopping early at 1/3 (max_seconds budget hit)"]


def test_put_row_gets_the_after_extraction_and_none_writes_nothing(dry, tmp_path):
    rows = []
    _run(dry, {"AAA": CHAIN}, tmp_path, put_row=rows.append)
    [row] = rows
    assert row["ticker"] == "AAA" and row["annual"] == se.extract_annual(CHAIN)
    assert row["quarterly"] == se.extract_quarterly(CHAIN) and row["fetched_at"] > 0
    assert row["meta"] == {"source": "sec_edgar", "entity_name": "Synthetic Co", "is_us": True, "available": True}
    _run(dry, {"AAA": CHAIN}, tmp_path / "second", put_row=None)          # no writer: no error, no row


def test_added_values_and_the_summary_count_what_was_added(dry, tmp_path):
    summary = _run(dry, {"AAA": CHAIN, "BBB": HOLE, "CCC": PLAIN}, tmp_path)
    assert sorted((r["ticker"], r["field"], r["years"]) for r in _rows(tmp_path, "added_values.csv")) == [
        ("AAA", "revenue", "2017;2018;2019;2020;2021;2022;2023;2024;2025"), ("BBB", "revenue", "2025")]
    data = summary["data"]
    assert (data["tickers_with_added_years"], data["added_years"]) == (2, 10)
    assert data["added_values_by_field"] == {"revenue": 10}
    assert data["tickers_with_added_values_by_field"] == {"revenue": 2}
    assert data["acceptance"] == "PASS" and (data["changed_existing"], data["unexplained_derived"]) == (0, 0)


def test_a_before_extraction_that_disagrees_with_the_after_one_fails_the_acceptance(dry, monkeypatch, tmp_path):
    """The wiring: a changed existing value must reach the CSV, the count and the PASS/FAIL line."""
    real = dry.extract_annual_before_246

    def disagreeing(facts):
        annual = real(facts)
        annual[-1]["revenue"] = 1.0
        return annual

    monkeypatch.setattr(dry, "extract_annual_before_246", disagreeing)
    summary = _run(dry, {"AAA": PLAIN}, tmp_path)
    assert [(r["ticker"], r["year"], r["field"]) for r in _rows(tmp_path, "changed_existing.csv")] == [
        ("AAA", "2026", "revenue")]
    assert summary["data"]["changed_existing"] == 1 and summary["data"]["acceptance"] == "FAIL"


def test_an_unexplained_derived_change_alone_fails_the_acceptance(dry, monkeypatch, tmp_path):
    facts = _facts(flow={REV: _s([2025, 2026], 10000)}, inst={LT_DEBT: _s([2025, 2026], 100)})
    real = dry.extract_annual_before_246

    def disagreeing(f):
        annual = real(f)
        annual[-1]["total_debt"] = 1.0                     # only the derived total differs
        return annual

    monkeypatch.setattr(dry, "extract_annual_before_246", disagreeing)
    summary = _run(dry, {"AAA": facts}, tmp_path)
    assert summary["data"]["changed_existing"] == 0 and summary["data"]["unexplained_derived"] == 1
    assert summary["data"]["acceptance"] == "FAIL"
    assert [(r["ticker"], r["field"]) for r in _rows(tmp_path, "unexplained_derived.csv")] == [("AAA", "total_debt")]


def test_the_output_files_are_all_written_even_for_an_empty_run(dry, tmp_path):
    summary = dry.run([], fetch=lambda t: PLAIN, put_row=None, out_dir=tmp_path / "out", census_names={})
    assert summary["n_attempted"] == 0 and summary["data"]["acceptance"] == "PASS"
    names = {p.name for p in (tmp_path / "out").iterdir()}
    assert names == {"reextract_log.jsonl", "changed_existing.csv", "unexplained_derived.csv", "added_values.csv",
                     "total_debt_moves.csv", "verdict_changes.csv", "gate_flips.csv", "stale_margins.csv",
                     "sti_flips.csv", "census_ten.json", "unmapped_tags.json", "summary.json", "run_info.json"}


# ═══ hardening (commit 7) ═══════════════════════════════════════════════════
# The live store, an error in one ticker, an interrupted run, a resume, the placeholder User-Agent, a run of 403s.

# ── the guard knows the live store ──────────────────────────────────────────

def test_the_live_store_is_the_repos_dot_store_and_the_real_one_is_refused(dry, capsys):
    assert dry.LIVE_STORE == (dry.REPO / ".store").resolve()
    for target in (dry.LIVE_STORE / "raw.duckdb", dry.LIVE_STORE / "rc246_dry" / "raw.duckdb"):
        with pytest.raises(SystemExit) as exc:
            dry.assert_store_copy(target)
        assert exc.value.code == 2
    assert "live" in capsys.readouterr().err.lower()


def test_a_path_with_the_marker_is_still_refused_when_it_is_the_live_store_or_inside_it(dry, monkeypatch, tmp_path,
                                                                                    capsys):
    live = tmp_path / "rc246_dry_checkout" / ".store"                 # the marker is in the live path itself
    live.mkdir(parents=True)
    monkeypatch.setattr(dry, "LIVE_STORE", live.resolve())
    for target in (live / "raw.duckdb", live / "rc246_dry" / "raw.duckdb", live):
        with pytest.raises(SystemExit) as exc:
            dry.assert_store_copy(target)
        assert exc.value.code == 2
    assert "live store" in capsys.readouterr().err
    assert dry.assert_store_copy(tmp_path / "rc246_dry" / ".store" / "raw.duckdb") is None   # a copy beside it is fine


def test_a_symlink_that_leads_into_the_live_store_is_refused(dry, monkeypatch, tmp_path):
    live = tmp_path / "live" / ".store"
    live.mkdir(parents=True)
    link = tmp_path / "rc246_dry_link"
    try:
        link.symlink_to(live, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("no symlinks here")
    monkeypatch.setattr(dry, "LIVE_STORE", live.resolve())
    with pytest.raises(SystemExit) as exc:
        dry.assert_store_copy(link / "raw.duckdb")
    assert exc.value.code == 2


def test_main_refuses_a_marked_live_store_before_any_request(dry, monkeypatch, tmp_path):
    live = tmp_path / "rc246_dry_checkout" / ".store"
    live.mkdir(parents=True)
    monkeypatch.setattr(dry, "LIVE_STORE", live.resolve())
    monkeypatch.setattr(rawstore, "DB_PATH", live / "raw.duckdb")
    with pytest.raises(SystemExit) as exc:
        dry.main(["--tickers", "AAA", "--out", str(tmp_path / "out")])
    assert exc.value.code == 2 and not (tmp_path / "out").exists()


# ── one ticker's error does not end the run ─────────────────────────────────

def _log(out: Path) -> list[dict]:
    return [json.loads(line) for line in (out / "reextract_log.jsonl").read_text(encoding="utf-8").splitlines()]


def test_an_analysis_error_is_logged_with_its_place_and_leaves_no_partial_rows(dry, monkeypatch, tmp_path):
    real = dry.verdict_changes

    def flaky(ticker, *args):
        if ticker == "BAD":
            raise ZeroDivisionError("boom")
        return real(ticker, *args)

    monkeypatch.setattr(dry, "verdict_changes", flaky)
    summary = _run(dry, {"AAA": CHAIN, "BAD": HOLE, "CCC": CHAIN}, tmp_path)
    assert summary["status"] == {"reextracted": 2, "analysis_error": 1}
    bad = next(r for r in _log(tmp_path / "out") if r["ticker"] == "BAD")
    assert (bad["status"], bad["stage"], bad["error"]) == ("analysis_error", "analyse", "ZeroDivisionError: boom")
    assert bad["where"].endswith("in flaky") and "test_reextract_dryrun_246.py" in bad["where"]
    assert summary["analysis_errors"] == [{"ticker": "BAD", "stage": "analyse", "error": "ZeroDivisionError: boom"}]
    # nothing of BAD was counted or written (HOLE would have added a 2025 year)
    assert {r["ticker"] for r in _rows(tmp_path, "added_values.csv")} == {"AAA", "CCC"}
    assert summary["data"]["added_years"] == 18 and summary["data"]["tickers_with_added_years"] == 2
    assert summary["data"]["acceptance"] == "INCOMPLETE"     # a ticker went unchecked: never a PASS


def test_a_failing_store_write_is_an_error_of_that_ticker_only(dry, tmp_path):
    def put_row(row):
        if row["ticker"] == "BBB":
            raise RuntimeError("database is locked")

    summary = _run(dry, {"AAA": PLAIN, "BBB": PLAIN, "CCC": PLAIN}, tmp_path, put_row=put_row)
    assert summary["status"] == {"reextracted": 2, "analysis_error": 1}
    assert summary["analysis_errors"] == [{"ticker": "BBB", "stage": "write", "error": "RuntimeError: database is locked"}]


def test_the_run_stops_after_consecutive_errors(dry, tmp_path):
    def put_row(row):
        if row["ticker"] != "C_OK":
            raise RuntimeError("locked")

    facts = {t: PLAIN for t in ("A1", "A2", "B1", "B2", "B3", "B4", "C_OK")}     # sorted: A1 A2 B1 B2 B3 B4 C_OK
    summary = _run(dry, facts, tmp_path, put_row=put_row, max_errors=3)
    assert summary["stopped"] == "consecutive_errors" and summary["n_attempted"] == 3   # A1 A2 B1, then stop
    assert [e["ticker"] for e in summary["analysis_errors"]] == ["A1", "A2", "B1"]


def test_two_errors_then_a_good_ticker_then_two_errors_does_not_stop(dry, tmp_path):
    def put_row(row):
        if row["ticker"] != "C_OK":
            raise RuntimeError("locked")

    facts = {t: PLAIN for t in ("A1", "A2", "C_OK", "D1", "D2")}
    summary = _run(dry, facts, tmp_path, put_row=put_row, max_errors=3)
    assert summary["stopped"] is None and summary["n_attempted"] == 5
    assert summary["status"] == {"analysis_error": 4, "reextracted": 1}


# ── an interrupted run still writes its summary ─────────────────────────────

@pytest.mark.parametrize("raiser, stopped", [(KeyboardInterrupt, "interrupted"), (SystemExit, "interrupted")])
def test_an_interrupted_run_writes_summary_json_and_keeps_its_state(dry, tmp_path, raiser, stopped):
    def fetch(ticker):
        if ticker == "BBB":
            raise raiser()
        return PLAIN

    out = tmp_path / "out"
    with pytest.raises(raiser):
        dry.run(["AAA", "BBB", "CCC"], fetch=fetch, put_row=None, out_dir=out, census_names={})
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["stopped"] == stopped and summary["status"] == {"reextracted": 1}
    assert summary["data"]["acceptance"] == "INCOMPLETE"
    assert (out / "state.json").exists()                              # resumable


def test_a_crash_outside_a_ticker_still_writes_summary_json(dry, monkeypatch, tmp_path):
    monkeypatch.setattr(dry, "time", types.SimpleNamespace(monotonic=iter(range(0, 1000, 10)).__next__, time=lambda: 1.0))

    def progress(message):
        raise RuntimeError("disk full")

    out = tmp_path / "out"
    with pytest.raises(RuntimeError):
        dry.run(["AAA", "BBB", "CCC"], fetch=lambda t: PLAIN, put_row=None, out_dir=out, max_seconds=15,
                census_names={}, progress=progress)
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["stopped"] == "crashed: RuntimeError: disk full" and summary["status"] == {"reextracted": 1}


# ── resume ──────────────────────────────────────────────────────────────────

RESUME_FACTS = {"AAA": CHAIN, "BBB": HOLE, "CCC": STALE, "DDD": LIQUID, "EEE": PLAIN, "FFF": GAP, "GGG": SHIFT}
RESUME_CSVS = ("changed_existing.csv", "unexplained_derived.csv", "total_debt_moves.csv", "added_values.csv",
               "verdict_changes.csv", "gate_flips.csv", "stale_margins.csv", "sti_flips.csv", "reextract_log.jsonl",
               "census_ten.json", "unmapped_tags.json", "summary.json")
MAIN_SCOPE = {"tickers": sorted(RESUME_FACTS), "limit": None, "start_after": None, "no_write": True, "partial": True}


def _info(out: Path) -> dict:
    """The last run of the folder (``run_info.json`` keeps the history of every run)."""
    return json.loads((out / "run_info.json").read_text(encoding="utf-8"))["history"][-1]


def _interrupted_after(dry, out: Path, last_good: str, put_row=None, scope=None) -> None:
    """Part one of a run: stops (interrupt) while fetching the ticker after ``last_good``."""
    tickers = sorted(RESUME_FACTS)
    stop_at = tickers[tickers.index(last_good) + 1]

    def fetch(ticker):
        if ticker == stop_at:
            raise KeyboardInterrupt
        return RESUME_FACTS[ticker]

    with pytest.raises(KeyboardInterrupt):
        dry.run(tickers, fetch=fetch, put_row=put_row, out_dir=out, census_names={"CCC": "revenue", "AAA": "revenue"},
                scope=scope)


def test_a_resumed_run_ends_with_the_same_outputs_as_one_uninterrupted_run(dry, tmp_path):
    names = {"CCC": "revenue", "AAA": "revenue"}
    whole = dry.run(sorted(RESUME_FACTS), fetch=lambda t: RESUME_FACTS[t], put_row=None, out_dir=tmp_path / "whole",
                    census_names=names)
    _interrupted_after(dry, tmp_path / "part", "CCC")
    state = json.loads((tmp_path / "part" / "state.json").read_text(encoding="utf-8"))
    assert state["last_ticker"] == "CCC"
    resumed = dry.run(sorted(RESUME_FACTS), fetch=lambda t: RESUME_FACTS[t], put_row=None, out_dir=tmp_path / "part",
                      census_names=names, resume=True)
    assert _info(tmp_path / "part")["resumed_after"] == "CCC" and _info(tmp_path / "whole")["resumed_after"] is None
    assert resumed == whole
    for name in RESUME_CSVS:
        assert (tmp_path / "part" / name).read_text(encoding="utf-8") == (tmp_path / "whole" / name).read_text(
            encoding="utf-8"), name
    assert not (tmp_path / "part" / "state.json").exists()           # finished: nothing left to resume


def test_a_resumed_run_fetches_only_the_tickers_after_the_last_one_done(dry, tmp_path):
    _interrupted_after(dry, tmp_path / "part", "CCC")
    seen = []
    dry.run(sorted(RESUME_FACTS), fetch=lambda t: seen.append(t) or RESUME_FACTS[t], put_row=None,
            out_dir=tmp_path / "part", census_names={}, resume=True)
    assert seen == ["DDD", "EEE", "FFF", "GGG"]


def test_the_time_budget_leaves_a_resumable_state_and_a_finished_run_leaves_none(dry, monkeypatch, tmp_path):
    monkeypatch.setattr(dry, "time", types.SimpleNamespace(monotonic=iter(range(0, 1000, 10)).__next__, time=lambda: 1.0))
    dry.run(["AAA", "BBB", "CCC"], fetch=lambda t: PLAIN, put_row=None, out_dir=tmp_path / "a", max_seconds=15,
            census_names={})
    assert json.loads((tmp_path / "a" / "state.json").read_text(encoding="utf-8"))["last_ticker"] == "AAA"
    dry.run(["AAA"], fetch=lambda t: PLAIN, put_row=None, out_dir=tmp_path / "b", census_names={})
    assert not (tmp_path / "b" / "state.json").exists()


def test_resume_without_a_state_is_an_error(dry, tmp_path):
    with pytest.raises(FileNotFoundError):
        dry.run(["AAA"], fetch=lambda t: PLAIN, put_row=None, out_dir=tmp_path / "none", census_names={}, resume=True)


def _copy_store(monkeypatch, tmp_path):
    monkeypatch.setattr(rawstore, "DB_PATH", tmp_path / "rc246_dry" / ".store" / "raw.duckdb")


def _cik_world(monkeypatch, tickers):
    """``_cik_map`` and ``_fetch_facts`` for AAA..: cik 11, 22, ...; the list of ciks fetched is returned."""
    fetched = []
    monkeypatch.setattr(se, "_cik_map", lambda: {t: {"cik": 11 * (i + 1)} for i, t in enumerate(tickers)})
    monkeypatch.setattr(se, "_fetch_facts", lambda cik: fetched.append(cik) or PLAIN)
    return fetched


def test_main_resume_continues_a_stopped_run_and_start_after_must_name_its_last_ticker(dry, monkeypatch, tmp_path,
                                                                                       capsys):
    _copy_store(monkeypatch, tmp_path)
    out = tmp_path / "out"
    tickers = ["AAA", "BBB", "CCC", "DDD"]
    fetched = _cik_world(monkeypatch, tickers)

    stops = []

    def stop_at_ccc(cik):
        if cik == 33 and not stops:         # once: a refusal that goes missing then fails the test, not the session
            stops.append(cik)
            raise KeyboardInterrupt
        return PLAIN

    monkeypatch.setattr(se, "_fetch_facts", stop_at_ccc)
    with pytest.raises(KeyboardInterrupt):
        dry.main(["--tickers", ",".join(tickers), "--no-write", "--out", str(out)])
    assert (out / "state.json").exists()
    capsys.readouterr()
    # a fresh run into the same folder would destroy the state: refused, and the message names the way out
    with pytest.raises(SystemExit) as exc:
        dry.main(["--tickers", ",".join(tickers), "--no-write", "--out", str(out)])
    assert exc.value.code == 2
    assert "--resume" in capsys.readouterr().err
    # a start-after that is not the state's last ticker would skip or double-count: refused
    with pytest.raises(SystemExit) as exc:
        dry.main(["--tickers", ",".join(tickers), "--no-write", "--out", str(out), "--start-after", "AAA"])
    assert exc.value.code == 2
    monkeypatch.setattr(se, "_fetch_facts", lambda cik: fetched.append(cik) or PLAIN)
    summary = dry.main(["--tickers", ",".join(tickers), "--no-write", "--out", str(out), "--start-after", "bbb"])
    assert fetched == [33, 44] and _info(out)["resumed_after"] == "BBB"
    assert summary["status"] == {"reextracted": 4} and summary["stopped"] is None
    assert not (out / "state.json").exists()


def test_main_resume_flag_takes_the_last_ticker_from_the_state_and_refuses_without_one(dry, monkeypatch, tmp_path):
    _copy_store(monkeypatch, tmp_path)
    out = tmp_path / "out"
    with pytest.raises(SystemExit) as exc:
        dry.main(["--tickers", "AAA", "--no-write", "--out", str(out), "--resume"])
    assert exc.value.code == 2
    _interrupted_after(dry, out, "CCC", scope=MAIN_SCOPE)
    fetched = _cik_world(monkeypatch, sorted(RESUME_FACTS))
    dry.main(["--tickers", ",".join(sorted(RESUME_FACTS)), "--no-write", "--out", str(out), "--resume"])
    assert _info(out)["resumed_after"] == "CCC" and len(fetched) == 4


def test_main_overwrite_starts_over_and_start_after_without_a_state_is_a_plain_partial_run(dry, monkeypatch, tmp_path):
    _copy_store(monkeypatch, tmp_path)
    tickers = ["AAA", "BBB", "CCC"]
    fetched = _cik_world(monkeypatch, tickers)
    out = tmp_path / "out"
    summary = dry.main(["--tickers", ",".join(tickers), "--no-write", "--out", str(out), "--start-after", "AAA"])
    assert fetched == [22, 33] and _info(out)["resumed_after"] is None
    _interrupted_after(dry, tmp_path / "stopped", "CCC")
    summary = dry.main(["--tickers", ",".join(tickers), "--no-write", "--out", str(tmp_path / "stopped"),
                        "--overwrite"])
    assert _info(tmp_path / "stopped")["resumed_after"] is None and summary["status"] == {"reextracted": 3}


# ── the SEC contact and the 403 stop ────────────────────────────────────────

@pytest.mark.parametrize("contact", [None, "", "   ", "RevenueChaser research (contact: local@example.com)",
                                     "Some Name <person@EXAMPLE.com>", "x (contact: y@sub.example.org)"])
def test_main_refuses_the_placeholder_user_agent_before_any_request(dry, monkeypatch, tmp_path, capsys, contact):
    _copy_store(monkeypatch, tmp_path)
    if contact is None:
        monkeypatch.delenv("RC_SEC_USER_AGENT", raising=False)
    else:
        monkeypatch.setenv("RC_SEC_USER_AGENT", contact)
    with pytest.raises(SystemExit) as exc:                           # the autouse boom would fail on any request
        dry.main(["--tickers", "AAA", "--no-write", "--out", str(tmp_path / "out")])
    assert exc.value.code == 2 and not (tmp_path / "out").exists()
    err = capsys.readouterr().err
    assert "RC_SEC_USER_AGENT" in err and "example" not in err.lower().replace("example.com placeholder", "")


def test_main_accepts_a_real_contact_and_never_prints_it(dry, monkeypatch, tmp_path, capsys):
    _copy_store(monkeypatch, tmp_path)
    monkeypatch.setenv("RC_SEC_USER_AGENT", "Avo Research avo.person@realdomain.nl")
    _cik_world(monkeypatch, ["AAA"])
    dry.main(["--tickers", "AAA", "--no-write", "--out", str(tmp_path / "out")])
    captured = capsys.readouterr()
    assert "realdomain" not in captured.out + captured.err
    assert "realdomain" not in (tmp_path / "out" / "summary.json").read_text(encoding="utf-8")


def _http(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://data.sec.gov/api/xbrl/companyfacts/CIK0000000001.json")
    return httpx.HTTPStatusError(f"Client error '{status}'", request=request, response=httpx.Response(status, request=request))


def test_the_run_stops_after_consecutive_403s_and_a_success_resets_the_count(dry, tmp_path):
    def fetch(ticker):
        if ticker in ("T1", "T2", "T4", "T5", "T6", "T7"):
            raise _http(403)
        return PLAIN

    facts = {t: PLAIN for t in ("T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8")}
    out = tmp_path / "out"
    summary = dry.run(sorted(facts), fetch=fetch, put_row=None, out_dir=out, census_names={}, max_403=3)
    # T1 T2 (2 in a row), T3 ok (reset), T4 T5 T6 -> the third in a row: stop. T7 and T8 are never requested
    assert summary["stopped"] == "consecutive_403" and summary["n_attempted"] == 6
    assert summary["sec"]["http_403"] == 5 and summary["sec"]["errors"] == 5
    assert summary["data"]["acceptance"] == "INCOMPLETE"
    # the refused tickers T4..T6 are not behind the saved state: a later resume asks for them again
    assert json.loads((out / "state.json").read_text(encoding="utf-8"))["last_ticker"] == "T3"


def test_a_run_stopped_by_403s_resumes_at_the_first_refused_ticker_and_ends_like_an_unblocked_run(dry, tmp_path):
    tickers = [f"T{i}" for i in range(1, 9)]
    blocked = {"on": True}

    def fetch(ticker):
        if ticker in ("T1", "T2") or (blocked["on"] and ticker in ("T4", "T5", "T6")):
            raise _http(403)
        return PLAIN

    first = dry.run(tickers, fetch=fetch, put_row=None, out_dir=tmp_path / "part", census_names={}, max_403=3)
    assert first["stopped"] == "consecutive_403"
    blocked["on"] = False
    seen = []
    resumed = dry.run(tickers, fetch=lambda t: seen.append(t) or fetch(t), put_row=None, out_dir=tmp_path / "part",
                      census_names={}, max_403=3, resume=True)
    assert seen == ["T4", "T5", "T6", "T7", "T8", "T1", "T2"] and _info(tmp_path / "part")["resumed_after"] == "T3"
    whole = dry.run(tickers, fetch=fetch, put_row=None, out_dir=tmp_path / "whole", census_names={}, max_403=3)
    assert whole["stopped"] is None and resumed["sec"]["http_403"] == 4        # T1 T2, and again at the end; T4..T6 not counted
    assert resumed == whole
    for name in RESUME_CSVS:                                                   # the log too: no stale 403 lines of T4..T6
        assert (tmp_path / "part" / name).read_text(encoding="utf-8") == (tmp_path / "whole" / name).read_text(
            encoding="utf-8"), name


def test_a_resume_cuts_every_file_back_to_the_state_so_a_torn_or_repeated_row_cannot_survive(dry, tmp_path):
    names = {"CCC": "revenue", "AAA": "revenue"}
    whole = dry.run(sorted(RESUME_FACTS), fetch=lambda t: RESUME_FACTS[t], put_row=None, out_dir=tmp_path / "whole",
                    census_names=names)
    _interrupted_after(dry, tmp_path / "part", "CCC")
    for name in ("changed_existing.csv", "added_values.csv", "verdict_changes.csv", "reextract_log.jsonl"):
        with (tmp_path / "part" / name).open("a", encoding="utf-8") as f:       # a hard kill: DDD's rows, half of one
            f.write("DDD,field,2026,half a ro")
    resumed = dry.run(sorted(RESUME_FACTS), fetch=lambda t: RESUME_FACTS[t], put_row=None, out_dir=tmp_path / "part",
                      census_names=names, resume=True)
    assert resumed == whole
    for name in RESUME_CSVS:
        assert (tmp_path / "part" / name).read_text(encoding="utf-8") == (tmp_path / "whole" / name).read_text(
            encoding="utf-8"), name


def test_a_resume_refuses_an_output_file_that_is_shorter_than_the_state_says(dry, tmp_path):
    _interrupted_after(dry, tmp_path / "part", "CCC")
    (tmp_path / "part" / "added_values.csv").write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="added_values.csv"):
        dry.run(sorted(RESUME_FACTS), fetch=lambda t: RESUME_FACTS[t], put_row=None, out_dir=tmp_path / "part",
                census_names={}, resume=True)
    assert (tmp_path / "part" / "added_values.csv").read_text(encoding="utf-8") == ""     # nothing touched


def test_a_fresh_run_removes_a_stale_state_before_it_starts(dry, tmp_path):
    out = tmp_path / "out"
    _interrupted_after(dry, out, "CCC")
    assert (out / "state.json").exists()

    def refused(ticker):
        raise _http(403)

    dry.run(["AAA", "BBB"], fetch=refused, put_row=None, out_dir=out, census_names={}, max_403=2)
    assert not (out / "state.json").exists()                          # it would point into another run's files


def test_a_403_is_recognised_by_status_or_by_text_and_a_ticker_without_cik_is_no_request(dry, tmp_path):
    def fetch(ticker):
        if ticker == "T1":
            raise _http(403)
        if ticker == "T2":
            raise RuntimeError("HTTP 403 Forbidden")
        if ticker == "T3":
            raise dry.NoCik(ticker)                                  # no request: neither counts nor resets
        raise _http(403)

    summary = dry.run(["T1", "T2", "T3", "T4"], fetch=fetch, put_row=None, out_dir=tmp_path / "out",
                      census_names={}, max_403=3)
    assert summary["stopped"] == "consecutive_403" and summary["sec"]["http_403"] == 3


def test_a_429_or_a_404_between_403s_resets_the_count(dry, tmp_path):
    asked = []

    def fetch(ticker):
        asked.append(ticker)
        if asked.count(ticker) > 1:
            return PLAIN                                               # the retry at the end gets through
        if ticker == "T2":
            raise _http(429)
        if ticker == "T4":
            return None
        raise _http(403)

    summary = dry.run(["T1", "T2", "T3", "T4", "T5"], fetch=fetch, put_row=None, out_dir=tmp_path / "out",
                      census_names={}, max_403=2)
    assert summary["stopped"] is None and summary["sec"]["http_403"] == 3 and summary["n_attempted"] == 5
    assert asked == ["T1", "T2", "T3", "T4", "T5", "T1", "T2", "T3", "T5"]
    assert summary["data"]["acceptance"] == "PASS" and summary["status"] == {"no_facts": 1, "reextracted": 4}


# ── deterministic output ────────────────────────────────────────────────────

def test_the_tag_census_and_the_summary_are_sorted(dry, tmp_path):
    names = [f"Zz{chr(97 + i)}DebtFacility" for i in range(12)]            # twelve tied tags, one filer each
    solo = {f"T{i:02d}": _facts(flow={REV: _s([2025, 2026], 10000)}, inst={names[11 - i]: _s([2026], 5)})
            for i in range(12)}                                            # met in descending name order
    solo["T99"] = _facts(flow={REV: _s([2025, 2026], 10000)},
                         inst={names[11]: _s([2026], 5), names[7]: _s([2026], 5)})        # two of them reach 2
    summary = _run(dry, solo, tmp_path)
    tags = json.loads((tmp_path / "out" / "unmapped_tags.json").read_text(encoding="utf-8"))
    assert [t["tag"] for t in tags] == sorted((t["tag"] for t in tags), key=lambda tag: (
        -next(x["tickers"] for x in tags if x["tag"] == tag), tag))
    assert [t["tag"] for t in tags][:2] == sorted([names[11], names[7]])      # the two with a count of 2, by name
    assert len(tags) == 12
    assert [t["tag"] for t in tags][2:] == sorted(set(names) - {names[11], names[7]})
    text = (tmp_path / "out" / "summary.json").read_text(encoding="utf-8")
    assert text == json.dumps(json.loads(text), indent=2, sort_keys=True)
    assert list(summary["239_debt"]["mapped_tags_present_tickers"]) == sorted(summary["239_debt"]["mapped_tags_present_tickers"])


def test_the_unmapped_tag_file_keeps_the_top_eighty_and_the_summary_counts_them_all(dry, tmp_path):
    solo = {f"T{i:03d}": _facts(flow={REV: _s([2025, 2026], 10000)}, inst={f"Zz{i:03d}DebtFacility": _s([2026], 5)})
            for i in range(85)}
    summary = _run(dry, solo, tmp_path)
    tags = json.loads((tmp_path / "out" / "unmapped_tags.json").read_text(encoding="utf-8"))
    assert [t["tag"] for t in tags] == [f"Zz{i:03d}DebtFacility" for i in range(80)]
    assert summary["239_debt"]["unmapped_tags_total"] == 85


# ── what the first hardening tests left to the mutations ────────────────────

@pytest.mark.parametrize("headers", [{}, {"User-Agent": ""}, {"User-Agent": "   "}, {"User-Agent": None}])
def test_a_missing_or_blank_user_agent_is_refused_even_when_the_header_helper_returns_it(dry, monkeypatch, headers):
    monkeypatch.setattr(se, "_headers", lambda: headers)
    with pytest.raises(SystemExit) as exc:
        dry.assert_real_contact()
    assert exc.value.code == 2


def test_the_built_in_default_is_refused_whatever_it_says(dry, monkeypatch):
    monkeypatch.setattr(se, "SEC_USER_AGENT_DEFAULT", "Plain Default Agent")
    monkeypatch.setattr(se, "_headers", lambda: {"User-Agent": "Plain Default Agent"})
    with pytest.raises(SystemExit):
        dry.assert_real_contact()


def test_a_contact_on_a_longer_domain_that_only_starts_like_the_placeholder_is_accepted(dry, monkeypatch):
    monkeypatch.setattr(se, "_headers", lambda: {"User-Agent": "Ann Researcher ann@example.network.nl"})
    dry.assert_real_contact()                                          # no refusal


def test_main_names_the_store_before_the_contact_when_both_are_wrong(dry, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(rawstore, "DB_PATH", tmp_path / "live" / "raw.duckdb")      # no marker
    monkeypatch.setenv("RC_SEC_USER_AGENT", "x (contact: y@example.com)")
    with pytest.raises(SystemExit):
        dry.main(["--tickers", "AAA", "--no-write", "--out", str(tmp_path / "out")])
    err = capsys.readouterr().err
    assert "throwaway copy" in err and "RC_SEC_USER_AGENT" not in err


def test_a_403_is_recognised_by_the_status_even_when_the_text_does_not_say_so(dry, tmp_path):
    request = httpx.Request("GET", "https://data.sec.gov/api/xbrl/companyfacts/CIK0000000001.json")

    def fetch(ticker):
        raise httpx.HTTPStatusError("Forbidden", request=request, response=httpx.Response(403, request=request))

    summary = dry.run(["T1", "T2", "T3"], fetch=fetch, put_row=None, out_dir=tmp_path / "out", census_names={}, max_403=2)
    assert summary["stopped"] == "consecutive_403" and summary["n_attempted"] == 2


def test_a_number_that_merely_contains_403_is_not_a_refusal(dry, tmp_path):
    def fetch(ticker):
        raise RuntimeError("read timed out after 14030 ms")

    summary = dry.run(["T1", "T2", "T3"], fetch=fetch, put_row=None, out_dir=tmp_path / "out", census_names={}, max_403=2)
    assert summary["stopped"] is None and summary["status"] == {"fetch_error": 3} and "http_403" not in summary["sec"]


def test_main_passes_the_403_limit_to_the_run(dry, monkeypatch, tmp_path):
    _copy_store(monkeypatch, tmp_path)
    tickers = ["AAA", "BBB", "CCC", "DDD"]
    _cik_world(monkeypatch, tickers)

    def refused(cik):
        raise _http(403)

    monkeypatch.setattr(se, "_fetch_facts", refused)
    summary = dry.main(["--tickers", ",".join(tickers), "--no-write", "--out", str(tmp_path / "out"), "--max-403", "2"])
    assert summary["stopped"] == "consecutive_403" and summary["n_attempted"] == 2


def test_main_passes_the_error_limit_to_the_run(dry, monkeypatch, tmp_path):
    _copy_store(monkeypatch, tmp_path)
    tickers = ["AAA", "BBB", "CCC", "DDD"]
    _cik_world(monkeypatch, tickers)

    def locked(table, rows):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(rawstore, "write_rows", locked)
    summary = dry.main(["--tickers", ",".join(tickers), "--out", str(tmp_path / "out"), "--max-errors", "2"])
    assert summary["stopped"] == "consecutive_errors" and summary["n_attempted"] == 2


def test_a_ticker_without_facts_neither_adds_to_nor_resets_the_error_count(dry, tmp_path):
    def put_row(row):
        raise RuntimeError("locked")

    summary = _run(dry, {"A1": PLAIN, "A2": PLAIN, "B1": None, "C1": PLAIN}, tmp_path, put_row=put_row, max_errors=3)
    assert summary["stopped"] == "consecutive_errors" and summary["status"] == {"analysis_error": 3, "no_facts": 1}


def test_the_summary_lists_at_most_fifty_errors_but_counts_and_logs_every_one(dry, tmp_path):
    def put_row(row):
        raise RuntimeError("locked")

    summary = _run(dry, {f"T{i:03d}": PLAIN for i in range(52)}, tmp_path, put_row=put_row, max_errors=100)
    assert len(summary["analysis_errors"]) == 50 and summary["status"] == {"analysis_error": 52}
    assert sum(1 for entry in _log(tmp_path / "out") if entry["status"] == "analysis_error") == 52


def test_a_stopped_run_that_already_moved_a_held_value_still_says_fail(dry, monkeypatch, tmp_path):
    real = dry.extract_annual_before_246

    def disagreeing(facts):
        annual = real(facts)
        annual[-1]["revenue"] = 1.0
        return annual

    def put_row(row):
        if row["ticker"] == "BBB":
            raise RuntimeError("locked")

    monkeypatch.setattr(dry, "extract_annual_before_246", disagreeing)
    summary = _run(dry, {"AAA": PLAIN, "BBB": PLAIN}, tmp_path, put_row=put_row, max_errors=1)
    assert summary["stopped"] == "consecutive_errors" and summary["data"]["acceptance"] == "FAIL"


def test_every_output_file_is_closed_even_when_the_run_is_interrupted(dry, monkeypatch, tmp_path):
    closed, real_close = [], dry._Csv.close
    monkeypatch.setattr(dry._Csv, "close", lambda self: closed.append(self) or real_close(self))

    def fetch(ticker):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        dry.run(["AAA"], fetch=fetch, put_row=None, out_dir=tmp_path / "out", census_names={})
    assert len(closed) == len(dry.CSV_FILES)


def test_a_resumed_run_keeps_the_errors_found_before_the_stop(dry, tmp_path):
    def put_row(row):
        if row["ticker"] == "BBB":
            raise RuntimeError("locked")

    names = {"CCC": "revenue", "AAA": "revenue"}
    whole = dry.run(sorted(RESUME_FACTS), fetch=lambda t: RESUME_FACTS[t], put_row=put_row, out_dir=tmp_path / "whole",
                    census_names=names)
    _interrupted_after(dry, tmp_path / "part", "CCC", put_row=put_row)
    resumed = dry.run(sorted(RESUME_FACTS), fetch=lambda t: RESUME_FACTS[t], put_row=put_row, out_dir=tmp_path / "part",
                      census_names=names, resume=True)
    assert resumed["analysis_errors"] == [{"ticker": "BBB", "stage": "write", "error": "RuntimeError: locked"}]
    assert resumed["status"] == {"analysis_error": 1, "reextracted": 6}
    assert resumed == whole


def test_the_census_and_the_status_come_out_sorted_whatever_the_order_the_tickers_arrive_in(dry, tmp_path):
    facts = {"CCC": PLAIN, "AAA": PLAIN, "BBB": None}
    summary = dry.run(["CCC", "AAA", "BBB"], fetch=lambda t: facts[t], put_row=None, out_dir=tmp_path / "out",
                      census_names={"CCC": "revenue", "AAA": "revenue"})
    assert [c["ticker"] for c in summary["census_ten"]] == ["AAA", "CCC"]
    census = json.loads((tmp_path / "out" / "census_ten.json").read_text(encoding="utf-8"))
    assert [c["ticker"] for c in census] == ["AAA", "CCC"]
    assert list(summary["status"]) == ["no_facts", "reextracted"]               # met in the other order


# ── the coordinator's review of ff5c537 (commit 8) ──────────────────────────

def _connect_error(ticker):
    raise httpx.ConnectError("connection refused")


def _script_exit(monkeypatch, argv: list[str]):
    """Run the file as ``python 246_reextract_dryrun.py <argv>``: the exit code of the process."""
    monkeypatch.setattr(sys, "argv", ["246_reextract_dryrun.py", *argv])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(_SCRIPT), run_name="__main__")
    return exc.value.code


def test_a_run_where_every_fetch_fails_is_incomplete_not_pass(dry, tmp_path):
    summary = dry.run(["A", "B", "C", "D"], fetch=_connect_error, put_row=None, out_dir=tmp_path / "out",
                      census_names={})
    assert summary["status"] == {"fetch_error": 4} and summary["stopped"] is None
    assert summary["data"]["acceptance"] == "INCOMPLETE"


def test_fetch_errors_other_than_403_stop_the_run_after_max_errors_in_a_row(dry, tmp_path):
    seen = []
    summary = dry.run([f"T{i}" for i in range(8)], fetch=lambda t: seen.append(t) or _connect_error(t), put_row=None,
                      out_dir=tmp_path / "out", census_names={}, max_errors=3)
    assert summary["stopped"] == "consecutive_errors" and seen == ["T0", "T1", "T2"]
    assert summary["data"]["acceptance"] == "INCOMPLETE"


def test_403s_fetch_errors_and_store_errors_all_count_toward_the_same_error_stop(dry, tmp_path):
    def fetch(ticker):
        if ticker == "T1":
            raise _http(403)
        if ticker == "T2":
            raise _http(503)
        return PLAIN

    def put_row(row):
        raise RuntimeError("locked")

    summary = dry.run(["T1", "T2", "T3", "T4"], fetch=fetch, put_row=put_row, out_dir=tmp_path / "out",
                      census_names={}, max_errors=3)
    assert summary["stopped"] == "consecutive_errors" and summary["n_attempted"] == 3


def test_403s_between_tickers_without_facts_still_end_in_the_error_stop_and_never_pass(dry, tmp_path):
    def fetch(ticker):
        if int(ticker[1:]) % 2:
            return None                                                # no facts: the SEC answered
        raise _http(403)

    summary = dry.run([f"T{i}" for i in range(10)], fetch=fetch, put_row=None, out_dir=tmp_path / "out",
                      census_names={}, max_403=2, max_errors=3)
    # T0 403, T1 none, T2 403, T3 none, T4 403: the third error in a row (a ticker without facts neither adds nor resets)
    assert summary["stopped"] == "consecutive_errors" and summary["sec"]["http_403"] == 3
    assert summary["data"]["acceptance"] == "INCOMPLETE"


def test_a_streak_of_failures_stays_behind_the_state_so_a_resume_asks_again(dry, tmp_path):
    tickers = [f"T{i}" for i in range(1, 9)]
    down = {"on": True}

    def fetch(ticker):
        if down["on"] and ticker in ("T4", "T5", "T6"):
            raise httpx.ConnectError("connection refused")
        return PLAIN

    first = dry.run(tickers, fetch=fetch, put_row=None, out_dir=tmp_path / "part", census_names={}, max_errors=3)
    assert first["stopped"] == "consecutive_errors"
    assert json.loads((tmp_path / "part" / "state.json").read_text(encoding="utf-8"))["last_ticker"] == "T3"
    down["on"] = False
    resumed = dry.run(tickers, fetch=fetch, put_row=None, out_dir=tmp_path / "part", census_names={}, resume=True)
    whole = dry.run(tickers, fetch=fetch, put_row=None, out_dir=tmp_path / "whole", census_names={})
    assert resumed == whole and resumed["data"]["acceptance"] == "PASS"
    for name in RESUME_CSVS:
        assert (tmp_path / "part" / name).read_text(encoding="utf-8") == (tmp_path / "whole" / name).read_text(
            encoding="utf-8"), name


def test_a_streak_of_store_errors_stays_behind_the_state_too(dry, tmp_path):
    tickers = [f"T{i}" for i in range(1, 7)]
    locked = {"on": True}

    def put_row(row):
        if locked["on"] and row["ticker"] in ("T3", "T4"):
            raise RuntimeError("locked")

    first = dry.run(tickers, fetch=lambda t: PLAIN, put_row=put_row, out_dir=tmp_path / "part", census_names={},
                    max_errors=2)
    assert first["stopped"] == "consecutive_errors"
    assert json.loads((tmp_path / "part" / "state.json").read_text(encoding="utf-8"))["last_ticker"] == "T2"
    locked["on"] = False
    resumed = dry.run(tickers, fetch=lambda t: PLAIN, put_row=put_row, out_dir=tmp_path / "part", census_names={},
                      resume=True)
    assert resumed["status"] == {"reextracted": 6} and resumed["analysis_errors"] == []


def test_a_finished_run_that_is_not_pass_keeps_its_state_and_a_pass_removes_it(dry, monkeypatch, tmp_path):
    def flaky(ticker):
        if ticker == "T2":
            raise httpx.ConnectError("connection refused")
        return PLAIN

    summary = dry.run(["T1", "T2", "T3"], fetch=flaky, put_row=None, out_dir=tmp_path / "incomplete", census_names={})
    assert summary["stopped"] is None and summary["data"]["acceptance"] == "INCOMPLETE"
    assert json.loads((tmp_path / "incomplete" / "state.json").read_text(encoding="utf-8"))["last_ticker"] == "T3"

    real = dry.extract_annual_before_246
    with monkeypatch.context() as patched:
        patched.setattr(dry, "extract_annual_before_246",
                        lambda facts: [{**r, "revenue": 1.0} if i == 0 else r for i, r in enumerate(real(facts))])
        summary = dry.run(["T1"], fetch=lambda t: PLAIN, put_row=None, out_dir=tmp_path / "fail", census_names={})
    assert summary["data"]["acceptance"] == "FAIL" and (tmp_path / "fail" / "state.json").exists()
    summary = dry.run(["T1"], fetch=lambda t: PLAIN, put_row=None, out_dir=tmp_path / "pass", census_names={})
    assert summary["data"]["acceptance"] == "PASS" and not (tmp_path / "pass" / "state.json").exists()


def test_the_process_exits_0_only_for_a_full_pass(dry, monkeypatch, tmp_path):
    _copy_store(monkeypatch, tmp_path)
    _table(monkeypatch, ["AAA", "BBB"])
    _cik_world(monkeypatch, ["AAA", "BBB"])
    assert _script_exit(monkeypatch, ["--no-write", "--out", str(tmp_path / "ok")]) == 0
    assert _script_exit(monkeypatch, ["--tickers", "AAA", "--no-write", "--out", str(tmp_path / "partial")]) == 1
    monkeypatch.setattr(se, "_fetch_facts", lambda cik: (_ for _ in ()).throw(httpx.ConnectError("down")))
    assert _script_exit(monkeypatch, ["--no-write", "--out", str(tmp_path / "down")]) == 1


# ── a hardlink or an --out inside the live store ────────────────────────────

def _live_world(dry, monkeypatch, tmp_path):
    live = tmp_path / "live" / ".store"
    live.mkdir(parents=True)
    (live / "raw.duckdb").write_bytes(b"live raw")
    (live / "edgar.duckdb").write_bytes(b"live edgar")
    monkeypatch.setattr(dry, "LIVE_STORE", live.resolve())
    copy = tmp_path / "rc246_dry" / ".store"
    copy.mkdir(parents=True)
    return live, copy


@pytest.mark.parametrize("linked", ["raw.duckdb", "edgar.duckdb", "raw.duckdb.wal"])
def test_a_hardlink_of_a_live_store_file_in_a_marked_folder_is_refused(dry, monkeypatch, tmp_path, linked):
    live, copy = _live_world(dry, monkeypatch, tmp_path)
    (live / "raw.duckdb.wal").write_bytes(b"live wal")
    (copy / "raw.duckdb").write_bytes(b"a real copy")
    (copy / "edgar.duckdb").write_bytes(b"a real copy")
    (copy / linked).unlink(missing_ok=True)
    os.link(live / linked, copy / linked)
    with pytest.raises(SystemExit) as exc:
        dry.assert_store_copy(copy / "raw.duckdb")
    assert exc.value.code == 2


def test_a_hardlink_under_any_file_name_is_refused(dry, monkeypatch, tmp_path):
    live, copy = _live_world(dry, monkeypatch, tmp_path)
    os.link(live / "raw.duckdb", copy / "renamed.db")
    with pytest.raises(SystemExit):
        dry.assert_store_copy(copy / "renamed.db")


def test_a_real_copy_next_to_the_live_store_is_accepted(dry, monkeypatch, tmp_path):
    live, copy = _live_world(dry, monkeypatch, tmp_path)
    (copy / "raw.duckdb").write_bytes(b"live raw")                     # same bytes, another file
    (copy / "edgar.duckdb").write_bytes(b"live edgar")
    dry.assert_store_copy(copy / "raw.duckdb")
    dry.assert_store_copy(copy / "not_created_yet.duckdb")             # no file yet: nothing to compare


@pytest.mark.parametrize("where", ["live", "inside_live", "store_dir", "inside_store_dir"])
def test_main_refuses_an_out_folder_inside_a_store_folder_before_creating_it(dry, monkeypatch, tmp_path, capsys, where):
    live, copy = _live_world(dry, monkeypatch, tmp_path)
    (copy / "raw.duckdb").write_bytes(b"x")
    monkeypatch.setattr(rawstore, "DB_PATH", copy / "raw.duckdb")
    out = {"live": live, "inside_live": live / "dry" / "out", "store_dir": copy,
           "inside_store_dir": copy / "out"}[where]
    before = sorted(p.name for p in out.parent.iterdir()) if out.parent.exists() else None
    with pytest.raises(SystemExit) as exc:                             # the autouse boom fails any request
        dry.main(["--tickers", "AAA", "--no-write", "--out", str(out)])
    assert exc.value.code == 2 and "--out" in capsys.readouterr().err
    assert (sorted(p.name for p in out.parent.iterdir()) if out.parent.exists() else None) == before


def test_main_refuses_an_out_folder_that_reaches_the_live_store_through_a_symlink(dry, monkeypatch, tmp_path):
    live, copy = _live_world(dry, monkeypatch, tmp_path)
    (copy / "raw.duckdb").write_bytes(b"x")
    monkeypatch.setattr(rawstore, "DB_PATH", copy / "raw.duckdb")
    link = tmp_path / "innocent"
    link.symlink_to(live, target_is_directory=True)
    with pytest.raises(SystemExit) as exc:
        dry.main(["--tickers", "AAA", "--no-write", "--out", str(link / "dryrun")])
    assert exc.value.code == 2 and not (live / "dryrun").exists()


def test_main_accepts_an_out_folder_beside_the_copy(dry, monkeypatch, tmp_path):
    _copy_store(monkeypatch, tmp_path)
    _cik_world(monkeypatch, ["AAA"])
    dry.main(["--tickers", "AAA", "--no-write", "--out", str(tmp_path / "rc246_dry" / "out")])
    assert (tmp_path / "rc246_dry" / "out" / "summary.json").exists()


# ── a partial run says so, and a resume must be the same run ────────────────

def _table(monkeypatch, tickers):
    monkeypatch.setattr(rawstore, "get_all", lambda table, cols: [{"ticker": t} for t in tickers])


def test_the_summary_records_the_scope_of_the_run(dry, monkeypatch, tmp_path):
    _copy_store(monkeypatch, tmp_path)
    tickers = ["AAA", "BBB", "CCC", "DDD"]
    _table(monkeypatch, tickers)
    _cik_world(monkeypatch, tickers)
    summary = dry.main(["--no-write", "--out", str(tmp_path / "full")])
    assert summary["scope"] == {"tickers": None, "limit": None, "start_after": None, "no_write": True, "partial": False}
    summary = dry.main(["--tickers", "ddd, bbb", "--limit", "1", "--no-write", "--out", str(tmp_path / "some")])
    assert summary["scope"] == {"tickers": ["BBB", "DDD"], "limit": 1, "start_after": None, "no_write": True,
                                "partial": True}
    monkeypatch.setattr(rawstore, "write_rows", lambda table, rows: None)            # a run that writes (the copy is fake)
    summary = dry.main(["--start-after", "bbb", "--out", str(tmp_path / "late")])
    assert summary["scope"] == {"tickers": None, "limit": None, "start_after": "BBB", "no_write": False, "partial": True}
    assert summary["n_attempted"] == 2
    assert json.loads((tmp_path / "late" / "summary.json").read_text(encoding="utf-8"))["scope"] == summary["scope"]


@pytest.mark.parametrize("argv, partial", [([], False), (["--tickers", "AAA,BBB,CCC"], True), (["--limit", "2"], True),
                                            (["--start-after", "AAA"], True), (["--overwrite"], False)])
def test_only_a_run_over_the_whole_table_is_not_partial(dry, monkeypatch, tmp_path, argv, partial):
    _copy_store(monkeypatch, tmp_path)
    tickers = ["AAA", "BBB", "CCC"]
    _table(monkeypatch, tickers)
    _cik_world(monkeypatch, tickers)
    summary = dry.main(["--no-write", "--out", str(tmp_path / "out"), *argv])
    assert summary["scope"]["partial"] is partial
    assert summary["data"]["acceptance"] == ("PARTIAL" if partial else "PASS")


def test_a_limit_and_a_start_after_work_from_the_scope_start(dry, monkeypatch, tmp_path):
    _copy_store(monkeypatch, tmp_path)
    tickers = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    _table(monkeypatch, tickers)
    fetched = _cik_world(monkeypatch, tickers)
    dry.main(["--no-write", "--out", str(tmp_path / "out"), "--start-after", "BBB", "--limit", "2"])
    assert fetched == [33, 44]                                         # CCC, DDD: strictly after BBB, two of them


def _stopped_main_run(dry, monkeypatch, tmp_path, argv, stop_at_cik):
    """``main(argv)`` interrupted while fetching the filer with this cik (cik 11, 22, ... for AAA, BBB, ...)."""
    def fetch(cik):
        if cik == stop_at_cik:
            raise KeyboardInterrupt
        return PLAIN

    monkeypatch.setattr(se, "_fetch_facts", fetch)
    with pytest.raises(KeyboardInterrupt):
        dry.main(argv)


@pytest.mark.parametrize("argv, expected", [
    (["--resume", "--limit", "2", "--no-write"], "--limit"),
    (["--resume", "--limit", "3"], "--no-write"),
    (["--resume", "--limit", "3", "--no-write", "--tickers", "AAA,BBB"], "--tickers")])
def test_a_resume_with_other_arguments_than_the_stopped_run_is_refused(dry, monkeypatch, tmp_path, capsys, argv,
                                                                         expected):
    _copy_store(monkeypatch, tmp_path)
    tickers = ["AAA", "BBB", "CCC", "DDD"]
    _table(monkeypatch, tickers)
    _cik_world(monkeypatch, tickers)
    out = str(tmp_path / "out")
    _stopped_main_run(dry, monkeypatch, tmp_path, ["--limit", "3", "--no-write", "--out", out], 22)
    capsys.readouterr()
    monkeypatch.setattr(se, "_fetch_facts", lambda cik: pytest.fail("a refused resume must not fetch"))
    with pytest.raises(SystemExit) as exc:
        dry.main([*argv, "--out", out])
    assert exc.value.code == 2 and expected in capsys.readouterr().err


def test_a_resume_finishes_the_scope_it_started_and_does_not_add_a_second_limit(dry, monkeypatch, tmp_path):
    _copy_store(monkeypatch, tmp_path)
    tickers = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    _table(monkeypatch, tickers)
    fetched = _cik_world(monkeypatch, tickers)
    out = str(tmp_path / "out")
    _stopped_main_run(dry, monkeypatch, tmp_path, ["--start-after", "AAA", "--limit", "3", "--no-write", "--out", out], 33)
    monkeypatch.setattr(se, "_fetch_facts", lambda cik: fetched.append(cik) or PLAIN)
    summary = dry.main(["--resume", "--limit", "3", "--no-write", "--out", out])
    assert fetched == [33, 44] and summary["status"] == {"reextracted": 3}      # BBB (before the stop), CCC, DDD
    assert summary["scope"]["start_after"] == "AAA" and summary["scope"]["limit"] == 3


def test_a_direct_resume_takes_the_scope_from_the_state(dry, tmp_path):
    out = tmp_path / "out"
    _interrupted_after(dry, out, "CCC", scope=MAIN_SCOPE)
    summary = dry.run(sorted(RESUME_FACTS), fetch=lambda t: RESUME_FACTS[t], put_row=None, out_dir=out,
                      census_names={}, resume=True)
    assert summary["scope"] == MAIN_SCOPE


def test_a_state_of_another_version_of_the_script_is_refused(dry, monkeypatch, tmp_path, capsys):
    _copy_store(monkeypatch, tmp_path)
    out = tmp_path / "out"
    _interrupted_after(dry, out, "CCC", scope=MAIN_SCOPE)
    state = json.loads((out / "state.json").read_text(encoding="utf-8"))
    state["version"] = 3                                               # what commit 8 wrote
    (out / "state.json").write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        dry.main(["--tickers", ",".join(sorted(RESUME_FACTS)), "--no-write", "--out", str(out), "--resume"])
    assert exc.value.code == 2 and "--overwrite" in capsys.readouterr().err


# ── an emptied output file, the timing, the fsync ───────────────────────────

def test_rewinding_to_a_size_of_zero_does_not_need_the_file(dry, tmp_path):
    dry._rewind_outputs(tmp_path, {"gone.csv": 0})                     # no error
    (tmp_path / "long.csv").write_text("abc", encoding="utf-8")
    dry._rewind_outputs(tmp_path, {"long.csv": 0})
    assert (tmp_path / "long.csv").stat().st_size == 0
    with pytest.raises(ValueError, match="gone.csv"):
        dry._rewind_outputs(tmp_path, {"gone.csv": 5})


def test_the_timing_is_in_run_info_and_summary_json_holds_the_data_only(dry, tmp_path):
    out = tmp_path / "out"
    summary = dry.run(["AAA", "BBB"], fetch=lambda t: PLAIN, put_row=None, out_dir=out, census_names={})
    assert "elapsed_seconds" not in summary and "resumed_after" not in summary
    assert "elapsed_seconds" not in (out / "summary.json").read_text(encoding="utf-8")
    info = _info(out)
    assert set(info) == {"elapsed_seconds", "resumed_after", "stopped"} and info["stopped"] is None
    assert isinstance(info["elapsed_seconds"], float) and info["resumed_after"] is None


def test_every_output_file_is_fsynced_before_the_state_that_counts_it_is_saved(dry, monkeypatch, tmp_path):
    out = tmp_path / "out"
    synced, snapshots, state_synced, inodes = [], [], [], {}
    names = [dry.LOG_FILE, *(f for f, _ in dry.CSV_FILES.values())]
    real_fsync, real_save = os.fsync, dry._save_state
    monkeypatch.setattr(os, "fsync", lambda fd: synced.append(os.fstat(fd).st_ino) or real_fsync(fd))

    def save(*args):
        snapshots.append(list(synced))
        inodes.update({name: (out / name).stat().st_ino for name in names})    # (a finished run replaces the csvs)
        real_save(*args)
        state_synced.append((out / "state.json").stat().st_ino in synced)

    monkeypatch.setattr(dry, "_save_state", save)
    facts = {"AAA": PLAIN, "BBB": CHAIN, "CCC": PLAIN}                    # only BBB adds years: rows in added_values.csv
    dry.run(sorted(facts), fetch=lambda t: facts[t], put_row=None, out_dir=out, census_names={})
    assert len(snapshots) == 3 and state_synced == [True, True, True]
    assert all(ino in snapshots[0] for ino in inodes.values())            # headers: before the first state
    assert [snap.count(inodes[dry.LOG_FILE]) for snap in snapshots] == [1, 2, 3]   # the log: every ticker
    assert [snap.count(inodes["added_values.csv"]) for snap in snapshots] == [1, 2, 2]   # BBB's rows once, nothing more


# ── commit 9: the retry pass, PARTIAL, the .env store, the run history ──────

def _first_call_fails(make_error, *failing, facts=None):
    """``fetch`` that raises ``make_error()`` for each ticker in ``failing`` on its FIRST call only."""
    calls = {}

    def fetch(ticker):
        calls[ticker] = calls.get(ticker, 0) + 1
        if ticker in failing and calls[ticker] == 1:
            raise make_error()
        return (facts or {}).get(ticker, PLAIN)

    fetch.calls = calls
    return fetch


NO_RETRY = {"retried_n": 0, "recovered_n": 0, "still_failed_n": 0, "awaiting_retry_n": 0, "retried": [],
            "still_failed": [], "awaiting_retry": []}


def test_a_clean_run_has_an_empty_retry_section(dry, tmp_path):
    assert _run(dry, {"AAA": PLAIN}, tmp_path)["retry"] == NO_RETRY


def test_an_isolated_fetch_failure_is_retried_once_at_the_end_and_a_recovery_is_a_pass(dry, tmp_path):
    fetch = _first_call_fails(lambda: httpx.ReadTimeout("read timed out"), "T2", facts={"T2": CHAIN})
    out = tmp_path / "out"
    summary = dry.run(["T1", "T2", "T3"], fetch=fetch, put_row=None, out_dir=out, census_names={})
    assert fetch.calls == {"T1": 1, "T2": 2, "T3": 1}
    assert summary["data"]["acceptance"] == "PASS" and summary["status"] == {"reextracted": 3}
    assert summary["n_attempted"] == 3 and summary["stopped"] is None
    assert summary["retry"] == {**NO_RETRY, "retried_n": 1, "recovered_n": 1, "retried": ["T2"]}
    assert not (out / "state.json").exists()
    assert "T2" in {r["ticker"] for r in _rows(tmp_path, "added_values.csv")}       # its rows are in the same csv
    assert [(r["ticker"], r["status"], r.get("retry", False)) for r in _log(out)] == [
        ("T1", "reextracted", False), ("T2", "fetch_error", False), ("T3", "reextracted", False),
        ("T2", "reextracted", True)]


@pytest.mark.parametrize("make_error, kind", [(lambda: httpx.ReadTimeout("read timed out"), "fetch_error"),
                                              (lambda: _http(403), "403")])
def test_a_ticker_that_fails_twice_leaves_the_run_incomplete_and_is_named(dry, tmp_path, make_error, kind):
    asked = []

    def fetch(ticker):
        asked.append(ticker)
        if ticker == "T2":
            raise make_error()
        return PLAIN

    summary = dry.run(["T1", "T2", "T3"], fetch=fetch, put_row=None, out_dir=tmp_path / "out", census_names={})
    assert asked.count("T2") == 2                                       # once more, not again and again
    assert summary["data"]["acceptance"] == "INCOMPLETE" and summary["status"] == {"fetch_error": 1, "reextracted": 2}
    error = dry._error_row(make_error())[0]
    assert summary["retry"] == {**NO_RETRY, "retried_n": 1, "still_failed_n": 1, "retried": ["T2"],
                                "still_failed": [{"ticker": "T2", "kind": kind, "error": error}]}


def test_an_isolated_403_that_clears_is_a_pass_and_still_counted_as_an_sec_event(dry, tmp_path):
    fetch = _first_call_fails(lambda: _http(403), "T2")
    summary = dry.run(["T1", "T2", "T3"], fetch=fetch, put_row=None, out_dir=tmp_path / "out", census_names={})
    assert summary["data"]["acceptance"] == "PASS" and summary["sec"] == {"calls": 4, "errors": 1, "http_403": 1}


def test_a_retry_that_finds_no_facts_is_a_recovery(dry, tmp_path):
    calls = []

    def fetch(ticker):
        calls.append(ticker)
        if ticker == "T2":
            if calls.count("T2") == 1:
                raise httpx.ReadTimeout("read timed out")
            return None
        return PLAIN

    summary = dry.run(["T1", "T2"], fetch=fetch, put_row=None, out_dir=tmp_path / "out", census_names={})
    assert summary["status"] == {"no_facts": 1, "reextracted": 1} and summary["retry"]["recovered_n"] == 1
    assert summary["data"]["acceptance"] == "PASS"
    assert [(r["status"], r.get("retry", False)) for r in _log(tmp_path / "out") if r["ticker"] == "T2"] == [
        ("fetch_error", False), ("no_facts", True)]


def test_every_kind_of_retry_outcome_is_logged_as_a_retry(dry, tmp_path):
    asked = []

    def fetch(ticker):
        asked.append(ticker)
        if asked.count(ticker) == 1 and ticker in ("T2", "T3"):
            raise httpx.ReadTimeout("read timed out")
        if ticker == "T2" and asked.count(ticker) == 2:
            raise dry.NoCik(ticker)                                    # (can only be a map that changed meanwhile)
        return PLAIN

    def put_row(row):
        if row["ticker"] == "T3":
            raise RuntimeError("locked")

    out = tmp_path / "out"
    summary = dry.run(["T1", "T2", "T3"], fetch=fetch, put_row=put_row, out_dir=out, census_names={})
    assert [(r["ticker"], r["status"]) for r in _log(out) if r.get("retry")] == [("T2", "no_cik"), ("T3", "analysis_error")]
    assert summary["status"] == {"analysis_error": 1, "no_cik": 1, "reextracted": 1} and summary["retry"]["recovered_n"] == 0
    assert summary["data"]["acceptance"] == "INCOMPLETE" and summary["analysis_errors"][0]["ticker"] == "T3"


def test_store_and_analysis_errors_are_not_retried(dry, tmp_path):
    asked = []

    def fetch(ticker):
        asked.append(ticker)
        return PLAIN

    def put_row(row):
        if row["ticker"] == "T2":
            raise RuntimeError("locked")

    summary = dry.run(["T1", "T2", "T3"], fetch=fetch, put_row=put_row, out_dir=tmp_path / "out", census_names={})
    assert asked == ["T1", "T2", "T3"] and summary["retry"] == NO_RETRY
    assert summary["data"]["acceptance"] == "INCOMPLETE"


def test_the_retry_pass_obeys_the_403_limit_and_a_resume_retries_what_is_left(dry, tmp_path):
    tickers = [f"T{i:02d}" for i in range(1, 11)]
    blocked = {"on": True}

    def fetch(ticker):
        if int(ticker[1:]) % 2 == 0 and blocked["on"]:
            raise _http(403)
        return PLAIN

    out = tmp_path / "out"
    first = dry.run(tickers, fetch=fetch, put_row=None, out_dir=out, census_names={}, max_403=2)
    # T02 T04 T06 T08 T10 refused (each between successes); the retry pass takes T02 T04 and stops at the second 403
    assert first["stopped"] == "consecutive_403" and first["data"]["acceptance"] == "INCOMPLETE"
    assert first["retry"]["retried"] == ["T02", "T04"] and first["retry"]["awaiting_retry"] == ["T06", "T08", "T10"]
    assert first["retry"]["still_failed_n"] == 2 and first["retry"]["awaiting_retry_n"] == 3
    blocked["on"] = False
    resumed = dry.run(tickers, fetch=fetch, put_row=None, out_dir=out, census_names={}, resume=True, max_403=2)
    assert resumed["stopped"] is None and resumed["data"]["acceptance"] == "PASS"
    assert resumed["status"] == {"reextracted": 10} and resumed["retry"]["recovered_n"] == 4     # T02 T04 T06 T08
    assert not (out / "state.json").exists()


def test_a_resume_after_an_interrupt_in_the_retry_pass_retries_only_what_is_left(dry, tmp_path):
    calls, asked, interrupt = {}, [], {"on": True}

    def fetch(ticker):
        asked.append(ticker)
        calls[ticker] = calls.get(ticker, 0) + 1
        if ticker in ("T2", "T4") and calls[ticker] == 1:
            raise httpx.ReadTimeout("read timed out")
        if ticker == "T4" and calls[ticker] == 2 and interrupt["on"]:
            raise KeyboardInterrupt
        return PLAIN

    tickers, out = [f"T{i}" for i in range(1, 7)], tmp_path / "out"
    with pytest.raises(KeyboardInterrupt):
        dry.run(tickers, fetch=fetch, put_row=None, out_dir=out, census_names={})
    state = json.loads((out / "state.json").read_text(encoding="utf-8"))
    assert state["last_ticker"] == "T6"                                  # the retry pass is after the last ticker
    interrupt["on"] = False
    asked.clear()
    resumed = dry.run(tickers, fetch=fetch, put_row=None, out_dir=out, census_names={}, resume=True)
    assert asked == ["T4"]                                               # T2 recovered before the interrupt
    assert resumed["data"]["acceptance"] == "PASS" and resumed["status"] == {"reextracted": 6}
    assert resumed["retry"] == {**NO_RETRY, "retried_n": 2, "recovered_n": 2, "retried": ["T2", "T4"]}


def test_a_resume_that_has_no_new_tickers_still_marks_the_last_ticker_while_it_retries(dry, tmp_path):
    calls, asked, interrupt = {}, [], {"at": "T4"}

    def fetch(ticker):
        asked.append(ticker)
        calls[ticker] = calls.get(ticker, 0) + 1
        if ticker in ("T2", "T4", "T5") and calls[ticker] == 1:
            raise httpx.ReadTimeout("read timed out")
        if ticker == interrupt["at"] and calls[ticker] == 2:
            raise KeyboardInterrupt
        return PLAIN

    tickers, out = [f"T{i}" for i in range(1, 7)], tmp_path / "out"
    with pytest.raises(KeyboardInterrupt):
        dry.run(tickers, fetch=fetch, put_row=None, out_dir=out, census_names={})      # T2 retried, T4 interrupted
    interrupt["at"] = "T5"
    with pytest.raises(KeyboardInterrupt):
        dry.run(tickers, fetch=fetch, put_row=None, out_dir=out, census_names={}, resume=True)   # T4 retried, T5 not
    state = json.loads((out / "state.json").read_text(encoding="utf-8"))
    assert state["last_ticker"] == "T6" and state["acc"]["failed"]["T5"]["retried"] is False
    interrupt["at"] = None
    asked.clear()
    resumed = dry.run(tickers, fetch=fetch, put_row=None, out_dir=out, census_names={}, resume=True)
    assert asked == ["T5"]                                                              # no ticker is asked twice
    assert resumed["data"]["acceptance"] == "PASS" and resumed["status"] == {"reextracted": 6}


def test_a_finished_incomplete_run_is_not_retried_again_by_a_resume(dry, tmp_path):
    asked = []

    def fetch(ticker):
        asked.append(ticker)
        if ticker == "T2":
            raise httpx.ReadTimeout("read timed out")
        return PLAIN

    out = tmp_path / "out"
    first = dry.run(["T1", "T2", "T3"], fetch=fetch, put_row=None, out_dir=out, census_names={})
    assert asked.count("T2") == 2 and (out / "state.json").exists()
    again = dry.run(["T1", "T2", "T3"], fetch=fetch, put_row=None, out_dir=out, census_names={}, resume=True)
    assert asked.count("T2") == 2 and again == first
    assert json.loads((out / "state.json").read_text(encoding="utf-8"))["last_ticker"] == "T3"


def test_the_retry_lists_are_capped_but_the_counts_are_not(dry, tmp_path):
    tickers = [f"T{i:03d}" for i in range(450)]

    def fetch(ticker):
        if int(ticker[1:]) % 2:
            raise httpx.ReadTimeout("read timed out")
        return PLAIN

    summary = dry.run(tickers, fetch=fetch, put_row=None, out_dir=tmp_path / "out", census_names={},
                      max_403=1000, max_errors=1000)
    retry = summary["retry"]
    assert retry["retried_n"] == retry["still_failed_n"] == 225
    assert len(retry["retried"]) == len(retry["still_failed"]) == 200 and retry["retried"][0] == "T001"


def test_the_awaiting_list_is_capped_when_the_retry_pass_stops_early(dry, tmp_path):
    tickers = [f"T{i:03d}" for i in range(450)]

    def fetch(ticker):
        if int(ticker[1:]) % 2:
            raise httpx.ReadTimeout("read timed out")
        return PLAIN

    summary = dry.run(tickers, fetch=fetch, put_row=None, out_dir=tmp_path / "out", census_names={}, max_403=1000,
                      max_errors=2)
    retry = summary["retry"]
    assert summary["stopped"] == "consecutive_errors" and retry["retried_n"] == 2          # two retries, then the stop
    assert retry["awaiting_retry_n"] == 223 and len(retry["awaiting_retry"]) == 200
    assert retry["awaiting_retry"][0] == "T005" and retry["still_failed_n"] == 2


def test_the_time_budget_stops_the_retry_pass_too(dry, monkeypatch, tmp_path):
    ticks = iter(range(0, 10000, 10))
    monkeypatch.setattr(dry, "time", types.SimpleNamespace(monotonic=lambda: next(ticks), time=lambda: 1.0))
    asked = []

    def fetch(ticker):
        asked.append(ticker)
        if ticker in ("T2", "T3"):
            raise httpx.ReadTimeout("read timed out")
        return PLAIN

    # the clock moves 10 per look: t0, then one look per ticker of the main pass (four), then one per retry
    summary = dry.run(["T1", "T2", "T3", "T4"], fetch=fetch, put_row=None, out_dir=tmp_path / "out", census_names={},
                      max_seconds=55)
    assert summary["stopped"] == "budget" and summary["retry"]["retried"] == ["T2"]
    assert summary["retry"]["awaiting_retry"] == ["T3"] and asked == ["T1", "T2", "T3", "T4", "T2"]


# ── a partial run is PARTIAL, not PASS ──────────────────────────────────────

@pytest.mark.parametrize("scope, expected", [({"partial": True}, "PARTIAL"), ({"partial": False}, "PASS"), ({}, "PASS"),
                                             (None, "PASS")])
def test_a_clean_partial_run_is_partial_and_leaves_no_state(dry, tmp_path, scope, expected):
    summary = dry.run(["AAA", "BBB"], fetch=lambda t: PLAIN, put_row=None, out_dir=tmp_path / "out", census_names={},
                      scope=scope)
    assert summary["data"]["acceptance"] == expected and not (tmp_path / "out" / "state.json").exists()


def test_a_partial_run_that_failed_or_is_incomplete_keeps_those_verdicts(dry, monkeypatch, tmp_path):
    scope = {"partial": True}
    down = dry.run(["AAA"], fetch=_connect_error, put_row=None, out_dir=tmp_path / "down", census_names={}, scope=scope)
    assert down["data"]["acceptance"] == "INCOMPLETE" and (tmp_path / "down" / "state.json").exists()
    real = dry.extract_annual_before_246
    monkeypatch.setattr(dry, "extract_annual_before_246",
                        lambda facts: [{**r, "revenue": 1.0} if i == 0 else r for i, r in enumerate(real(facts))])
    moved = dry.run(["AAA"], fetch=lambda t: PLAIN, put_row=None, out_dir=tmp_path / "moved", census_names={}, scope=scope)
    assert moved["data"]["acceptance"] == "FAIL"


# ── an RC_STORE_DIR that .env sets is the live store too ────────────────────

def _env_world(dry, monkeypatch, tmp_path, line="RC_STORE_DIR={store}"):
    """A live store named only by ``.env`` (the repo's own ``.store`` stands elsewhere)."""
    env_store = tmp_path / "data" / "rc_store"
    env_store.mkdir(parents=True)
    (env_store / "raw.duckdb").write_bytes(b"live raw")
    env_file = tmp_path / "repo" / ".env"
    env_file.parent.mkdir()
    env_file.write_text("EDGAR_CONTACT=hush-hush-value\n" + line.format(store=env_store) + "\nOTHER=1\n", encoding="utf-8")
    monkeypatch.setattr(dry, "ENV_FILE", env_file)
    monkeypatch.setattr(dry, "LIVE_STORE", (tmp_path / "repo" / ".store").resolve())
    return env_store


@pytest.mark.parametrize("line", ["RC_STORE_DIR={store}", 'RC_STORE_DIR="{store}"  # the real one',
                                  "export RC_STORE_DIR = '{store}'"])
def test_a_marked_folder_inside_the_store_that_env_names_is_refused(dry, monkeypatch, tmp_path, capsys, line):
    env_store = _env_world(dry, monkeypatch, tmp_path, line)
    copy = env_store / "rc246_dry"
    copy.mkdir()
    with pytest.raises(SystemExit) as exc:
        dry.assert_store_copy(copy / "raw.duckdb")
    assert exc.value.code == 2 and "hush-hush-value" not in capsys.readouterr().err


def test_the_store_that_env_names_is_refused_even_with_the_marker_in_its_own_name(dry, monkeypatch, tmp_path):
    env_store = _env_world(dry, monkeypatch, tmp_path)
    marked = env_store.parent / "rc246_dry_store"
    env_store.rename(marked)
    (tmp_path / "repo" / ".env").write_text(f"RC_STORE_DIR={marked}\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        dry.assert_store_copy(marked / "raw.duckdb")


def test_a_hardlink_of_a_file_in_the_store_that_env_names_is_refused(dry, monkeypatch, tmp_path):
    env_store = _env_world(dry, monkeypatch, tmp_path)
    copy = tmp_path / "rc246_dry"
    copy.mkdir()
    os.link(env_store / "raw.duckdb", copy / "raw.duckdb")
    with pytest.raises(SystemExit):
        dry.assert_store_copy(copy / "raw.duckdb")


def test_main_refuses_an_out_folder_inside_the_store_that_env_names(dry, monkeypatch, tmp_path, capsys):
    env_store = _env_world(dry, monkeypatch, tmp_path)
    copy = tmp_path / "rc246_dry"
    copy.mkdir()
    (copy / "raw.duckdb").write_bytes(b"x")
    monkeypatch.setattr(rawstore, "DB_PATH", copy / "raw.duckdb")
    with pytest.raises(SystemExit) as exc:
        dry.main(["--tickers", "AAA", "--no-write", "--out", str(env_store / "out")])
    assert exc.value.code == 2 and not (env_store / "out").exists()


def test_a_store_dir_of_env_is_followed_through_a_symlink_and_a_home_shorthand(dry, monkeypatch, tmp_path):
    env_store = _env_world(dry, monkeypatch, tmp_path)
    link = tmp_path / "link_to_store"
    link.symlink_to(env_store, target_is_directory=True)
    (tmp_path / "repo" / ".env").write_text(f"RC_STORE_DIR={link}\n", encoding="utf-8")
    copy = env_store / "rc246_dry"
    copy.mkdir()
    with pytest.raises(SystemExit):
        dry.assert_store_copy(copy / "raw.duckdb")                       # the copy sits inside the real folder
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "repo" / ".env").write_text("RC_STORE_DIR=~/data/rc_store\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        dry.assert_store_copy(copy / "raw.duckdb")


def test_the_retry_section_lists_tickers_in_sorted_order_whatever_order_they_were_recorded_in(dry):
    acc = dry._Acc()
    acc.retried = ["T9", "T2", "T5"]
    acc.failed = {"T9": {"kind": "fetch_error", "error": "e9", "retried": True},
                  "T7": {"kind": "403", "error": "e7", "retried": False},
                  "T2": {"kind": "403", "error": "e2", "retried": True},
                  "T1": {"kind": "fetch_error", "error": "e1", "retried": False}}
    section = dry._retry_section(acc)
    assert section["retried"] == ["T2", "T5", "T9"]
    assert [e["ticker"] for e in section["still_failed"]] == ["T2", "T9"]
    assert section["awaiting_retry"] == ["T1", "T7"]


@pytest.mark.parametrize("content", [None, "OTHER=1\n", "RC_STORE_DIR=\n", "RC_STORE_DIR=   \n", 'RC_STORE_DIR="   "\n',
                                     "# RC_STORE_DIR=/live\n"])
def test_an_env_without_a_store_dir_adds_no_live_store(dry, monkeypatch, tmp_path, content):
    env_file = tmp_path / ".env"
    if content is not None:
        env_file.write_text(content, encoding="utf-8")
    monkeypatch.setattr(dry, "ENV_FILE", env_file)
    monkeypatch.setattr(dry, "LIVE_STORE", (tmp_path / "repo" / ".store").resolve())
    copy = tmp_path / "rc246_dry"
    copy.mkdir()
    (copy / "raw.duckdb").write_bytes(b"x")
    dry.assert_store_copy(copy / "raw.duckdb")                          # no refusal
    assert dry._live_stores() == [dry.LIVE_STORE]                       # a blank value names no folder


# ── run_info.json is the history of the runs of a folder ────────────────────

def test_run_info_keeps_the_history_of_every_run_of_the_folder(dry, tmp_path):
    out = tmp_path / "part"
    _interrupted_after(dry, out, "CCC")
    dry.run(sorted(RESUME_FACTS), fetch=lambda t: RESUME_FACTS[t], put_row=None, out_dir=out, census_names={},
            resume=True)
    history = json.loads((out / "run_info.json").read_text(encoding="utf-8"))["history"]
    assert [(h["resumed_after"], h["stopped"]) for h in history] == [(None, "interrupted"), ("CCC", None)]
    assert all(isinstance(h["elapsed_seconds"], float) for h in history)


def test_a_fresh_run_starts_a_new_history(dry, tmp_path):
    out = tmp_path / "out"
    for _ in range(2):
        dry.run(["AAA"], fetch=lambda t: PLAIN, put_row=None, out_dir=out, census_names={})
    assert len(json.loads((out / "run_info.json").read_text(encoding="utf-8"))["history"]) == 1


@pytest.mark.parametrize("damage", ["{not json", "{}", '{"history": 5}', None])
def test_a_damaged_or_missing_run_info_does_not_stop_a_resume(dry, tmp_path, damage):
    part = tmp_path / "part"
    _interrupted_after(dry, part, "CCC")
    if damage is None:
        (part / "run_info.json").unlink()
    else:
        (part / "run_info.json").write_text(damage, encoding="utf-8")
    dry.run(sorted(RESUME_FACTS), fetch=lambda t: RESUME_FACTS[t], put_row=None, out_dir=part, census_names={},
            resume=True)
    assert [h["resumed_after"] for h in json.loads((part / "run_info.json").read_text(encoding="utf-8"))["history"]] == ["CCC"]


# ── commit 10: the outputs of a retried run, what counts as a recovery, an interrupt in the retry pass ─────

CSV_OUTPUTS = ("changed_existing.csv", "unexplained_derived.csv", "total_debt_moves.csv", "added_values.csv",
               "verdict_changes.csv", "gate_flips.csv", "stale_margins.csv", "sti_flips.csv", "census_ten.json",
               "unmapped_tags.json")
CENSUS = {"CCC": "revenue", "AAA": "revenue"}


def _clean_run(dry, out: Path) -> dict:
    return dry.run(sorted(RESUME_FACTS), fetch=lambda t: RESUME_FACTS[t], put_row=None, out_dir=out,
                   census_names=CENSUS)


def _same_outputs(a: Path, b: Path) -> None:
    for name in CSV_OUTPUTS:
        assert (a / name).read_bytes() == (b / name).read_bytes(), name


def _tickers_in(out: Path, name: str) -> list[str]:
    with (out / name).open(encoding="utf-8", newline="") as f:
        return [r["ticker"] for r in csv.DictReader(f)]


def test_a_run_that_retried_writes_the_same_bytes_as_a_clean_run(dry, tmp_path):
    clean = _clean_run(dry, tmp_path / "clean")
    fetch = _first_call_fails(lambda: httpx.ReadTimeout("read timed out"), "AAA", "CCC", "DDD", facts=RESUME_FACTS)
    retried = dry.run(sorted(RESUME_FACTS), fetch=fetch, put_row=None, out_dir=tmp_path / "retried",
                      census_names=CENSUS)
    assert retried["retry"]["recovered_n"] == 3 and retried["data"]["acceptance"] == clean["data"]["acceptance"]
    _same_outputs(tmp_path / "retried", tmp_path / "clean")
    assert {k: v for k, v in retried.items() if k not in ("sec", "retry")} == {
        k: v for k, v in clean.items() if k not in ("sec", "retry")}
    for name in ("added_values.csv", "gate_flips.csv", "verdict_changes.csv"):
        seen = _tickers_in(tmp_path / "retried", name)
        assert seen == sorted(seen) and len(set(seen)) > 2, name         # a world where the order could differ


def test_a_stopped_run_leaves_its_files_as_written_and_the_resume_that_finishes_them_sorts_them(dry, tmp_path):
    blocked = {"on": True}
    calls = {}

    def fetch(ticker):
        calls[ticker] = calls.get(ticker, 0) + 1
        if ticker == "AAA" and calls[ticker] == 1:
            raise httpx.ReadTimeout("read timed out")
        if ticker in ("CCC", "EEE") and blocked["on"]:
            raise _http(403)
        return RESUME_FACTS[ticker]

    clean = _clean_run(dry, tmp_path / "clean")
    out = tmp_path / "out"
    first = dry.run(sorted(RESUME_FACTS), fetch=fetch, put_row=None, out_dir=out, census_names=CENSUS, max_403=2)
    assert first["stopped"] == "consecutive_403" and first["retry"]["retried"] == ["AAA", "CCC", "EEE"]
    assert first["retry"]["recovered_n"] == 1                            # AAA: its rows come after those of GGG
    seen = _tickers_in(out, "added_values.csv")
    assert seen != sorted(seen)                                          # AAA's rows are at the end for now
    blocked["on"] = False
    dry.run(sorted(RESUME_FACTS), fetch=fetch, put_row=None, out_dir=out, census_names=CENSUS, resume=True)
    _same_outputs(out, tmp_path / "clean")
    assert not (out / "state.json").exists() and clean["data"]["acceptance"] == "PASS"


def test_a_finished_incomplete_run_has_sorted_files_and_a_state_that_counts_exactly_them(dry, tmp_path):
    def fetch(ticker):
        if ticker == "CCC":
            raise httpx.ReadTimeout("read timed out")
        return RESUME_FACTS[ticker]

    flaky = _first_call_fails(lambda: httpx.ReadTimeout("read timed out"), "AAA", facts=RESUME_FACTS)
    calls = []

    def both(ticker):
        calls.append(ticker)
        return fetch(ticker) if ticker == "CCC" else flaky(ticker)

    out = tmp_path / "out"
    summary = dry.run(sorted(RESUME_FACTS), fetch=both, put_row=None, out_dir=out, census_names=CENSUS)
    assert summary["data"]["acceptance"] == "INCOMPLETE" and summary["retry"]["recovered_n"] == 1
    state = json.loads((out / "state.json").read_text(encoding="utf-8"))
    for name in ("added_values.csv", "gate_flips.csv", "verdict_changes.csv"):
        seen = _tickers_in(out, name)
        assert seen == sorted(seen), name
    assert state["sizes"] == {name: (out / name).stat().st_size for name in state["sizes"]}
    before = {name: (out / name).read_bytes() for name in CSV_OUTPUTS}
    again = dry.run(sorted(RESUME_FACTS), fetch=both, put_row=None, out_dir=out, census_names=CENSUS, resume=True)
    assert again == summary and calls.count("CCC") == 2
    assert {name: (out / name).read_bytes() for name in CSV_OUTPUTS} == before


def test_a_retry_that_ends_in_an_analysis_error_is_not_a_recovery(dry, tmp_path):
    fetch = _first_call_fails(lambda: httpx.ReadTimeout("read timed out"), "T2")

    def put_row(row):
        if row["ticker"] == "T2":
            raise RuntimeError("locked")

    summary = dry.run(["T1", "T2", "T3"], fetch=fetch, put_row=put_row, out_dir=tmp_path / "out", census_names={})
    assert summary["status"] == {"analysis_error": 1, "reextracted": 2}
    assert summary["retry"] == {**NO_RETRY, "retried_n": 1, "retried": ["T2"]}          # asked again, not recovered
    assert summary["analysis_errors"][0]["ticker"] == "T2" and summary["data"]["acceptance"] == "INCOMPLETE"


@pytest.mark.parametrize("where", ["fetch", "write"])
def test_an_interrupt_in_the_retry_pass_leaves_the_ticker_waiting_in_the_summary(dry, tmp_path, where):
    calls, armed = {}, {"on": True}

    def fetch(ticker):
        calls[ticker] = calls.get(ticker, 0) + 1
        if ticker == "T2" and calls[ticker] == 1:
            raise httpx.ReadTimeout("read timed out")
        if where == "fetch" and ticker == "T2" and armed["on"]:
            raise KeyboardInterrupt
        return PLAIN

    def put_row(row):
        if where == "write" and row["ticker"] == "T2" and armed["on"]:
            raise KeyboardInterrupt

    out = tmp_path / "out"
    with pytest.raises(KeyboardInterrupt):
        dry.run(["T1", "T2", "T3"], fetch=fetch, put_row=put_row, out_dir=out, census_names={})
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["stopped"] == "interrupted" and summary["data"]["acceptance"] == "INCOMPLETE"
    assert summary["status"] == {"fetch_error": 1, "reextracted": 2}               # T2 is still the failure it was
    assert summary["retry"] == {**NO_RETRY, "awaiting_retry_n": 1, "awaiting_retry": ["T2"]}
    state = json.loads((out / "state.json").read_text(encoding="utf-8"))["acc"]
    assert state["failed"]["T2"]["retried"] is False and state["retried"] == []
    armed["on"] = False
    resumed = dry.run(["T1", "T2", "T3"], fetch=fetch, put_row=put_row, out_dir=out, census_names={}, resume=True)
    assert resumed["data"]["acceptance"] == "PASS" and resumed["status"] == {"reextracted": 3}
    assert resumed["retry"] == {**NO_RETRY, "retried_n": 1, "recovered_n": 1, "retried": ["T2"]}


def test_every_csv_starts_with_its_ticker_column(dry):
    assert all(header[0] == "ticker" for _, header in dry.CSV_FILES.values())       # what the final sort keys on


def test_a_finished_run_sorts_its_files_before_the_state_is_removed_or_saved(dry, monkeypatch, tmp_path):
    seen = []
    real_sort, real_save = dry._sort_csv, dry._save_state

    def sort(path):
        seen.append(("sort", (path.parent / "state.json").exists()))
        real_sort(path)

    def save(out_dir, mark, acc, scope):
        done = _tickers_in(out_dir, "added_values.csv")
        seen.append(("save", done == sorted(done)))
        real_save(out_dir, mark, acc, scope)

    monkeypatch.setattr(dry, "_sort_csv", sort)
    monkeypatch.setattr(dry, "_save_state", save)
    flaky = _first_call_fails(lambda: httpx.ReadTimeout("read timed out"), "AAA", facts=RESUME_FACTS)
    dry.run(sorted(RESUME_FACTS), fetch=flaky, put_row=None, out_dir=tmp_path / "pass", census_names={})
    assert [kind for kind, _ in seen].count("sort") == 8
    assert all(state_still_there for kind, state_still_there in seen if kind == "sort")     # removed only afterwards
    assert not (tmp_path / "pass" / "state.json").exists()
    seen.clear()

    def both(ticker):
        if ticker == "CCC":
            raise httpx.ReadTimeout("read timed out")
        return flaky(ticker)

    dry.run(sorted(RESUME_FACTS), fetch=both, put_row=None, out_dir=tmp_path / "incomplete", census_names={})
    assert [kind for kind, _ in seen][-9:] == ["sort"] * 8 + ["save"]       # the final state: after the eight sorts
    assert seen[-1] == ("save", True)                                       # ... over sorted files


def test_the_sorted_files_are_fsynced_before_they_replace_the_unsorted_ones(dry, monkeypatch, tmp_path):
    synced, replaced = [], []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(os, "fsync", lambda fd: synced.append(os.fstat(fd).st_ino) or real_fsync(fd))

    def replace(src, dst):
        if str(dst).endswith(".csv"):
            replaced.append((Path(dst).name, Path(src).stat().st_ino in synced))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    out = tmp_path / "out"
    dry.run(sorted(RESUME_FACTS), fetch=lambda t: RESUME_FACTS[t], put_row=None, out_dir=out, census_names={})
    assert sorted(name for name, _ in replaced) == sorted(f for f, _ in dry.CSV_FILES.values())
    assert all(was_synced for _, was_synced in replaced)
    assert not list(out.glob("*.tmp"))                                  # nothing left aside


def test_the_final_sort_keeps_the_rows_of_one_ticker_in_their_order_and_every_field_as_written(dry, tmp_path):
    path = tmp_path / "rows.csv"
    rows = [["B", "z", "1;2"], ["A", "y", 'say "hi", twice'], ["B", "a", ""], ["A", "b", "line\nbreak"], ["C", "m", "x"]]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["ticker", "field", "years"])
        writer.writerows(rows)
    dry._sort_csv(path)
    with path.open(encoding="utf-8", newline="") as f:
        got = list(csv.reader(f))
    assert got == [["ticker", "field", "years"], rows[1], rows[3], rows[0], rows[2], rows[4]]
    raw = path.read_bytes()
    assert raw.count(b"\r\n") == 6 and b'"line\nbreak"' in raw         # one terminator per row, the field kept as it was
