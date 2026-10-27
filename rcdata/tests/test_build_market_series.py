"""``market_series``: each source parses its fixture to the key shape; the whole dataset builds and serves."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from RevenueChaser.core import calendar
from RevenueChaser.rcdata import ops, read, serving
from RevenueChaser.rcdata.build import base, market_series as ms
from RevenueChaser.rcdata.build.sources import bls, cboe, cftc, fed, fred, treasury


def _keys(rows):
    return {(r["series_id"], r["obs_date"]) for r in rows}


def test_treasury_parses_every_tenor(fixture_text):
    rows = treasury.parse(fixture_text("treasury_2026.csv"), {})
    by = {(r["series_id"], r["obs_date"]): r["value"] for r in rows}
    assert by[("UST_PAR_10Y", date(2026, 10, 27))] == 4.02
    assert by[("UST_PAR_1.5M", date(2026, 10, 26))] == 4.02
    assert ("UST_PAR_1.5M", date(2026, 10, 23)) not in by          # an empty cell is no row
    assert {r["series_id"] for r in rows} >= {"UST_PAR_1M", "UST_PAR_6M", "UST_PAR_1Y", "UST_PAR_30Y"}
    assert treasury.tenor("1 Mo") == "1M" and treasury.tenor("30 Yr") == "30Y" and treasury.tenor("Date") is None


def test_cboe_parses_ohlc_and_single_column_files(fixture_text):
    vix = cboe.parse(fixture_text("VIX_History.csv"), {"symbol": "VIX"})
    last = [r for r in vix if r["obs_date"] == date(2026, 10, 27)][0]
    assert (last["open"], last["high"], last["low"], last["close"], last["value"]) == (17.9, 18.0, 16.9, 17.1, 17.1)
    assert min(r["obs_date"] for r in vix) == date(1990, 1, 2)
    vvix = cboe.parse(fixture_text("VVIX_History.csv"), {"symbol": "VVIX"})
    assert [(r["obs_date"], r["value"], r["close"]) for r in vvix][-1] == (date(2026, 10, 27), 94.8, None)


def test_fred_skips_the_missing_dot(fixture_text):
    rows = fred.parse(fixture_text("fred_DGS10.json"), {"series": "DGS10"})
    assert [(r["obs_date"], r["value"]) for r in rows] == [
        (date(1962, 1, 2), 4.06), (date(2026, 10, 23), 4.04), (date(2026, 10, 26), 4.03)]


def test_cftc_rows_are_keyed_on_the_contract_code_across_the_2022_rename(fixture_text):
    import json
    doc = json.dumps({"tff": json.loads(fixture_text("cftc_tff.json")),
                      "disagg": json.loads(fixture_text("cftc_disagg.json"))})
    rows = cftc.parse(doc, {})
    series = "cftc:tff:13874A:asset_mgr_positions_long"
    assert [(r["obs_date"], r["value"]) for r in rows if r["series_id"] == series] == [
        (date(2022, 2, 1), 1200000.0), (date(2022, 2, 8), 1201000.0), (date(2026, 10, 20), 1300000.0)]
    ids = {r["series_id"] for r in rows}
    assert "cftc:disagg:067651:swap__positions_short_all" in ids and "cftc:tff:13874A:open_interest_all" in ids
    assert not any("pct_" in i or "change_" in i or "market_and_exchange" in i for i in ids)


def test_fed_parses_meetings_across_months_and_skips_notation_votes(fixture_text):
    m = fed.meetings(fixture_text("fomc.htm"))
    assert [d[0] for d in m] == [date(2026, 1, 27), date(2026, 3, 17), date(2026, 4, 28), date(2026, 10, 31),
                                 date(2027, 1, 26)]
    assert m[2] == [date(2026, 4, 28), date(2026, 4, 29)]
    assert m[3] == [date(2026, 10, 31), date(2026, 11, 1)]
    rows = fed.parse(fixture_text("fomc.htm"), {})
    decisions = sorted(r["obs_date"] for r in rows if r["series_id"] == "FOMC_DECISION")
    assert decisions[0] == date(2026, 1, 28) and len(decisions) == 5
    assert all(r["value"] == 1.0 for r in rows)


def test_bls_parses_cpi_release_days_from_folded_lines(fixture_text):
    rows = bls.parse(fixture_text("bls.ics"), {})
    assert [(r["series_id"], r["obs_date"]) for r in rows] == [
        ("CPI_RELEASE", date(2026, 11, 10)), ("CPI_RELEASE", date(2026, 12, 10))]


# ── The whole dataset, every source mocked at the HTTP boundary ──

@pytest.fixture
def sources(http_mock, fixture_text, monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "fred-test-key")
    monkeypatch.setattr(treasury, "BASE", "https://treasury.test/csv")
    monkeypatch.setattr(cboe, "BASE", "https://cboe.test/daily")
    monkeypatch.setattr(fred, "BASE", "https://fred.test/obs")
    monkeypatch.setattr(cftc, "TFF_BASE", "https://cftc.test/resource/udgc-27he.json")
    monkeypatch.setattr(cftc, "DISAGG_BASE", "https://cftc.test/resource/72hh-3qpy.json")
    monkeypatch.setattr(fed, "BASE", "https://fed.test")
    monkeypatch.setattr(bls, "BASE", "https://bls.test")
    monkeypatch.setattr(calendar, "_now", lambda: datetime(2026, 10, 27, 22, 0, tzinfo=timezone.utc))
    http_mock.add("treasury.test", fixture_text("treasury_2026.csv"))
    http_mock.add("cboe.test/daily/VVIX", fixture_text("VVIX_History.csv"))
    http_mock.add("cboe.test", fixture_text("VIX_History.csv"))
    http_mock.add("fred.test", fixture_text("fred_DGS10.json"))
    http_mock.add("udgc-27he", fixture_text("cftc_tff.json"))
    http_mock.add("72hh-3qpy", fixture_text("cftc_disagg.json"))
    http_mock.add("fed.test", fixture_text("fomc.htm"))
    http_mock.add("bls.test", fixture_text("bls.ics"))
    return http_mock


def _run():
    return base.run_request({"dataset": ms.NAME, "params": {"as_of": "2026-10-27"}}, sleep=lambda s: None)


def test_a_daily_run_builds_every_source_and_a_second_run_makes_no_call(sources):
    row = _run()
    assert row["result"] == "changed", row
    first_calls = len(sources.requests)
    # 1 Treasury year + 8 Cboe + 13 FRED + 2 CFTC reports + 1 FOMC + 1 BLS
    assert first_calls == row["calls"] == 1 + len(cboe.SYMBOLS) + len(fred.SERIES) + 2 + 1 + 1
    assert row["coverage"] == 1.0 and row["added"] == row["rows"] and row["removed"] == 0
    second = _run()
    assert len(sources.requests) == first_calls, "a second run the same day must not call any source"
    assert second["result"] == "unchanged" and second["calls"] == 0 and second["rows"] == row["rows"]


def test_the_served_rows_carry_session_dates_from_the_calendar(sources):
    _run()
    rows = {(r["series_id"], r["obs_date"]): r for r in read.market_series(["VIX", "DGS10", "FOMC_DECISION"])}
    assert rows[("VIX", date(2026, 10, 27))]["session_date"] == date(2026, 10, 27)
    assert rows[("VIX", date(1990, 1, 2))]["session_date"] is None            # before the calendar's table
    assert rows[("FOMC_DECISION", date(2026, 11, 1))]["session_date"] == date(2026, 10, 30)  # a Sunday
    r = rows[("DGS10", date(2026, 10, 26))]
    assert r["source"] == "fred" and r["source_as_of"] == date(2026, 10, 27) and r["status"] == "changed"
    assert r["frequency"] == "daily" and r["run_id"]


def test_the_fred_key_is_sent_but_never_stored_in_the_raw_metadata(sources):
    _run()
    fred_calls = sources.calls("fred.test")
    assert len(fred_calls) == len(fred.SERIES) and all(b"api_key=fred-test-key" in c.url.query for c in fred_calls)
    for meta in base.raw_root().rglob("*.meta.json"):
        assert "fred-test-key" not in meta.read_text()


def test_fred_without_a_key_is_an_issue_and_the_rest_still_build(sources, monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "")
    row = _run()
    assert row["result"] == "changed" and sources.calls("fred.test") == []
    with ops.connect() as con:
        kinds = [r[0] for r in con.execute("SELECT kind FROM build_issues WHERE key = 'fred'")]
    assert kinds == ["missing_key"]


def test_one_source_down_is_an_issue_and_its_old_rows_stay_served(sources, http_mock, fixture_text):
    _run()
    before = len(read.market_series(["VIX"]))
    http_mock.routes.insert(0, ("cboe.test", b"down", 503, {}))
    row = base.run_request({"dataset": ms.NAME, "params": {"as_of": "2026-10-28"}}, sleep=lambda s: None)
    assert row["result"] in ("changed", "unchanged") and "cboe" in (row["note"] or "")
    assert len(read.market_series(["VIX"])) == before


def test_a_new_as_of_with_the_same_values_is_unchanged(sources):
    first = _run()
    later = base.run_request({"dataset": ms.NAME, "params": {"as_of": "2026-10-28"}}, sleep=lambda s: None)
    # Every source fetches again for a new as-of date, except CFTC: it already holds the newest report.
    assert later["calls"] == first["calls"] - 2
    assert later["result"] == "unchanged" and later["added"] == later["removed"] == 0


def test_a_run_that_raises_writes_a_failed_check_and_leaves_the_live_file(sources, monkeypatch):
    _run()
    live = serving.live_path("market").read_bytes()
    monkeypatch.setattr(ms.MarketSeries, "check", lambda self, ctx, con: 1 / 0)
    row = base.run_request({"dataset": ms.NAME, "params": {"as_of": "2026-10-27"}}, sleep=lambda s: None)
    assert row["result"] == "failed" and "ZeroDivisionError" in row["note"]
    assert serving.live_path("market").read_bytes() == live


def test_both_datasets_share_the_market_file(sources, http_mock):
    from RevenueChaser.rcdata.build import uw_spot_expiry_strike as uw

    from . import uw_fixture
    _run()
    http_mock.add("expiry-strike", uw_fixture.body())
    base.run_request({"dataset": uw.NAME, "params": {"as_of": "2026-10-26"}})
    with serving.open_read("market") as con:
        counts = dict(con.execute("SELECT table_name, estimated_size FROM duckdb_tables()").fetchall())
    assert set(counts) >= {"market_series", "spx_gamma_expiry_strike", "serving_schema_version"}
    assert read.market_series(["VIX"]) and len(read.spx_expiry_strike("2026-10-26")) == 391


def test_b2_an_empty_raw_root_never_swaps_in_an_empty_spx_table(sources, http_mock, monkeypatch, tmp_path):
    """The reviewer's case: the HDD is not mounted (or RC_MARKET_RAW_DIR unset), so a market_series run builds the
    market kind from an empty Parquet tree and would swap in spx_gamma_expiry_strike with 0 rows."""
    from RevenueChaser.core import config
    from RevenueChaser.rcdata.build import uw_spot_expiry_strike as uw

    from . import uw_fixture
    _run()
    http_mock.add("expiry-strike", uw_fixture.body())
    base.run_request({"dataset": uw.NAME, "params": {"as_of": "2026-10-26"}})
    live = serving.live_path("market").read_bytes()
    monkeypatch.setattr(config, "MARKET_RAW_DIR", tmp_path / "unmounted")
    row = base.run_request({"dataset": ms.NAME, "params": {"as_of": "2026-10-28"}}, sleep=lambda s: None)
    assert row["result"] == "failed" and "spx_gamma_expiry_strike" in row["note"]
    assert serving.live_path("market").read_bytes() == live
    assert len(read.spx_expiry_strike("2026-10-26")) == 391


def test_b2_a_build_refuses_a_missing_parquet_root(monkeypatch, tmp_path):
    from RevenueChaser.core import config
    monkeypatch.setattr(config, "MARKET_RAW_DIR", tmp_path / "nowhere")
    ctx = base.RunContext(run_id="r", dataset=ms.NAME)
    with pytest.raises(base.SourceError, match="Parquet root"):
        base.build_kind(ctx, ms.DATASET)
    assert not serving.live_path("market").exists()


def test_the_cftc_sidecar_names_both_report_urls(sources):
    _run()
    metas = [base.read_raw_json(p) for p in (base.raw_root() / "cftc").rglob("*.meta.json")]
    assert metas and all(m["urls"] == [cftc.TFF_BASE, cftc.DISAGG_BASE] for m in metas)


def test_a_date_past_the_calendar_table_has_no_session_date_and_one_issue(tmp_path):
    from RevenueChaser.core.calendar import MARKET_HOLIDAYS
    last = max(MARKET_HOLIDAYS)
    html = (f'<h4>{last + 1} FOMC Meetings</h4><div class="fomc-meeting__month"><strong>January</strong></div>'
            '<div class="fomc-meeting__date">26-27</div>')
    ctx = base.RunContext(run_id="r", dataset=ms.NAME)
    raw = base.raw_root() / "fed" / "fomc" / "2026-10-27.htm"
    ms.save_raw(ctx, raw, html.encode(), date(2026, 10, 27), "https://fed.test")
    ms.DATASET.to_parquet_all(ctx, [raw])
    import duckdb
    part = ms.partition_path(fed, last + 1)
    got = duckdb.connect().execute(f"SELECT DISTINCT session_date FROM read_parquet({base.sql_path(part)})").fetchall()
    assert got == [(None,)]
    with ops.connect() as con:
        issues = con.execute("SELECT kind, key FROM build_issues").fetchall()
    assert [tuple(i) for i in issues] == [("outside_calendar", "fed")]


def test_g1_the_tff_query_asks_for_futures_only_and_the_disaggregated_one_needs_no_filter(sources):
    _run()
    tff = [r for r in sources.calls("udgc-27he")]
    disagg = [r for r in sources.calls("72hh-3qpy")]
    from urllib.parse import parse_qs
    assert tff and all("futonly_or_combined='FutOnly'" in parse_qs(r.url.query.decode())["$where"][0] for r in tff)
    assert disagg and all("futonly_or_combined" not in parse_qs(r.url.query.decode())["$where"][0] for r in disagg)


def test_g1_a_combined_row_beside_the_futures_only_one_yields_one_series_row():
    """The live TFF answer for 13874A on 2026-10-20 held a FutOnly and a Combined row (open interest 1935470 and
    2814190); both gave the same series id, and the build failed on the key."""
    import json
    row = {"report_date_as_yyyy_mm_dd": "2026-10-20T00:00:00.000", "cftc_contract_market_code": "13874A"}
    doc = json.dumps({"tff": [{**row, "futonly_or_combined": "FutOnly", "open_interest_all": "1935470"},
                              {**row, "futonly_or_combined": "Combined", "open_interest_all": "2814190"}],
                      "disagg": []})
    rows = [r for r in cftc.parse(doc, {}) if r["series_id"] == "cftc:tff:13874A:open_interest_all"]
    assert [r["value"] for r in rows] == [1935470.0]
