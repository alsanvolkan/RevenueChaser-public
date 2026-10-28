"""``ownership/serve``: the 13F totals per ticker and quarter over the real spine (the amendment rule, long equity
only, each CUSIP named through Compustat's dated table, then the spine's current CUSIP, then OpenFIGI behind the
reused-ticker guard), VALUE still reported in thousands after 2023 found from the 13F's own prices, the period
summary with the plan's two counts, the bridge views ``sec13f_totals`` and ``short_interest``, and a run of another
ownership dataset that carries the 13F tables over instead of deriving them again."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

from RevenueChaser.rcdata import ops, read, serving
from RevenueChaser.rcdata.build import base
from RevenueChaser.rcdata.build.ownership import finra, ibkr, sec, sec13f, serve
from RevenueChaser.rcdata.build.spine import openfigi_map

from . import ownership_fixture as ox
from . import spine_fixture as fx
from .test_spine_security import built, run_all  # noqa: F401 (the spine built from every one of its sources)

Q2, Q3 = date(2026, 6, 30), date(2026, 9, 30)
MSFT_OLD, OLD_VYLR, WFC_PC, NOBODY = "594918100", "92921Q100", "949746804", "999999999"
NOON_NY = datetime(2026, 10, 28, 16, 0, tzinfo=timezone.utc).timestamp()      # 12:00 New York, a session day


def _h(issuer: str, cusip: str, value: float, shares: float, **kw) -> dict:
    return ox.f13_holding(issuer, cusip, value, shares, **kw)


def _f(acc: str, cik: int, day: str, holdings: list[dict], period: str = "09-30-2026", **kw) -> tuple:
    form = kw.get("form", "13F-HR")
    return (ox.idx_row(cik, f"MANAGER {cik}", form, day, acc), ox.f13_filing(acc, cik, day, holdings, period, **kw))


def _edgar(http_mock) -> ox.Edgar:
    e = ox.Edgar()
    e.add_day("2001-11-14", [_f("0001000001-01-000001", 1000001, "2001-11-14",
                                [_h("MICROSOFT CORP", MSFT_OLD, 6, 100)], period="09-30-2001")])
    e.add_day("2026-08-14", [
        _f("0001000001-26-000002", 1000001, "2026-08-14", [_h("MICROSOFT CORP", "594918104", 400_000, 1000),
                                                           _h("EATON CORP PLC", "G29183103", 150_000, 500)],
           period="06-30-2026"),
        _f("0001000002-26-000003", 1000002, "2026-08-14", [_h("MICROSOFT CORP", "594918104", 120_000, 300),
                                                           _h("EATON CORP PLC", "G29183103", 30_000, 100)],
           period="06-30-2026")])
    e.add_day("2026-10-27", [
        _f("0001000001-26-000010", 1000001, "2026-10-27", [
            _h("MICROSOFT CORP", "594918104", 480_000, 1200), _h("EATON CORP PLC", "G29183103", 160_000, 500),
            _h("MICROSOFT CORP", "594918104", 90_000, 400, put_call="Put"),
            _h("MICROSOFT CORP", "594918104", 10_000, 10_000, sh_type="PRN"),
            _h("HEICO CORP", "422806109", 20_000, 100), _h("HEICO CORP CL A", "422806208", 16_000, 100)]),
        # B's restatement replaces its holdings report: the FDX line is gone, MSFT is 350.
        _f("0001000002-26-000011", 1000002, "2026-10-27", [_h("MICROSOFT CORP", "594918104", 120_000, 300),
                                                           _h("FEDEX CORP", "31428X106", 25_000, 100)]),
        _f("0001000002-26-000012", 1000002, "2026-10-27", [_h("MICROSOFT CORP", "594918104", 140_000, 350)],
           form="13F-HR/A", amendment_type="RESTATEMENT", amendment_no=1),
        # C's new-holdings amendment adds to its report.
        _f("0001000003-26-000013", 1000003, "2026-10-27", [_h("NVR INC", "62944T105", 250_000, 50)]),
        _f("0001000003-26-000014", 1000003, "2026-10-27", [_h("WELLS FARGO", "949746101", 10_000, 200)],
           form="13F-HR/A", amendment_type="NEW HOLDINGS", amendment_no=1),
        _f("0001000004-26-000015", 1000004, "2026-10-27", [
            _h("OLD VAYLOR CORP", OLD_VYLR, 1_000, 100), _h("NOBODY KNOWS INC", NOBODY, 500, 50),
            _h("WELLS FARGO PFD SER C", WFC_PC, 250, 10)])])
    e.install(http_mock)
    return e


def _thirteen_f(**params) -> dict:
    return base.run_request({"dataset": sec13f.NAME, "params": params}, sleep=lambda s: None)


def _serve(day: str = "2026-10-28") -> dict:
    return base.run_request({"dataset": serve.NAME, "requested_by": "cli", "params": {"as_of": day}})


@pytest.fixture
def store(built, monkeypatch):
    monkeypatch.setattr(sec, "_shared_pace", lambda: True)
    monkeypatch.setenv("RC_HTTP_CONTACT", "ops@example.org")
    # OpenFIGI still names the reused ticker for the old company's CUSIP; it names the preferred the spine holds
    # without a Compustat link.
    monkeypatch.setitem(fx.FIGI, OLD_VYLR, {"figi": "BBG000OLDVY1", "ticker": "VYLR", "exchCode": "US",
                                            "securityType": "Common Stock"})
    monkeypatch.setitem(fx.FIGI, WFC_PC, {"figi": "BBG000WFCPC1", "ticker": "WFC-PC", "exchCode": "US",
                                          "securityType": "Preferred"})
    _edgar(built)
    _thirteen_f(as_of="2026-10-28")
    _thirteen_f(mode="backfill", **{"from": "2026-08-14", "to": "2026-08-14"})
    _thirteen_f(mode="backfill", **{"from": "2001-11-14", "to": "2001-11-14"})
    built.requests.clear()
    run_all("2026-10-28", [openfigi_map.NAME])
    return _serve()


def _served(sql: str, *args) -> list[tuple]:
    with serving.open_read("ownership") as con:
        return con.execute(sql, list(args)).fetchall()


def _issues() -> dict[tuple[str, str], str]:
    return {(i["kind"], i["key"]): i["detail"] for i in ops.open_issues(serve.NAME, limit=500)[1]}


def test_the_openfigi_map_asks_the_newest_quarters_13f_cusips_no_other_source_holds(store, built):
    asked = {j["idValue"] for c in built.calls("figi.test/v3/mapping") for j in json.loads(c.content)}
    assert asked == {OLD_VYLR, NOBODY, WFC_PC}         # not MSFT's 2001 CUSIP: only the two newest periods


def test_the_amendment_rule_long_equity_only_and_share_classes_apart(store):
    assert store["result"] == "changed"
    assert _served("SELECT ticker, shares, value, holders, cusips FROM sec13f_ticker_period WHERE period = ? "
                   "ORDER BY ticker", Q3) == [
        ("ETN", 500.0, 160_000.0, 1, 1), ("HEI", 100.0, 20_000.0, 1, 1), ("HEI-A", 100.0, 16_000.0, 1, 1),
        ("MSFT", 1550.0, 620_000.0, 2, 1), ("NVR", 50.0, 250_000.0, 1, 1), ("WFC", 200.0, 10_000.0, 1, 1),
        ("WFC-PC", 10.0, 250.0, 1, 1)]


def test_the_metrics_compare_with_the_calendar_quarter_before(store):
    cols = ("top10_pct, hhi, new_holders, exited_holders, buy_flow, sell_flow, breadth, breadth_chg, prev_period, "
            "filers")
    rows = {(t, p): r for t, p, *r in _served(f"SELECT ticker, period, {cols} FROM sec13f_ticker_period")}
    assert rows[("MSFT", Q3)] == [100.0, 6503.6, 0, 0, 250.0, 0.0, 50.0, -50.0, Q2, 4]
    assert rows[("ETN", Q3)] == [100.0, 10000.0, 0, 1, 0.0, 100.0, 25.0, -75.0, Q2, 4]
    assert rows[("NVR", Q3)] == [100.0, 10000.0, 1, 0, 50.0, 0.0, 25.0, 25.0, Q2, 4]
    # The quarter before 30 Jun 2026 is not in the store: what it would compare with is unknown, never 0.
    assert rows[("MSFT", Q2)] == [100.0, 6449.7, None, None, None, None, 100.0, None, date(2026, 3, 31), 2]
    assert rows[("MSFT", date(2001, 9, 30))][6:] == [100.0, None, date(2001, 6, 30), 1]


def test_each_cusip_is_named_with_its_source_and_none_is_dropped(store):
    assert _served("SELECT cusip, period, ticker, map_source, candidate_ticker, value, holders "
                   "FROM sec13f_cusip_map ORDER BY period, cusip") == [
        (MSFT_OLD, date(2001, 9, 30), "MSFT", "wrds/sec_idhist", None, 6_000.0, 1),
        ("594918104", Q2, "MSFT", "spine/security", None, 520_000.0, 2),
        ("G29183103", Q2, "ETN", "spine/security", None, 180_000.0, 2),
        ("422806109", Q3, "HEI", "spine/security", None, 20_000.0, 1),
        ("422806208", Q3, "HEI-A", "wrds/sec_idhist", None, 16_000.0, 1),
        ("594918104", Q3, "MSFT", "spine/security", None, 620_000.0, 2),
        ("62944T105", Q3, "NVR", "spine/security", None, 250_000.0, 1),
        (OLD_VYLR, Q3, None, "refused", "VYLR", 1_000.0, 1),
        ("949746101", Q3, "WFC", "spine/security", None, 10_000.0, 1),
        (WFC_PC, Q3, "WFC-PC", "openfigi/map", None, 250.0, 1),
        (NOBODY, Q3, None, None, None, 500.0, 1),
        ("G29183103", Q3, "ETN", "spine/security", None, 160_000.0, 1)]


def test_an_old_companys_cusip_openfigi_gives_a_reused_ticker_is_refused_and_an_issue(store):
    """#236 in 13F: OpenFIGI names VYLR for the old company's CUSIP; Compustat gives that CUSIP to gvkey 123456,
    and VYLR is linked to 040001 today. The holding stays in the map unnamed, never VYLR's, and is one issue."""
    assert not _served("SELECT * FROM sec13f_ticker_period WHERE ticker = 'VYLR'")
    detail = _issues()[("cusip_owner_differs", OLD_VYLR)]
    assert "VYLR" in detail and "123456/01" in detail and "040001/01" in detail
    assert ("cusip_unmapped_large", NOBODY) in _issues()
    assert ("cusip_unmapped_large", OLD_VYLR) not in _issues()


