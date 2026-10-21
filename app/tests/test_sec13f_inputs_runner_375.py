"""#375 F2 (13F-B): the 13F Fetch and Refresh render the board from ``ownership_snapshot`` and D01, through one path.

* the UW shares step runs before the render, so a Fetch's board shows the shares outstanding it stored;
* a Refresh (``run_sec_13f_latest`` alone) builds the same payload from the store, with no enrichment read;
* the history row records the % Out the board showed;
* the ``/api/sec_13f/<t>`` chart takes its price line from D01, not from ``sec13f_enrichment``.

Offline; temp stores, a fake UW client.
"""
from __future__ import annotations

import gzip
import json
from datetime import date

import pytest

from RevenueChaser.app import runners
from RevenueChaser.app.tests.test_sec13f_shares_300 import COMPLETE, _UW, _at, uw  # noqa: F401 - fixtures
from RevenueChaser.app.tests.test_sec13f_job import store  # noqa: F401 - the store fixture
from RevenueChaser.core import rawstore
from RevenueChaser.core.reporting_common import payload_path
from RevenueChaser.module_sec_13f import enrich


def _payload():
    return json.loads(gzip.decompress(payload_path(runners.SEC_13F_LATEST).read_bytes()))


def _cells(payload, ticker):
    row = next(r for r in payload["rows"] if r[0] == ticker)
    return dict(zip(payload["cols"], row[3]))


def _bar(ticker, d, close):
    return {"ticker": ticker, "session_date": date.fromisoformat(d), "open": close, "high": close, "low": close,
            "close": close, "adj_close": None, "volume": 1000.0, "split_ratio": None, "adj_source": None,
            "source": "uw", "fetched_at": 0.0, "run_id": "seed", "run_at": 0.0}


def _short(ticker, session, si_float, source="uw_short_screener"):
    return {"ticker": ticker, "session_date": date.fromisoformat(session), "market_date": None,
            "short_interest": None, "si_float": si_float, "total_float": None, "days_to_cover": None,
            "fee_rate": None, "rebate_rate": None, "short_shares_available": None, "payload": {},
            "source": source, "fetched_at": 0.0, "run_id": "seed", "run_at": 0.0}


@pytest.fixture
def no_enrichment(monkeypatch):
    """``sec13f_enrichment`` is no longer read by the board, its Refresh or its chart route (F2)."""
    def boom():
        raise AssertionError("load_enrichment was read")
    monkeypatch.setattr(enrich, "load_enrichment", boom)


def test_a_fetch_board_shows_the_shares_its_own_shares_step_stored(uw, monkeypatch, no_enrichment):  # noqa: F811
    _at(monkeypatch, COMPLETE)
    runners.build_sec13f(None)
    assert uw.calls == ["AAPL"]
    pct_out = _cells(_payload(), "AAPL")["pct_out"]
    assert pct_out[1] == pytest.approx(300.0 / 1.5e10 * 100.0)          # 300 13F shares of 15B outstanding
    rows = rawstore.query("SELECT pct_out, shares_outstanding FROM sec13f_metrics_history", db="history")
    assert rows[0]["shares_outstanding"] == 1.5e10 and rows[0]["pct_out"] == pytest.approx(pct_out[1])


def test_a_refresh_builds_the_same_payload_as_the_fetch(uw, monkeypatch, no_enrichment):  # noqa: F811
    _at(monkeypatch, COMPLETE)
    rawstore.write_rows("price_bars_daily", [_bar("AAPL", "2026-03-31", 220.0)])
    runners.build_sec13f(None)
    fetched = _payload()
    payload_path(runners.SEC_13F_LATEST).unlink()
    runners.SEC_13F_LATEST.unlink()
    uw.calls, uw.built = [], 0
    runners.run_sec_13f_latest(None)
    assert _payload() == fetched and uw.calls == [] and uw.built == 0
    assert _cells(fetched, "AAPL")["pct_out"][1] is not None


def test_the_chart_route_takes_its_price_from_d01(store, no_enrichment):  # noqa: F811
    from RevenueChaser.app import dashboard
    from RevenueChaser.module_sec_13f import engine as sec_13f
    sec_13f.recompute_totals()                        # the ingested quarter: AAPL 2026-03-31
    rawstore.write_rows("price_bars_daily", [_bar("AAPL", "2026-03-31", 220.0)])
    dashboard.app.config["TESTING"] = True
    r = dashboard.app.test_client().get("/api/sec_13f/AAPL")
    assert r.status_code == 200, r.data
    (price,) = [t for t in r.get_json()["fig"]["data"] if t.get("name") == "Price"]
    assert list(price["y"]) == [220.0]


def test_fetch_refresh_and_chart_route_agree_on_short_pct(uw, monkeypatch, no_enrichment):  # noqa: F811
    _at(monkeypatch, COMPLETE)
    rawstore.write_rows("short_interest", [_short("AAPL", "2026-10-15", 0.048)])
    runners.build_sec13f(None)
    fetched = _cells(_payload(), "AAPL")["short"]
    assert fetched[:2] == ["4.8%", pytest.approx(4.8)]

    payload_path(runners.SEC_13F_LATEST).unlink()
    runners.SEC_13F_LATEST.unlink()
    uw.calls, uw.built = [], 0
    runners.run_sec_13f_latest(None)
    assert _cells(_payload(), "AAPL")["short"] == fetched

    from RevenueChaser.app import dashboard
    dashboard.app.config["TESTING"] = True
    r = dashboard.app.test_client().get("/api/sec_13f/AAPL")
    assert r.status_code == 200, r.data
    assert r.get_json()["ok"] is True


def test_refresh_reads_the_same_short_interest_path_with_no_network(uw, monkeypatch, no_enrichment):  # noqa: F811
    _at(monkeypatch, COMPLETE)
    rawstore.write_rows("short_interest", [_short("AAPL", "2026-10-15", 0.048)])
    runners.build_sec13f(None)
    payload_path(runners.SEC_13F_LATEST).unlink()
    runners.SEC_13F_LATEST.unlink()
    uw.calls, uw.built = [], 0

    seen = []
    real = enrich.load_short_interest

    def spy(tickers):
        seen.append("load_short_interest")
        return real(tickers)

    monkeypatch.setattr(enrich, "load_short_interest", spy)
    runners.run_sec_13f_latest(None)
    assert _cells(_payload(), "AAPL")["short"][:2] == ["4.8%", pytest.approx(4.8)]
    assert seen == ["load_short_interest"] and uw.calls == [] and uw.built == 0
