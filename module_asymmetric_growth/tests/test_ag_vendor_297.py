"""#297 (D12): an AG Fetch stores the yfinance answers it fetched in vendor_financials / vendor_valuation.

A name with EDGAR years costs one ``.info`` call (#150), so it stores a valuation only; a name without them stores
its statements too. A Refresh (``no_fetch``) and a cache hit fetch nothing and store nothing. Synthetic stores, no
network; the run clock is pinned after the close."""
from __future__ import annotations

import copy
from datetime import datetime, timezone

from RevenueChaser.core import rawstore, vendor_data
from RevenueChaser.core.providers import cache as cache_manager
from RevenueChaser.module_asymmetric_growth import engine
from RevenueChaser.module_asymmetric_growth.tests.test_ag_info_only_150 import _QUOTE, _YF_ANNUAL, _Yahoo, _edgar
from RevenueChaser.module_asymmetric_growth.tests.test_ag_dated_momentum_380 import seed_momentum

COMPLETE = datetime(2026, 10, 8, 20, 35, tzinfo=timezone.utc).timestamp()   # 16:35 EDT: a complete session
# Period ends two years after the fixture's labels, so the newest (2025-12-31) is not stale on the pinned run day:
# since PR 6 a Yahoo record's period end is judged too (AG review SF-2).
_DATED = [dict(r, period_end=f"{r['fiscal_year'] + 2}-12-31") for r in _YF_ANNUAL]


class _DatedYahoo(_Yahoo):
    def _full(self, ticker):
        out = super()._full(ticker)
        out["annual"] = copy.deepcopy(_DATED)
        return out


def _run(monkeypatch, tickers, no_fetch=False, run_id="job-ag"):
    rows = [{"ticker": t, "quote": dict(_QUOTE), "market_cap": 5e9} for t in tickers]
    seed_momentum(tickers)                                    # the momentum signal comes from D01 (#380 S4)
    monkeypatch.setattr(engine, "fetch_universe_stats", lambda *a, **k: (rows, {}))
    import RevenueChaser.module_sec_13f.engine as s13
    monkeypatch.setattr(s13, "load_totals", lambda: {})
    monkeypatch.setattr(engine._spine, "excluded_tickers", lambda: set())
    monkeypatch.setattr(engine._spine, "spine_gics", lambda *a, **k: {})
    monkeypatch.setattr(vendor_data, "_clock", lambda: COMPLETE)
    monkeypatch.setattr(engine, "_run_day", lambda: datetime.fromtimestamp(COMPLETE, timezone.utc).date())
    return engine.run_asymmetric_growth_hunter(None, max_workers=2, no_fetch=no_fetch, run_id=run_id)


def _stored(table):
    return rawstore.query(f"SELECT * FROM {table} ORDER BY ticker")


def test_a_fetch_stores_valuation_for_every_fetched_name_and_statements_without_edgar(rawstore_tmp_db, monkeypatch):
    rawstore.write_rows("edgar_financials", [_edgar("EDG")])
    yahoo = _DatedYahoo(monkeypatch)
    _run(monkeypatch, ["EDG", "NOE"])
    assert sorted(yahoo.info_calls) == ["EDG"] and sorted(yahoo.full_calls) == ["NOE"]
    val = _stored("vendor_valuation")
    assert [(r["ticker"], r["source"], r["run_id"]) for r in val] == \
        [("EDG", "yfinance", "job-ag"), ("NOE", "yfinance", "job-ag")]
    fin = _stored("vendor_financials")
    assert {r["ticker"] for r in fin} == {"NOE"}                 # EDGAR names keep SEC first: no Yahoo statements
    assert len(fin) == 3 * len(_DATED) and {r["run_id"] for r in fin} == {"job-ag"}


def test_a_second_fetch_of_unchanged_statements_appends_nothing(rawstore_tmp_db, monkeypatch):
    _DatedYahoo(monkeypatch)
    _run(monkeypatch, ["NOE"])
    rawstore.execute("DELETE FROM ticker_payload_cache")        # force a second fetch
    _run(monkeypatch, ["NOE"], run_id="job-ag-2")
    assert len(_stored("vendor_financials")) == 3 * len(_DATED)


def test_a_refresh_stores_nothing(rawstore_tmp_db, monkeypatch):
    rawstore.write_rows("edgar_financials", [_edgar("EDG")])
    yahoo = _DatedYahoo(monkeypatch)
    _run(monkeypatch, ["EDG"], no_fetch=True)
    assert yahoo.info_calls == [] and yahoo.full_calls == []
    assert _stored("vendor_valuation") == [] and _stored("vendor_financials") == []


def test_a_cache_hit_stores_nothing(rawstore_tmp_db, monkeypatch):
    cache_manager.save("NOE", {"info": {"country": "United States"}, "annual": copy.deepcopy(_DATED)})
    yahoo = _DatedYahoo(monkeypatch)
    _run(monkeypatch, ["NOE"])
    assert yahoo.full_calls == []
    assert _stored("vendor_valuation") == [] and _stored("vendor_financials") == []


def test_a_vendor_store_failure_does_not_fail_the_scan(rawstore_tmp_db, monkeypatch):
    _DatedYahoo(monkeypatch)

    def boom(*a, **k):
        raise rawstore.RawStoreError("locked")

    monkeypatch.setattr(vendor_data, "record", boom)
    out = _run(monkeypatch, ["NOE"])
    assert [c["ticker"] for c in out["cards"]] == ["NOE"]