def test_the_period_summary_and_the_plans_two_counts(store):
    assert _served("SELECT period, filers, filings_active, value_total, value_mapped, coverage, unmapped_cusips, "
                   "refused_value, filing_deadline, filed_through FROM sec13f_periods "
                   "ORDER BY period DESC LIMIT 2") == [
        (Q3, 4, 5, 1_077_750.0, 1_076_250.0, 0.9986, 2, 1_000.0, date(2026, 11, 16), date(2026, 10, 27)),
        (Q2, 2, 2, 700_000.0, 700_000.0, 1.0, 0, 0.0, date(2026, 8, 14), date(2026, 8, 14))]
    # 30 Sep is not complete before its deadline (16 Nov): the counts are the newest complete quarter's.
    assert store["coverage"] == 1.0
    assert "S&P 500 names with 13F holders in 2026-06-30: 2 of 5" in store["note"]
    assert "13F value mapped in 2026-06-30: 100.00%" in store["note"]


def test_the_totals_view_keeps_the_old_table_shape(store):
    (entry,) = _served("SELECT entry FROM sec13f_totals WHERE ticker = 'MSFT'")[0]
    entry = json.loads(entry)
    assert sorted(entry) == ["2001-09-30", "2026-06-30", "2026-09-30"]
    assert entry["2026-09-30"] == {"shares": 1550.0, "value": 620_000.0, "holders": 2, "new_holders": 0,
                                   "exited_holders": 0, "top10_pct": 100.0, "hhi": 6503.6, "buy_flow": 250.0,
                                   "sell_flow": 0.0, "breadth": 50.0, "breadth_chg": -50.0}


