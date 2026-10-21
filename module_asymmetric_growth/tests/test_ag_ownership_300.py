"""#300 (D20): an AG Fetch (the universe job) stores the yfinance ownership fields of every name it fetched in
``ownership_snapshot``, from the same ``.info`` as the D12 valuation; a Refresh and a cache hit store nothing.
Synthetic stores, no network; the run clock is pinned after the close."""
from __future__ import annotations

import copy
from datetime import date

from RevenueChaser.core import rawstore
from RevenueChaser.core.providers import cache as cache_manager
from RevenueChaser.module_asymmetric_growth.tests.test_ag_info_only_150 import _edgar
from RevenueChaser.module_asymmetric_growth.tests.test_ag_vendor_297 import _DATED, _DatedYahoo, _run

_OWN = {"sharesOutstanding": 2.0e8, "heldPercentInsiders": 0.12, "heldPercentInstitutions": 0.65}


class _OwnedYahoo(_DatedYahoo):
    def _info(self, ticker):
        out = super()._info(ticker)
        out["info"].update(_OWN)
        return out

    def _full(self, ticker):
        out = super()._full(ticker)
        out["info"].update(_OWN)
        return out


def _owned():
    return rawstore.query("SELECT * FROM ownership_snapshot ORDER BY ticker")


def test_a_fetch_stores_ownership_for_every_fetched_name(rawstore_tmp_db, monkeypatch):
    rawstore.write_rows("edgar_financials", [_edgar("EDG")])
    _OwnedYahoo(monkeypatch)
    _run(monkeypatch, ["EDG", "NOE"])
    rows = _owned()
    assert [(r["ticker"], r["source"], r["session_date"], r["run_id"]) for r in rows] == \
        [("EDG", "yfinance", date(2026, 10, 8), "job-ag"), ("NOE", "yfinance", date(2026, 10, 8), "job-ag")]
    assert {(r["shares_outstanding"], r["insider_pct"], r["institutional_pct"]) for r in rows} == \
        {(2.0e8, 0.12, 0.65)}


def test_a_refresh_stores_no_ownership(rawstore_tmp_db, monkeypatch):
    rawstore.write_rows("edgar_financials", [_edgar("EDG")])
    yahoo = _OwnedYahoo(monkeypatch)
    _run(monkeypatch, ["EDG"], no_fetch=True)
    assert yahoo.info_calls == [] and _owned() == []


def test_a_cache_hit_stores_no_ownership(rawstore_tmp_db, monkeypatch):
    cache_manager.save("NOE", {"info": {"country": "United States", **_OWN}, "annual": copy.deepcopy(_DATED)})
    yahoo = _OwnedYahoo(monkeypatch)
    _run(monkeypatch, ["NOE"])
    assert yahoo.full_calls == [] and _owned() == []


def test_a_second_fetch_of_the_session_keeps_the_first_row(rawstore_tmp_db, monkeypatch):
    _OwnedYahoo(monkeypatch)
    _run(monkeypatch, ["NOE"])
    rawstore.execute("DELETE FROM ticker_payload_cache")        # force a second fetch
    _OWN["heldPercentInsiders"] = 0.99
    try:
        _run(monkeypatch, ["NOE"], run_id="job-ag-2")
    finally:
        _OWN["heldPercentInsiders"] = 0.12
    (row,) = _owned()
    assert (row["run_id"], row["insider_pct"]) == ("job-ag", 0.12)


def test_an_ownership_store_failure_does_not_fail_the_scan(rawstore_tmp_db, monkeypatch):
    from RevenueChaser.core import ownership
    _OwnedYahoo(monkeypatch)

    def boom(*a, **k):
        raise rawstore.RawStoreError("locked")

    monkeypatch.setattr(ownership, "record", boom)
    out = _run(monkeypatch, ["NOE"])
    assert [c["ticker"] for c in out["cards"]] == ["NOE"]
    assert len(rawstore.query("SELECT * FROM vendor_valuation")) == 1      # D12 unaffected
