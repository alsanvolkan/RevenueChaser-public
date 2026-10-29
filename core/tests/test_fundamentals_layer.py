"""The fundamentals boards read their statements from the fundamentals layer (G6, build plan PR 13-15).

``core/fundamentals_layer`` rebuilds the old ``edgar_financials`` record shape from ``fundamental_fact``. These tests
build a real ``fundamentals.duckdb`` through the builder (``sec/companyfacts`` against the HTTP mock) and compare
what the boards would read with what the extraction gives for the same facts: the same values, period ends,
provenance and year-to-date figures, except the layer's own total debt.
"""
from __future__ import annotations

import pytest

from RevenueChaser.core import fundamentals_layer as layer
from RevenueChaser.core import rawstore
from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.rcdata.build.fundamentals import companyfacts
from RevenueChaser.rcdata.tests import fund_fixture as fx
from RevenueChaser.rcdata.tests.conftest import http_mock, market_roots  # noqa: F401  (fixtures)

CIK = "1000001"
TICKER = "GRW"


def _f(val, start, end, accn, filed, form, fp, fy):
    f = {"val": val, "end": end, "accn": accn, "filed": filed, "form": form, "fp": fp, "fy": fy}
    if start:
        f["start"] = start
    return f


# GRW: a fiscal year to 31 December. Two 10-Ks (FY2024, FY2025), the 10-Qs of 2025 and of 2026 to 30 June.
# Revenue is reported per quarter (3M facts); operating cash flow and capex only as years to date (6M, 9M), so
# their second and third quarters are derived, and every fourth quarter is the year minus nine months.
K24, K25 = "0000000001-25-000010", "0000000001-26-000010"
Q = {("2025", 1): "0000000001-25-000101", ("2025", 2): "0000000001-25-000102", ("2025", 3): "0000000001-25-000103",
     ("2026", 1): "0000000001-26-000101", ("2026", 2): "0000000001-26-000102"}
QEND = {1: "03-31", 2: "06-30", 3: "09-30"}
QFILED = {1: "05-01", 2: "08-01", 3: "11-01"}


def _flow(per_quarter: dict, ytd_only: bool) -> list[dict]:
    """A flow concept's facts: the two years, and per 10-Q its quarter (unless ``ytd_only``) and its year to date."""
    out = [_f(sum(per_quarter[("2024", q)] for q in range(1, 5)), "2024-01-01", "2024-12-31", K24, "2025-02-20",
              "10-K", "FY", 2024),
           _f(sum(per_quarter[("2025", q)] for q in range(1, 5)), "2025-01-01", "2025-12-31", K25, "2026-02-20",
              "10-K", "FY", 2025)]
    for (year, q), accn in Q.items():
        end, filed = f"{year}-{QEND[q]}", f"{year}-{QFILED[q]}"
        if not ytd_only:
            start = f"{year}-{['01-01', '04-01', '07-01'][q - 1]}"
            out.append(_f(per_quarter[(year, q)], start, end, accn, filed, "10-Q", f"Q{q}", int(year)))
        if q > 1 or ytd_only:
            out.append(_f(sum(per_quarter[(year, k)] for k in range(1, q + 1)), f"{year}-01-01", end, accn, filed,
                          "10-Q", f"Q{q}", int(year)))
    return out


def _instant(values: dict) -> list[dict]:
    out = [_f(values["2024-12-31"], None, "2024-12-31", K24, "2025-02-20", "10-K", "FY", 2024),
           _f(values["2025-12-31"], None, "2025-12-31", K25, "2026-02-20", "10-K", "FY", 2025)]
    for (year, q), accn in Q.items():
        end = f"{year}-{QEND[q]}"
        out.append(_f(values[end], None, end, accn, f"{year}-{QFILED[q]}", "10-Q", f"Q{q}", int(year)))
    return out