def test_the_filing_deadline_rolls_past_weekends_and_washingtons_birthday():
    assert serve.filing_deadline(date(2026, 9, 30)) == date(2026, 11, 16)       # 14 Nov is a Saturday
    assert serve.filing_deadline(date(2026, 6, 30)) == date(2026, 8, 14)
    assert serve.filing_deadline(date(2025, 12, 31)) == date(2026, 2, 17)       # Sat 14, Sun 15, Mon 16 a holiday
    assert serve.filing_deadline(date(2026, 3, 31)) == date(2026, 5, 15)


def test_another_datasets_run_carries_the_13f_tables_over(store, built, monkeypatch):
    before = _served("SELECT * FROM sec13f_ticker_period ORDER BY ticker, period")
    monkeypatch.setenv("RC_FINRA_API_BASE", "https://finra.test")
    ox.Finra({"2026-10-15": [ox.finra_row("MSFT", "2026-10-15", 50_000_000)]}).install(built)

    def never():
        raise AssertionError("a FINRA run derived the 13F tables again")
    monkeypatch.setattr(sec13f.DATASET, "holding_files", never)
    row = base.run_request({"dataset": finra.NAME, "params": {"as_of": "2026-10-28"}})
    assert row["result"] == "changed"
    assert _served("SELECT * FROM sec13f_ticker_period ORDER BY ticker, period") == before
    assert ("cusip_owner_differs", OLD_VYLR) in _issues()


