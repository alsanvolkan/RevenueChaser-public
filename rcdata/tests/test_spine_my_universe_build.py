"""``my_universe`` through a real ``spine/security`` build, and the ``adv_usd_20d`` it reads from Compustat.

The same sources as ``test_spine_etf``: the SEC equity file, Alpha Vantage's listing status and four N-CEN
quarters. Compustat's ``comp.secd`` partitions are written before the build, because that is where the 20-session
dollar volume comes from (an ETF with no AUM in N-CEN is decided on it, avo 4 Nov 2026; from the raw Parquet and
not from the live prices store since #357, avo's decision B of 5 Nov 2026).
"""
from __future__ import annotations

from datetime import date

import pytest

from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.rcdata import ops, serving
from RevenueChaser.rcdata.build import base
from RevenueChaser.rcdata.build.spine import av_listing, etf, my_universe, ncen, sec_tickers, security

from . import spine_fixture as fx
from .test_spine_etf import AV, SEC_FILE
from .test_spine_etf_adv import put_secd, series

#: One Compustat series for each fund of these tests, its 20 sessions ending on the build's own day.
GVKEYS = {"SPY": "109503", "GLD": "131855", "QYLD": "200001"}


def _secd(ticker: str, sessions: int, dollars: float) -> list[dict]:
    """``sessions`` priced Compustat days of ``ticker`` whose close times volume is ``dollars`` a day."""
    return series(GVKEYS[ticker], "01", ticker, sessions, dollars, last=date(2026, 11, 2))


@pytest.fixture
def built(monkeypatch, http_mock):
    """The spine built once, after ``secd`` (if the test asked for any) is in Compustat's Parquet."""
    def build(secd: list[dict] | None = None):
        if secd:
            put_secd(secd)
        monkeypatch.setattr(sec_edgar, "SEC_WWW_BASE", "https://sec.test")
        monkeypatch.setenv("RC_HTTP_CONTACT", "ops@example.org")
        monkeypatch.setenv("RC_ALPHAVANTAGE_BASE", "https://av.test")
        monkeypatch.setenv("ALPHAVANTAGE_API_KEY", "av-test-key")
        monkeypatch.setattr(av_listing, "MIN_ETFS", 1)
        http_mock.add("company_tickers_exchange.json", SEC_FILE)
        fx.add_ncen_routes(http_mock)
        http_mock.add("state=delisted", "{}")
        http_mock.add("av.test/query", AV)
        base.run_request({"dataset": ncen.NAME, "params": {"mode": "backfill", "to": "2026-11-02"}})
        base.run_request({"dataset": av_listing.NAME, "params": {"as_of": "2026-11-02"}})
        return base.run_request({"dataset": sec_tickers.NAME, "params": {"as_of": "2026-11-02"}})
    return build


def _mine() -> dict[str, dict]:
    with serving.open_read("spine") as con:
        cur = con.execute(f"SELECT * FROM {my_universe.TABLE} ORDER BY ticker")
        cols = [d[0] for d in cur.description]
        return {r[0]: dict(zip(cols, r)) for r in cur.fetchall()}


def _changes() -> list[dict]:
    with serving.open_read("spine") as con:
        cur = con.execute(f"SELECT * FROM {my_universe.CHANGES} ORDER BY ticker")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def test_the_build_serves_both_tables_with_the_plans_columns(built):
    built()
    with serving.open_read("spine") as con:
        mine = [d[0] for d in con.execute(f"SELECT * FROM {my_universe.TABLE} LIMIT 0").description]
        changes = [d[0] for d in con.execute(f"SELECT * FROM {my_universe.CHANGES} LIMIT 0").description]
    assert mine == ["ticker", "kind", "security_type", "name", "exchange", "tier", "rule", "reason", "aum",
                    "aum_as_of", "adv_usd_20d", "entered_on", "source", "source_as_of", "fetched_at", "run_id"]
    assert changes == ["ticker", "kind", "change", "reason", "as_of", "run_id"]