def grw_facts() -> dict:
    quarters = [("2024", q) for q in range(1, 5)] + [("2025", q) for q in range(1, 5)] + [("2026", 1), ("2026", 2)]
    revenue = {k: 100.0 + 10 * i for i, k in enumerate(quarters)}
    ocf = {k: 20.0 + 3 * i for i, k in enumerate(quarters)}
    capex = {k: 5.0 + i for i, k in enumerate(quarters)}
    ends = ["2024-12-31", "2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30"]
    gaap = {
        "Revenues": {"units": {"USD": _flow(revenue, ytd_only=False)}},
        "NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": _flow(ocf, ytd_only=True)}},
        "PaymentsToAcquirePropertyPlantAndEquipment": {"units": {"USD": _flow(capex, ytd_only=True)}},
        # A share count is a weighted average: it never adds up into a year to date.
        "WeightedAverageNumberOfDilutedSharesOutstanding": {"units": {"shares": _flow({k: 50.0 for k in quarters},
                                                                                     ytd_only=False)}},
        "Assets": {"units": {"USD": _instant({e: 1_000.0 + 10 * i for i, e in enumerate(ends)})}},
        "LongTermDebtNoncurrent": {"units": {"USD": _instant({e: 300.0 for e in ends})}},
        "DebtCurrent": {"units": {"USD": _instant({e: 20.0 for e in ends})}},
        "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": _instant({e: 50.0 for e in ends})}},
    }
    return {"cik": int(CIK), "entityName": "GROWTH CO", "facts": {"us-gaap": gaap}}


SPINE_ROW = {"ticker": TICKER, "cik": CIK, "name": "GROWTH CO", "exchange": "NYSE", "tier": "A",
             "gsector": "45", "ggroup": "4510", "gind": "451030", "gsubind": "45103020",
             "sector": "Information Technology", "industry": "Software & Services"}


@pytest.fixture
def built(monkeypatch, http_mock):  # noqa: F811
    monkeypatch.setattr(sec_edgar, "SEC_DATA_BASE", "https://data.sec.test")
    monkeypatch.setattr(sec_edgar, "SEC_WWW_BASE", "https://sec.test")
    monkeypatch.delenv(layer.SOURCE_ENV, raising=False)
    fx.spine([SPINE_ROW])
    fx.sec_routes(http_mock, {CIK: grw_facts()})
    fx.run(companyfacts.NAME, mode="backfill", to="2026-10-26")
    return http_mock


def _comparable(rec: dict) -> dict:
    """A record without what the layer changes on purpose (total debt) or does not serve (the per-period flags)."""
    return {k: v for k, v in rec.items() if k != "total_debt" and not k.endswith("_suspect")
            and k != "short_term_investments_basis"}


def test_the_layer_gives_the_extractions_records(built):
    annual, quarterly, meta = layer.records(TICKER)
    want_annual = sec_edgar.extract_annual(grw_facts(), set())
    want_quarterly = sec_edgar.extract_quarterly(grw_facts(), set())
    assert [r["fiscal_year"] for r in annual] == [r["fiscal_year"] for r in want_annual] == ["2024", "2025"]
    for got, want in zip(annual, want_annual):
        for field in sec_edgar.CONCEPTS:
            assert got.get(field) == want.get(field), (got["fiscal_year"], field)
        assert got["period_ends"] == want["period_ends"]
        assert got.get("period_starts") == want.get("period_starts")
        assert got.get("filed") == want.get("filed") and got.get("accn") == want.get("accn")
    assert [r["period"] for r in quarterly] == [r["period"] for r in want_quarterly]
    for got, want in zip(quarterly, want_quarterly):
        for field in sec_edgar.CONCEPTS:
            assert got.get(field) == want.get(field), (got["period"], field)
        assert sorted(got.get("derived") or []) == sorted(want.get("derived") or []), got["period"]
        assert got.get("period_starts") == want.get("period_starts"), got["period"]
        # The year to date the Rule of 40 reads: the same start, months and value for 3, 6 and 9 months.
        want_ytd = {f: y for f, y in (want.get("ytd") or {}).items() if y["months"] in (3, 6, 9)}
        assert got.get("ytd", {}) == want_ytd, got["period"]
    assert meta["available"] and meta["layer"] and meta["cik"] == CIK and meta["is_us"]
    assert meta["newest_filed"] == "2026-08-01" and meta["non_usd_units"] == []
    # The layer's total debt: the long-term line plus DebtCurrent (the old two-term sum read 300 alone).
    assert annual[-1]["total_debt"] == 320.0 and quarterly[-1]["total_debt"] == 320.0


