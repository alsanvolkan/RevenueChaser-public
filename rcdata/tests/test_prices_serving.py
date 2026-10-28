"""``prices/serving``: the served tables of ``prices.duckdb`` from the sources' staging, through the builder, with
Alpaca mocked at the HTTP boundary and the share and float sources' Parquet written as their datasets write it.

The fixture week (27 Oct 2026 is a Tuesday): AAPL goes ex a 0.26 dividend on the 23rd, FLG does a 1-for-3 reverse
split on the 22nd (Flagstar's of July 2024, moved into the calendar's years), GAPX triples on the 22nd with no
action, SPY's Alpaca-adjusted day return on the 26th is 5 bp off its raw one (an action Alpaca knows and the store
does not), BRK.B has a spin-off the store does not apply."""
from __future__ import annotations

import math
from datetime import date, datetime, timezone

import pytest

from RevenueChaser.core import calendar
from RevenueChaser.rcdata import ops, serving
from RevenueChaser.rcdata.build import base
from RevenueChaser.rcdata.build.prices import actions, bars, cover_shares, floats, serving as prices_serving

from .prices_fixture import ALPHABET, SPINE, BarsAnswer, bar, write_spine

DAYS = [date(2026, 10, 20), date(2026, 10, 21), date(2026, 10, 22), date(2026, 10, 23), date(2026, 10, 26),
        date(2026, 10, 27)]
M = 1 - 0.26 / 200.0                                       # AAPL's dividend factor on the close before its ex-date
RAW = {
    "AAPL": [199.0, 199.5, 200.0, 199.9, 201.0, 202.0],
    "FLG": [12.0, 12.1, 36.6, 36.9, 37.2, 37.5],
    "GAPX": [10.0, 10.1, 30.6, 30.7, 30.8, 30.9],
    "BRK.B": [480.0, 481.0, 482.0, 483.0, 484.0, 485.0],
    "SPY": [580.0, 581.0, 582.0, 583.0, 584.0, 585.0],
    "GOOGL": [250.0, 251.0, 252.0, 253.0, 254.0, 255.0],
    "GOOG": [251.0, 252.0, 253.0, 254.0, 255.0, 256.0],
}
ALL = {**RAW,
       "AAPL": [c * M for c in RAW["AAPL"][:3]] + RAW["AAPL"][3:],
       "FLG": [c * 3 for c in RAW["FLG"][:2]] + RAW["FLG"][2:],
       "SPY": RAW["SPY"][:4] + [c * 1.0005 for c in RAW["SPY"][4:]]}
ACTIONS = {
    "cash_dividends": [{"id": "cd-1", "symbol": "AAPL", "rate": 0.26, "special": False, "foreign": False,
                        "process_date": "2026-10-24", "ex_date": "2026-10-23", "currency": "USD"}],
    "reverse_splits": [{"id": "rs-1", "symbol": "FLG", "old_rate": 3, "new_rate": 1, "process_date": "2026-10-22",
                        "ex_date": "2026-10-22"}],
    "spin_offs": [{"id": "so-1", "source_symbol": "BRK.B", "source_rate": 1, "new_symbol": "SPUN", "new_rate": 0.1,
                   "process_date": "2026-10-26", "ex_date": "2026-10-26"}],
    "forward_splits": [{"id": "fs-9", "symbol": "ZZZZ", "old_rate": 1, "new_rate": 2, "process_date": "2026-10-21",
                        "ex_date": "2026-10-21"}],
}
AAPL, FLG, GAPX, SPY = "001690:01", "figi:BBG000BWBX65", "cik:4000001:GAPX", "001000:01"