# ── VALUE still in thousands after 2023 (private #208), from the 13F's own prices ────────────────────────────

PRICES = {"594918104": 400.0, "115637100": 60.0, "115637209": 58.0, "084670702": 450.0, "037833100": 230.0,
          "36467W109": 25.0}


def _book(scale: float = 1.0, names: int = 6) -> list[dict]:
    return [_h(f"ISSUER {c}", c, round(px * 100 * scale, 2), 100) for c, px in list(PRICES.items())[:names]]


def test_a_filer_still_reporting_thousands_is_rescaled_once_its_prices_agree(monkeypatch, http_mock):
    monkeypatch.setattr(sec, "_shared_pace", lambda: True)
    monkeypatch.setenv("RC_HTTP_CONTACT", "ops@example.org")
    ox.make_spine()
    e = ox.Edgar()
    filers = [_f(f"000200000{i}-26-00000{i}", 2000000 + i, "2026-10-27", _book()) for i in range(1, 5)]
    filers.append(_f("0002000005-26-000005", 2000005, "2026-10-27", _book(scale=0.001)))       # thousands
    filers.append(_f("0002000006-26-000006", 2000006, "2026-10-27", _book(scale=0.001, names=3)))  # too few names
    e.add_day("2026-10-27", filers)
    e.install(http_mock)
    _thirteen_f(as_of="2026-10-28")
    row = _serve()
    assert _served("SELECT cik, names, value_factor FROM sec13f_filer_period WHERE value_factor <> 1") == [
        ("2000005", 6, 1000)]
    assert _served("SELECT value FROM sec13f_ticker_period WHERE ticker = 'MSFT'") == [(40_000.0 * 5 + 40.0,)]
    assert "1 filer-quarters reported VALUE in thousands after 2023: rescaled" in row["note"]


# ── The short_interest bridge ────────────────────────────────────────────────────────────────────────────────

