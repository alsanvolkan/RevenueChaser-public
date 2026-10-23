"""#46, #64 checkbox 6: the currencies a filer tags its money facts in, reported on ``meta``.

Every money concept is read at one fixed unit (``"USD"``), so a filer that tags ``Revenues`` under ``EUR`` yields an
empty series and the board drops it as if it had no data at all. This reader does not read or convert those values —
converting currencies is a decision nobody made — it only says which currencies it saw, on the row's existing
``meta`` JSON, so the scan can count the names. Synthetic companyfacts dicts only: no real store, no network.
"""
from __future__ import annotations

import pytest

from RevenueChaser.core.providers import sec_edgar as se


def _fact(start, end, val, form="10-K", fp="FY", filed="2024-02-01"):
    return {"start": start, "end": end, "val": val, "form": form, "fp": fp, "filed": filed}


_ANNUAL = [_fact("2022-01-01", "2022-12-31", 100.0), _fact("2023-01-01", "2023-12-31", 123.0)]
_EUR_ANNUAL = [_fact("2022-01-01", "2022-12-31", 8_000.0), _fact("2023-01-01", "2023-12-31", 9_000.0)]
_QUARTER = [_fact("2023-01-01", "2023-03-31", 30.0, form="10-Q", fp="Q1")]


def _facts(**concepts) -> dict:
    """companyfacts with one ``us-gaap`` entry per concept: ``Revenues={"USD": [...], "EUR": [...]}``."""
    return {"entityName": "Test Co",
            "facts": {"us-gaap": {name: {"units": units or {}} for name, units in concepts.items()}}}


def extract(monkeypatch, facts: dict, ticker: str = "AAA"):
    """``get_fundamentals`` on ``facts``, with the CIK map and the download faked and every HTTP call fatal."""
    monkeypatch.setattr(se, "_cik_map", lambda: {ticker: {"cik": "1234", "name": "Test Co"}})
    monkeypatch.setattr(se, "_fetch_facts", lambda cik: facts)
    for name in ("get_json", "get_text", "get_bytes"):
        monkeypatch.setattr(se, name, lambda *a, **k: pytest.fail("a SEC request was made"))
    return se.get_fundamentals(ticker)


# ── the reader ───────────────────────────────────────────────────────────────

def test_a_filer_tagging_revenue_only_in_eur_is_reported_and_reads_no_value(monkeypatch, rawstore_tmp_db):
    """Both halves of test 1: the currency is recorded AND nothing is read, because reading an EUR figure as if it
    were USD is the worst outcome. A test that only checked the list would prove nothing."""
    annual, quarterly, meta = extract(monkeypatch, _facts(Revenues={"EUR": _EUR_ANNUAL}))

    assert meta["non_usd_units"] == ["EUR"]
    assert annual == [] and quarterly == []


def test_the_quarterly_extraction_reports_the_currency_too():
    """The twin path: a 10-Q-only filer's units are seen by ``_quarterly_facts`` as well as the annual one."""
    seen: set[str] = set()

    assert se.extract_quarterly(_facts(Revenues={"BRL": _QUARTER}), seen) == []
    assert seen == {"BRL"}


def test_usd_facts_are_used_and_the_filer_is_not_counted_as_non_usd(monkeypatch, rawstore_tmp_db):
    """Test 2: a filer that tags both reads its USD series, and the EUR figures are never mixed in."""
    annual, _quarterly, meta = extract(monkeypatch, _facts(Revenues={"USD": _ANNUAL, "EUR": _EUR_ANNUAL}))

    assert [r["revenue"] for r in annual] == [100.0, 123.0]
    assert meta["non_usd_units"] == []


def test_a_concept_with_no_unit_at_all_is_missing_not_non_usd(monkeypatch, rawstore_tmp_db):
    """Test 3: the two reasons stay distinct. A concept nobody tagged is not a currency; the key is present and
    empty, so "measured, none seen" is not "this row predates the measure"."""
    annual, _quarterly, meta = extract(monkeypatch, _facts(ProfitLoss={}))

    assert annual == []
    assert meta["non_usd_units"] == []
    assert "non_usd_units" in meta


def test_an_absent_concept_is_missing_not_non_usd(monkeypatch, rawstore_tmp_db):
    """The same distinction for a concept the filer never reports in any unit."""
    _annual, _quarterly, meta = extract(monkeypatch, _facts(Revenues={"USD": _ANNUAL}))

    assert meta["non_usd_units"] == []


def test_a_share_count_unit_is_not_a_currency(monkeypatch, rawstore_tmp_db):
    """Only the USD-wanted money concepts report: a share count read at ``shares`` is no currency at all."""
    _annual, _quarterly, meta = extract(monkeypatch, _facts(
        Revenues={"USD": _ANNUAL}, WeightedAverageNumberOfDilutedSharesOutstanding={"shares": _ANNUAL}))

    assert meta["non_usd_units"] == []


def test_every_currency_a_money_concept_was_tagged_in_is_listed_sorted(monkeypatch, rawstore_tmp_db):
    """A filer may spread its lines over several currencies; the row reports the set, sorted."""
    _annual, _quarterly, meta = extract(monkeypatch, _facts(
        Revenues={"EUR": _EUR_ANNUAL}, ProfitLoss={"CAD": _EUR_ANNUAL}))

    assert meta["non_usd_units"] == ["CAD", "EUR"]


def test_the_extraction_makes_no_sec_request(monkeypatch, rawstore_tmp_db):
    """Test 7: the currencies come from the companyfacts dict already in hand — zero SEC calls."""
    calls: list = []
    monkeypatch.setattr(se, "_cik_map", lambda: {"AAA": {"cik": "1234", "name": "Test Co"}})
    monkeypatch.setattr(se, "_fetch_facts", lambda cik: calls.append(cik) or
                        _facts(Revenues={"EUR": _EUR_ANNUAL}))
    monkeypatch.setattr(se, "get_json", lambda *a, **k: calls.append(a) or {})

    se.get_fundamentals("AAA")

    assert calls == ["1234"]                            # the download, and nothing else: no symbol lookup, no retry