@pytest.fixture(autouse=True)
def env(monkeypatch, http_mock):
    monkeypatch.setenv("ALPACA_API_KEY_ID", "test-key-id")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "test-secret")
    monkeypatch.setenv("RC_ALPACA_BASE", "https://alpaca.test")
    monkeypatch.setattr(calendar, "_now", lambda: datetime(2026, 10, 27, 21, 0, tzinfo=timezone.utc))
    monkeypatch.setattr("RevenueChaser.rcdata.build.prices.ROTATION", ())
    monkeypatch.setattr("RevenueChaser.rcdata.build.prices.BENCHMARKS", ("SPY",))
    monkeypatch.setattr("RevenueChaser.rcdata.read.watched", lambda kind: set())
    write_spine(SPINE + ALPHABET)
    http_mock.add("/v2/stocks/bars", BarsAnswer({
        "raw": {s: [bar(d, c) for d, c in zip(DAYS, cs)] for s, cs in RAW.items()},
        "all": {s: [bar(d, c) for d, c in zip(DAYS, cs)] for s, cs in ALL.items()}}))
    http_mock.add("/v1/corporate-actions", {"corporate_actions": ACTIONS, "next_page_token": None})


def _shares_and_floats():
    from RevenueChaser.rcdata.build import market_series
    from RevenueChaser.rcdata.build.sources import cboe
    base.upsert_partition([{"series_id": "SPX", "obs_date": d, "source": "cboe", "value": v, "frequency": "daily",
                            "session_date": d, "source_as_of": date(2026, 10, 27), "fetched_at": 1.0, "run_id": "cboe"}
                           for d, v in ((date(2026, 10, 26), 6800.0), (date(2026, 10, 27), 6810.0))],
                          market_series.SCHEMA, market_series.KEY, market_series.partition_path(cboe, 2026))
    cover = [("320193", "0000320193-26-000090", "10-K", "", "AAPL", 14_800_000_000, "2026-10-17"),
             ("910073", "0000910073-26-000010", "10-Q", "", "FLG", 1_245_000_000, "2026-08-01"),
             ("1652044", "0001652044-26-000071", "10-Q", "us-gaap:CommonClassAMember", "GOOGL", 5_800_000_000,
              "2026-07-17"),
             ("1652044", "0001652044-26-000071", "10-Q", "us-gaap:CommonClassBMember", None, 860_000_000,
              "2026-07-17"),
             ("1652044", "0001652044-26-000071", "10-Q", "goog:CapitalClassCMember", "GOOG", 5_450_000_000,
              "2026-07-17")]
    base.upsert_partition([{"cik": c, "accession": a, "form": f, "filed": date(2026, 10, 1), "period_end": None,
                            "class_member": m, "symbol": s, "shares": n, "as_of": date.fromisoformat(d),
                            "source_as_of": date(2026, 10, 27), "fetched_at": 1.0, "run_id": "cover"}
                           for c, a, f, m, s, n, d in cover], cover_shares.SCHEMA, cover_shares.KEY,
                          cover_shares.partition_path(2026))
    yahoo = {"AAPL": (AAPL, 14_700_000_000), "BRK-B": ("072726:02", 1_300_000_000), "FLG": (FLG, 2_000_000_000)}
    base.upsert_partition([{"ticker": t, "security_id": sid, "float_shares": v, "source_as_of": date(2026, 10, 27),
                            "fetched_at": 1.0, "run_id": "yf"} for t, (sid, v) in yahoo.items()], floats.YAHOO_SCHEMA,
                          ["ticker"], base.parquet_root() / "yfinance" / "float" / "date=2026-10-27" / "part-0.parquet")
    uw = {"AAPL": (AAPL, 14_690_000_000), "BRK-B": ("072726:02", 2_000_000_000)}
    base.upsert_partition([{"ticker": t, "security_id": sid, "total_float": v, "source_as_of": date(2026, 10, 27),
                            "fetched_at": 1.0, "run_id": "uw"} for t, (sid, v) in uw.items()], floats.UW_SCHEMA,
                          ["ticker"], base.parquet_root() / "uw" / "total_float" / "date=2026-10-27" / "part-0.parquet")


@pytest.fixture
def built():
    _shares_and_floats()
    first = base.run_request({"dataset": actions.NAME, "params": {"as_of": "2026-10-27"}})
    assert first["result"] == "unchanged" and first["rows"] == 4, first      # no bars yet: no action resolves
    row = base.run_request({"dataset": bars.NAME, "params": {"as_of": "2026-10-27"}})
    assert row["result"] == "changed", row
    return row