def test_short_interest_joins_the_newest_settled_short_interest_to_each_session(monkeypatch, http_mock):
    monkeypatch.setenv("RC_FINRA_API_BASE", "https://finra.test")
    day = "2026-10-15"
    ox.Finra({day: [ox.finra_row("MSFT", day, 50_000_000), ox.finra_row("BFA", day, 120_000),
                    ox.finra_row("BRK.B", day, 9_000_000, dtc=2.5)]}).install(http_mock)
    fake = ox.FakeFtp(ox.ibkr_file())
    fake.sessions = []
    monkeypatch.setattr(ibkr, "_ftp_factory", fake)
    monkeypatch.setattr(ibkr, "_now", lambda: NOON_NY)
    monkeypatch.setenv("RC_IBKR_SHORT_FTP_HOST", "ftp.ibkr.test")
    ox.make_spine()
    base.run_request({"dataset": finra.NAME, "params": {"as_of": "2026-10-28"}})
    base.run_request({"dataset": ibkr.NAME, "params": {"as_of": "2026-10-28"}}, sleep=lambda s: None)
    with serving.open_read("ownership") as con:
        cols = [r[0] for r in con.execute("DESCRIBE short_interest").fetchall()]
        rows = con.execute("SELECT ticker, session_date, market_date, short_interest, days_to_cover, fee_rate, "
                           "rebate_rate, short_shares_available FROM short_interest "
                           "ORDER BY ticker, session_date").fetchall()
    assert cols == ["ticker", "session_date", "market_date", "short_interest", "si_float", "total_float",
                    "days_to_cover", "fee_rate", "rebate_rate", "short_shares_available", "payload", "source",
                    "fetched_at", "run_id", "run_at"]
    oct15, oct28 = date(2026, 10, 15), date(2026, 10, 28)
    assert rows == [
        ("AAPL", oct28, None, None, None, 0.25, 3.3383, 10_000_000.0),
        ("BF-A", oct15, oct15, 120_000.0, 1.5, None, None, None),
        ("BRK-B", oct15, oct15, 9_000_000.0, 2.5, None, None, None),
        ("BRK-B", oct28, oct15, 9_000_000.0, 2.5, 0.25, 3.1, 3_500_000.0),
        ("GME", oct28, None, None, None, 16.0, -12.5, 150_000.0),
        ("MSFT", oct15, oct15, 50_000_000.0, 1.5, None, None, None)]
    finra_rows = read.short_interest(["brk-b", "MSFT"])
    assert [(r["ticker"], r["settlement_date"], r["current_short"]) for r in finra_rows] == [
        ("BRK-B", oct15, 9_000_000), ("MSFT", oct15, 50_000_000)]
    assert read.short_interest(["MSFT"], end=date(2026, 10, 14)) == []
    assert [(r["ticker"], r["available"]) for r in read.shortable(["GME", "AAPL"])] == [("AAPL", 10_000_000),
                                                                                        ("GME", 150_000)]
    assert {r["session_date"] for r in read.shortable()} == {oct28}
    assert read.shortable(session=date(2026, 10, 27)) == []


def test_no_spine_names_nothing_and_is_one_issue():
    base.parquet_root().mkdir(parents=True, exist_ok=True)
    row = _serve()
    assert row["result"] == "unchanged" and row["rows"] == 0
    assert ("spine_missing", "spine") in _issues()
    with serving.open_read("ownership") as con:
        assert con.execute("SELECT count(*) FROM sec13f_totals").fetchone() == (0,)


# ── The read API ─────────────────────────────────────────────────────────────────────────────────────────────

def test_the_read_api_gives_13f_holders_with_a_complete_flag(store, monkeypatch):
    monkeypatch.setattr(read.calendar, "today_ny", lambda: date(2026, 10, 28))
    rows = read.holders_13f(["msft"])
    assert [(r["period"], r["holders"], r["complete"]) for r in rows] == [
        (date(2001, 9, 30), 1, True), (Q2, 2, True), (Q3, 2, False)]        # 30 Sep is due 16 Nov
    assert {r["status"] for r in rows} == {ops.latest_check(serve.NAME)["result"]}
    assert {r["source"] for r in rows} == {serve.NAME}
    assert [r["ticker"] for r in read.holders_13f(periods=[Q3])] == ["ETN", "HEI", "HEI-A", "MSFT", "NVR", "WFC",
                                                                     "WFC-PC"]
    periods = read.periods_13f()
    assert [(p["period"], p["complete"], p["coverage"]) for p in periods] == [
        (date(2001, 9, 30), True, 1.0), (Q2, True, 1.0), (Q3, False, 0.9986)]


def test_nothing_built_yet_is_no_rows_from_any_ownership_reader():
    assert read.holders_13f() == [] and read.periods_13f() == [] and read.short_interest(["MSFT"]) == []
    assert read.shortable() == [] and read.threshold_list() == [] and read.fails_to_deliver(["MSFT"]) == []
    assert read.insider_transactions(["MSFT"]) == [] and read.beneficial_ownership(["MSFT"]) == []