def test_the_rules_pick_the_three_large_etfs_and_no_stock_of_this_fixture(built):
    """This fixture has no WRDS and no OpenFIGI source, so its one Tier A name (MSFT) is typed ``unknown`` -- and
    an untyped name stays out of the list until it is typed. The three funds over the AUM floor are the list."""
    built()
    rows = _mine()
    assert sorted(rows) == ["SOXL", "SOXX", "XLK"]
    assert rows["XLK"]["kind"] == "etf" and rows["XLK"]["rule"] == "etf_aum"
    assert rows["XLK"]["reason"] == "AUM 80,000.0M USD" and rows["XLK"]["aum_as_of"] == date(2026, 4, 30)
    # CCOI and LEVX are under the 100M floor; TAPR, BNKU and FNGU are notes; SPY, GLD, QYLD and NEWE have no
    # AUM and, with no Compustat partition in this build, no dollar volume either.
    assert not {"MSFT", "CCOI", "LEVX", "TAPR", "BNKU", "FNGU", "SPY", "GLD", "QYLD", "NEWE"} & set(rows)


def test_a_fund_with_no_aum_enters_on_its_20_session_dollar_volume_and_a_thin_one_does_not(built):
    built(_secd("SPY", 20, 40_000_000_000.0) + _secd("GLD", 20, 1_000_000.0) + _secd("QYLD", 5, 90_000_000.0))
    rows = _mine()
    assert "SPY" in rows and rows["SPY"]["rule"] == "etf_adv"
    assert rows["SPY"]["adv_usd_20d"] == pytest.approx(40_000_000_000.0)
    assert "GLD" not in rows                                  # 1M a session, under the 10M floor
    assert "QYLD" not in rows                                 # only 5 sessions: not measured, not judged


def test_the_undecided_funds_are_build_issues_the_ops_file_keeps(built):
    built()
    kinds = {r["kind"]: r["count"] for r in ops.open_issue_kinds(security.NAME)}
    assert kinds.get("my_universe_etf_undecided") == 1        # one issue naming SPY, GLD, QYLD and NEWE
    detail = next(i for i in ops.open_issues(security.NAME, limit=50)[1]
                  if i["kind"] == "my_universe_etf_undecided")["detail"]
    assert detail.startswith("4 ETFs") and "SPY" in detail and "NEWE" in detail
    assert set(my_universe.ISSUES) <= set(security.ISSUE_KINDS)


def test_a_second_build_keeps_the_entered_day_and_appends_what_changed(built, monkeypatch):
    built()
    assert {(c["ticker"], c["change"]) for c in _changes()} == {
        ("XLK", "entered"), ("SOXX", "entered"), ("SOXL", "entered")}
    first = _mine()["XLK"]["entered_on"]
    monkeypatch.setattr(my_universe, "load_manual", lambda *a, **k: ([], [{"ticker": "SOXL", "reason": "avo: out"}]))
    built()
    rows, changes = _mine(), _changes()
    assert sorted(rows) == ["SOXX", "XLK"] and rows["XLK"]["entered_on"] == first
    assert [(c["ticker"], c["change"]) for c in changes if c["change"] == "left"] == [("SOXL", "left")]
    assert len(changes) == 4                                  # the three carried forward plus SOXL leaving


def test_the_served_schema_version_names_the_two_new_tables(built):
    built()
    assert serving.SERVING_SCHEMA_VERSION["spine"] == 6
    assert my_universe.TABLE in security.TABLES and my_universe.CHANGES in security.TABLES
    assert etf.ADV_SESSIONS == 20


def test_the_list_may_shrink_by_any_amount_but_still_may_not_empty(built, monkeypatch):
    """A shrink is what changing the rules looks like, so the guard lets it through; an empty list is a bug."""
    built()
    assert my_universe.TABLE in serving.SHRINKS_BY_CHOICE
    monkeypatch.setattr(my_universe, "load_rules", lambda *a, **k: my_universe.Rules(
        stock_tiers=("A",), stock_types=("common",), etf_aum_floor=1e15, etf_adv_floor=1e15,
        exclude_etn=True, exclude_cef=True))
    built()                                                   # the swap is refused: the run fails, the file stands
    assert sorted(_mine()) == ["SOXL", "SOXX", "XLK"]