def _q(sql: str, params=()):
    with serving.open_read("prices") as con:
        cur = con.execute(sql, list(params))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _issues() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for i in ops.open_issues(prices_serving.NAME, limit=500)[1]:
        out.setdefault(i["kind"], set()).add(i["key"])
    return out


def test_the_bars_are_served_nominal_keyed_on_the_security(built):
    assert built["coverage"] == 1.0 and built["source_as_of"] == "2026-10-27"
    rows = _q("SELECT * FROM price_bar_daily WHERE security_id = ? ORDER BY session_date", [AAPL])
    assert [r["session_date"] for r in rows] == DAYS[1:]
    assert [r["close"] for r in rows] == RAW["AAPL"][1:] and rows[0]["symbol"] == "AAPL"
    assert {r["source"] for r in rows} == {bars.NAME}
    assert _q("SELECT count(*) AS n FROM price_bar_daily WHERE source = 'alpaca/bars'")[0]["n"] == 7 * 5
    assert _q("SELECT count(*) AS n FROM price_bar_daily WHERE security_id = 'index:SPX'")[0]["n"] == 2
    version = _q("SELECT kind, version FROM serving_schema_version")[0]
    assert (version["kind"], version["version"]) == ("prices", 1)


def test_actions_typed_resolved_and_marked_applied(built):
    acts = {r["source_id"]: r for r in _q("SELECT * FROM corporate_action")}
    assert set(acts) == {"cd-1", "rs-1", "so-1"}                          # ZZZZ is no security of the store
    assert acts["rs-1"]["security_id"] == FLG and acts["rs-1"]["action"] == "split"
    assert acts["rs-1"]["ratio"] == pytest.approx(1 / 3) and acts["rs-1"]["applied"] is True
    assert acts["cd-1"]["action"] == "cash_dividend" and acts["cd-1"]["amount"] == 0.26 and acts["cd-1"]["applied"]
    assert acts["so-1"]["applied"] is False and "spin_off" in acts["so-1"]["not_applied_why"]


def test_adjustment_factors_and_the_one_basis_point_rule(built):
    seg = {(r["security_id"], r["valid_to"]): r for r in _q("SELECT * FROM adj_factor")}
    aapl = seg[(AAPL, date(2026, 10, 23))]
    assert aapl["tr_factor"] == pytest.approx(M, abs=1e-12) and aapl["split_factor"] == 1.0
    flg = seg[(FLG, date(2026, 10, 22))]
    assert flg["split_factor"] == pytest.approx(3.0) and flg["tr_factor"] == pytest.approx(3.0)
    tr = {r["session_date"]: r["close"] * r["f"] for r in _q(
        "SELECT b.session_date, b.close, coalesce(a.tr_factor, 1) AS f FROM price_bar_daily b "
        "LEFT JOIN adj_factor a ON a.security_id = b.security_id AND b.session_date >= coalesce(a.valid_from, "
        "DATE '1900-01-01') AND (a.valid_to IS NULL OR b.session_date < a.valid_to) WHERE b.security_id = ?", [AAPL])}
    ours = tr[date(2026, 10, 23)] / tr[date(2026, 10, 22)] - 1
    reinvested = (199.9 + 0.26) / 200.0 - 1                               # the CRSP-style total return of the day
    assert abs(ours - reinvested) < 1e-4
    alpaca = ALL["AAPL"][3] / ALL["AAPL"][2] - 1
    assert abs(ours - alpaca) < 1e-4
    assert AAPL + ":2026-10-23" not in _issues().get("adjustment_differs", set())
    assert _issues()["adjustment_differs"] == {SPY + ":2026-10-26"}


def test_a_gap_without_an_action_is_an_issue_and_the_reverse_split_is_not(built):
    gaps = _issues()["price_gap_no_action"]
    assert gaps == {GAPX + ":2026-10-22"}