def test_a_board_on_the_layer_never_downloads_facts(built):
    asked = len(built.requests)
    annual, meta = sec_edgar.get_annual(TICKER)                 # a Fetch's call (no_fetch False)
    assert annual and meta["layer"] and len(built.requests) == asked
    assert sec_edgar.get_fundamentals("NOPE") == ([], [], layer._unavailable())
    assert len(built.requests) == asked
    many = sec_edgar.stored_annual_many([TICKER, "NOPE"])
    assert set(many) == {TICKER} and many[TICKER][0] == annual


def test_without_a_layer_the_old_rows_are_read(monkeypatch):
    monkeypatch.delenv(layer.SOURCE_ENV, raising=False)
    rawstore.write_rows("edgar_financials", [{"ticker": TICKER, "annual": [{"fiscal_year": 2020, "revenue": 1.0}],
                                              "quarterly": [], "meta": {"source": "sec_edgar"}, "fetched_at": 1.0}])
    state = layer.source_state()
    assert state["effective"] == layer.STORE and state["reason"] == "no fundamentals.duckdb yet"
    assert sec_edgar.get_annual(TICKER, no_fetch=True)[0] == [{"fiscal_year": 2020, "revenue": 1.0}]


def test_a_failed_facts_check_keeps_the_last_good_build(built, monkeypatch, caplog):
    # Opus review S3: one failed ``sec/companyfacts`` run (an SEC 403, a timeout) leaves the served file the last
    # good build, so the boards keep reading it, never the old rows nobody refreshes; the failure is logged.
    from RevenueChaser.rcdata import ops
    monkeypatch.delenv(layer.SOURCE_ENV, raising=False)
    layer._FALLBACK_LOGGED.clear()
    ops.record_check(layer.FACTS_DATASET, "failed", note="HTTP 403")
    with caplog.at_level("WARNING"):
        state = layer.source_state()
    assert state["effective"] == layer.LAYER and state["reason"] is None
    assert "failed" in caplog.text and "last good build" in caplog.text
    assert sec_edgar.get_annual(TICKER, no_fetch=True)[1]["layer"]


def test_without_any_good_facts_check_the_old_rows_are_read(market_roots, monkeypatch):  # noqa: F811
    from RevenueChaser.rcdata import ops, serving
    monkeypatch.delenv(layer.SOURCE_ENV, raising=False)
    serving.live_path("fundamentals").parent.mkdir(parents=True, exist_ok=True)
    serving.live_path("fundamentals").touch()
    assert layer.source_state()["reason"] == f"{layer.FACTS_DATASET} has not built yet"
    ops.record_check(layer.FACTS_DATASET, "failed", note="HTTP 403")
    assert layer.source_state()["reason"] == f"{layer.FACTS_DATASET} has not built yet"


def test_the_escape_hatch_reads_the_old_rows(built, monkeypatch):
    monkeypatch.setenv(layer.SOURCE_ENV, "store")
    assert layer.source_state()["effective"] == layer.STORE
    assert sec_edgar.get_annual(TICKER, no_fetch=True) == ([], {"source": "sec_edgar", "entity_name": "",
                                                                "is_us": True, "available": False})


def test_a_batch_read_keeps_a_window_per_company(built):
    from datetime import date

    from RevenueChaser.rcdata import read
    rows = read.statements_many([CIK], annual_years=1, quarters=2)
    assert {r["fiscal_year"] for r in rows if r["period_type"] == "annual"} == {2025}
    assert {r["period_end"] for r in rows if r["period_type"] == "quarterly"} == {date(2026, 3, 31),
                                                                                  date(2026, 6, 30)}
