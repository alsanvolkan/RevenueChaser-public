"""#375 F2 (13F-B), since G9 on the ownership store: the 13F Fetch and Refresh render the board through one path.

* a Refresh (``run_sec_13f_latest`` alone) builds the same payload as the Fetch, from the store, with no
  enrichment read and no provider call;
* the ``/api/sec_13f/<t>`` chart takes its price line from D01, not from ``sec13f_enrichment``;
* the Fetch, the Refresh and the chart agree on the short interest (FINRA, through the ownership store).

Offline; temp stores.
"""
from __future__ import annotations

import gzip
import json
from datetime import date

import pytest

from RevenueChaser.app import runners
from RevenueChaser.core import rawstore
from RevenueChaser.core.reporting_common import payload_path
from RevenueChaser.module_sec_13f import enrich
from RevenueChaser.module_sec_13f.tests.ownership_store import _store, market  # noqa: F401 - the fixture


def _payload():
    return json.loads(gzip.decompress(payload_path(runners.SEC_13F_LATEST).read_bytes()))


def _cells(payload, ticker):
    row = next(r for r in payload["rows"] if r[0] == ticker)
    return dict(zip(payload["cols"], row[3]))


def _bar(ticker, d, close):
    return {"ticker": ticker, "session_date": date.fromisoformat(d), "open": close, "high": close, "low": close,
            "close": close, "adj_close": None, "volume": 1000.0, "split_ratio": None, "adj_source": None,
            "source": "uw", "fetched_at": 0.0, "run_id": "seed", "run_at": 0.0}


@pytest.fixture
def no_enrichment(monkeypatch):
    """``sec13f_enrichment`` is no longer read by the board, its Refresh or its chart route (F2)."""
    def boom():
        raise AssertionError("load_enrichment was read")
    monkeypatch.setattr(enrich, "load_enrichment", boom)


@pytest.fixture
def store(market, rawstore_tmp_db):  # noqa: F811
    _store(market)
    return market


def test_a_refresh_builds_the_same_payload_as_the_fetch(store, no_enrichment):
    rawstore.write_rows("price_bars_daily", [_bar("AAA", "2025-06-30", 220.0)])
    runners.build_sec13f(None)
    fetched = _payload()
    payload_path(runners.SEC_13F_LATEST).unlink()
    runners.SEC_13F_LATEST.unlink()
    runners.run_sec_13f_latest(None)
    assert _payload() == fetched
    assert _cells(fetched, "AAA")["short"][1] == 2_500_000


def test_the_chart_route_takes_its_price_from_d01(store, no_enrichment):
    from RevenueChaser.app import dashboard
    rawstore.write_rows("price_bars_daily", [_bar("AAA", "2025-03-28", 100.0), _bar("AAA", "2025-06-30", 120.0)])
    with dashboard.app.test_client() as c:
        fig = c.get("/api/sec_13f/AAA").get_json()["fig"]
    price = next(t for t in fig["data"] if t.get("name") == "Price")
    assert list(price["y"])[:2] == [100.0, 120.0]