def test_market_cap_from_the_cover_shares_split_adjusted_and_summed_per_issuer(built):
    cap = {(r["security_id"], r["session_date"]): r for r in _q("SELECT * FROM market_cap_daily")}
    assert cap[(AAPL, date(2026, 10, 27))]["market_cap"] == pytest.approx(202.0 * 14_800_000_000)
    assert cap[(FLG, date(2026, 10, 21))]["shares"] == pytest.approx(1_245_000_000)
    assert cap[(FLG, date(2026, 10, 22))]["shares"] == pytest.approx(415_000_000)
    assert cap[(FLG, date(2026, 10, 22))]["shares_source"] == cover_shares.NAME
    googl, goog = cap[("160329:01", date(2026, 10, 27))], cap[("160329:03", date(2026, 10, 27))]
    assert googl["market_cap"] == pytest.approx(255.0 * 5_800_000_000)
    assert googl["issuer_market_cap"] == pytest.approx(255.0 * 5_800_000_000 + 256.0 * 5_450_000_000)
    assert goog["issuer_market_cap"] == googl["issuer_market_cap"]
    assert (GAPX, date(2026, 10, 27)) not in cap                           # no shares known: no row, an issue
    assert {GAPX, "072726:02"} <= _issues()["tier_a_no_shares"]


def test_shares_and_float_per_class_with_their_checks(built):
    rows = _q("SELECT * FROM shares_class ORDER BY security_id, measure, source")
    b = [r for r in rows if r["class_member"] == "us-gaap:CommonClassBMember"]
    assert len(b) == 1 and b[0]["security_id"].startswith("class:1652044:") and b[0]["value"] == 860_000_000
    floats_ = {(r["security_id"], r["source"]): r["value"] for r in rows if r["measure"] == "float"}
    assert floats_[(AAPL, floats.YAHOO)] == 14_700_000_000 and floats_[(AAPL, floats.UW)] == 14_690_000_000
    issues = _issues()
    assert issues["float_sources_disagree"] == {"072726:02"}
    assert issues["float_above_outstanding"] == {FLG}
    assert issues["action_not_applied"] == {"so-1"}
    assert "tier_a_no_bar" not in issues


def test_the_bridge_views_keep_the_old_columns(built):
    old = _q("SELECT * FROM price_bars_daily WHERE ticker = 'FLG' ORDER BY session_date")
    assert list(old[0]) == ["ticker", "session_date", "open", "high", "low", "close", "adj_close", "volume",
                            "split_ratio", "adj_source", "source", "fetched_at", "run_id", "run_at"]
    assert old[0]["close"] == pytest.approx(12.1 * 3) and old[1]["close"] == 36.6      # split-adjusted to today
    assert old[0]["volume"] == pytest.approx(1_000_000 / 3)
    built_at = _q("SELECT built_at FROM serving_schema_version")[0]["built_at"]
    assert {r["fetched_at"] for r in old} == {built_at}                    # so the old reader applies no split again
    aapl = {r["session_date"]: r for r in _q("SELECT * FROM price_bars_daily WHERE ticker = 'AAPL'")}
    assert aapl[date(2026, 10, 22)]["adj_close"] == pytest.approx(200.0 * M)
    acts = {(r["ticker"], r["action"]): r for r in _q("SELECT * FROM price_actions")}
    assert acts[("FLG", "split")]["value"] == pytest.approx(1 / 3) and acts[("AAPL", "dividend")]["value"] == 0.26
    spx = _q("SELECT * FROM index_levels_daily ORDER BY session_date")
    assert list(spx[0]) == ["symbol", "session_date", "open", "high", "low", "close", "volume", "source",
                            "fetched_at", "run_id", "run_at"]
    assert [(r["symbol"], r["session_date"], r["close"]) for r in spx] == [
        ("^GSPC", date(2026, 10, 26), 6800.0), ("^GSPC", date(2026, 10, 27), 6810.0)]


def test_a_source_that_never_ran_is_one_issue(http_mock):
    base.run_request({"dataset": bars.NAME, "params": {"as_of": "2026-10-27"}})
    never = _issues()["source_never_run"]
    assert {actions.NAME, cover_shares.NAME, floats.YAHOO, floats.UW} <= never and bars.NAME not in never
    assert math.isclose(_q("SELECT count(*) AS n FROM corporate_action")[0]["n"], 0)
