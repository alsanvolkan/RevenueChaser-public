"""SEC exchange and fund files (#301, D21 + D8): change history, floors, one request per file per weekday night."""
from __future__ import annotations

from datetime import date, datetime, timezone

import httpx
import pytest

from RevenueChaser.core import rawstore, sec_files
from RevenueChaser.core.identity.tests.helpers import Router
from RevenueChaser.core.providers import sec_edgar


def _at(iso: str) -> float:
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp()


TUE = _at("2026-10-13T20:35:00")    # 22:35 Amsterdam, 16:35 New York: RC-Daily-Bars on a Tuesday
WED = _at("2026-10-14T20:35:00")
THU = _at("2026-10-15T20:35:00")
FRI = _at("2026-10-16T20:35:00")
SAT = _at("2026-10-17T20:35:00")

_EX_FIELDS = ["cik", "name", "ticker", "exchange"]
_MF_FIELDS = ["cik", "seriesId", "classId", "symbol"]
EXCHANGE = [[320193, "Apple Inc.", "AAPL", "Nasdaq"], [1067983, "Berkshire Hathaway", "BRK.B", "NYSE"],
            [1000, "Otc Co", "OTCX", "OTC"], [2000, "Small Co", "SMOL", "Nasdaq"]]
FUNDS = [[2110, "S000009184", "C000024954", "LACAX"], [2110, "S000009184", "C000024956", ""],
         [3000, "S000000001", "C000000001", "ABCDX"]]


@pytest.fixture(autouse=True)
def _sec(monkeypatch):
    monkeypatch.setattr(sec_edgar.time, "sleep", lambda s: None)
    monkeypatch.setattr(sec_edgar, "_throttle", lambda: None)
    sec_edgar.reset_breaker()
    monkeypatch.setattr(sec_files, "MIN_MEMBERS", 2)
    monkeypatch.setattr(sec_files, "MIN_FUND_CLASSES", 2)
    yield
    sec_edgar.reset_breaker()


@pytest.fixture
def http(monkeypatch):
    router = Router()
    monkeypatch.setattr(httpx, "get", lambda url, *a, **kw: router._dispatch("GET", url, kw))
    return router


def _serve(http, exchange=EXCHANGE, funds=FUNDS, ex_status=200, mf_status=200):
    http.routes.clear()
    http.add("company_tickers_exchange.json", {"fields": _EX_FIELDS, "data": exchange}, status=ex_status)
    http.add("company_tickers_mf.json", {"fields": _MF_FIELDS, "data": funds}, status=mf_status)


def _night(tmp_path, run_at, run_id="r"):
    return sec_files.nightly(run_id=run_id, run_at=run_at, marker_dir=tmp_path / "markers", log=lambda m: None)


def _rows(table, key):
    return rawstore.query(f"SELECT * FROM {table} ORDER BY {key}, run_at")


def test_a_first_night_stores_both_files_present_with_two_requests(rawstore_tmp_db, tmp_path, http):
    _serve(http)
    res = _night(tmp_path, TUE, run_id="run-tue")
    assert res["status"] == "ok" and res["requests"] == 2 and len(http.calls) == 2
    assert res["exchange"] == {"status": "ok", "file_rows": 4, "members": 3, "appended": 4, "absent": 0,
                               "unchanged": 0}
    ex = {r["ticker"]: r for r in _rows("sec_exchange_file", "ticker")}
    assert set(ex) == {"AAPL", "BRK-B", "OTCX", "SMOL"}               # the whole file, OTC included
    a = ex["AAPL"]
    assert (a["cik"], a["name"], a["exchange"], a["status"]) == ("320193", "Apple Inc.", "Nasdaq", "present")
    assert (a["valid_from"], a["source"], a["run_id"], a["run_at"]) == (date(2026, 10, 13), "sec_exchange",
                                                                       "run-tue", TUE)
    mf = {r["class_id"]: r for r in _rows("sec_mf_file", "class_id")}
    assert mf["C000024956"]["symbol"] is None and mf["C000024954"]["series_id"] == "S000009184"
    assert {r["status"] for r in mf.values()} == {"present"} and mf["C000000001"]["source"] == "sec_mf"
    assert res["mf"]["appended"] == 3


def test_an_unchanged_file_appends_nothing(rawstore_tmp_db, tmp_path, http):
    _serve(http)
    _night(tmp_path, TUE)
    res = _night(tmp_path, WED)
    assert res["exchange"]["appended"] == 0 and res["exchange"]["unchanged"] == 4
    assert res["mf"]["appended"] == 0 and res["mf"]["unchanged"] == 3
    assert len(_rows("sec_exchange_file", "ticker")) == 4 and len(_rows("sec_mf_file", "class_id")) == 3


def test_a_changed_exchange_appends_one_row(rawstore_tmp_db, tmp_path, http):
    _serve(http)
    _night(tmp_path, TUE)
    moved = [r if r[2] != "SMOL" else [2000, "Small Co", "SMOL", "NYSE"] for r in EXCHANGE]
    _serve(http, exchange=moved)
    res = _night(tmp_path, WED, run_id="run-wed")
    assert res["exchange"]["appended"] == 1
    smol = [r for r in _rows("sec_exchange_file", "ticker") if r["ticker"] == "SMOL"]
    assert [(r["exchange"], r["status"], r["valid_from"], r["run_id"]) for r in smol] == [
        ("Nasdaq", "present", date(2026, 10, 13), "r"), ("NYSE", "present", date(2026, 10, 14), "run-wed")]


def test_a_ticker_that_leaves_gets_one_absent_row_and_one_present_row_when_back(rawstore_tmp_db, tmp_path, http):
    _serve(http)
    _night(tmp_path, TUE)
    _serve(http, exchange=[r for r in EXCHANGE if r[2] != "SMOL"])
    assert _night(tmp_path, WED)["exchange"]["absent"] == 1
    assert _night(tmp_path, THU)["exchange"]["absent"] == 0          # still gone: no second 'absent' row
    _serve(http)
    assert _night(tmp_path, FRI)["exchange"]["appended"] == 1        # back: a new 'present' row
    smol = [r for r in _rows("sec_exchange_file", "ticker") if r["ticker"] == "SMOL"]
    assert [r["status"] for r in smol] == ["present", "absent", "present"]
    assert smol[1]["cik"] is None and smol[1]["valid_from"] == date(2026, 10, 14)


def test_a_fund_class_that_leaves_or_changes_symbol(rawstore_tmp_db, tmp_path, http):
    _serve(http)
    _night(tmp_path, TUE)
    _serve(http, funds=[[2110, "S000009184", "C000024954", "LACBX"], FUNDS[2]])
    res = _night(tmp_path, WED)
    assert (res["mf"]["appended"], res["mf"]["absent"], res["mf"]["unchanged"]) == (1, 1, 1)
    mf = _rows("sec_mf_file", "class_id")
    assert [(r["class_id"], r["symbol"], r["status"]) for r in mf if r["run_at"] == WED] == [
        ("C000024954", "LACBX", "present"), ("C000024956", None, "absent")]


def test_a_pull_below_the_floor_appends_nothing(rawstore_tmp_db, tmp_path, http, monkeypatch):
    _serve(http)
    _night(tmp_path, TUE)
    monkeypatch.setattr(sec_files, "MIN_MEMBERS", 3)
    monkeypatch.setattr(sec_files, "MIN_FUND_CLASSES", 3)
    _serve(http, exchange=EXCHANGE[:2], funds=FUNDS[:2])          # 2 members, 2 classes: a short file
    res = _night(tmp_path, WED)
    assert res["status"] == "failed"
    assert res["exchange"]["status"] == "failed" and "floor 3" in res["exchange"]["error"]
    assert res["mf"]["status"] == "failed" and "floor 3" in res["mf"]["error"]
    assert len(_rows("sec_exchange_file", "ticker")) == 4 and len(_rows("sec_mf_file", "class_id")) == 3


def test_at_most_one_request_per_file_per_night_even_on_a_rerun(rawstore_tmp_db, tmp_path, http):
    _serve(http, ex_status=503, mf_status=503)
    res = _night(tmp_path, TUE)
    assert res["status"] == "failed" and len(http.calls) == 2 and res["requests"] == 2     # no retry
    _serve(http)
    again = _night(tmp_path, TUE + 3600)          # a manual rerun the same New York night
    assert len(http.calls) == 2 and again["requests"] == 0 and again["status"] == "skipped"
    assert again["exchange"]["status"] == "skipped" and "already asked" in again["exchange"]["reason"]
    late = _night(tmp_path, _at("2026-10-14T05:00:00"))     # 07:00 Amsterdam: Wednesday 01:00 in New York, still
    assert len(http.calls) == 2 and late["status"] == "skipped"   # Tuesday's session, so the same night
    assert _rows("sec_exchange_file", "ticker") == []
    assert _night(tmp_path, WED)["requests"] == 2                     # the next night asks again


def test_one_file_failing_leaves_the_other_written(rawstore_tmp_db, tmp_path, http):
    _serve(http, ex_status=503)
    res = _night(tmp_path, TUE)
    assert res["status"] == "failed" and res["exchange"]["status"] == "failed" and "503" in res["exchange"]["error"]
    assert res["mf"]["status"] == "ok" and len(_rows("sec_mf_file", "class_id")) == 3
    assert _rows("sec_exchange_file", "ticker") == []


def test_a_weekend_night_makes_no_request(rawstore_tmp_db, tmp_path, http):
    _serve(http)
    res = _night(tmp_path, SAT)
    assert res["status"] == "skipped" and res["reason"] == "weekend" and http.calls == []


def test_it_never_raises(rawstore_tmp_db, tmp_path, http, monkeypatch):
    _serve(http)

    def boom(*a, **k):
        raise RuntimeError("store is locked")

    monkeypatch.setattr(sec_files, "record", boom)
    res = _night(tmp_path, TUE)
    assert res["status"] == "failed" and "store is locked" in res["exchange"]["error"]
    monkeypatch.setattr(sec_files, "_ny_date", boom)
    assert _night(tmp_path, WED)["status"] == "failed"


def test_no_request_without_a_writable_marker(rawstore_tmp_db, tmp_path, http):
    _serve(http)
    blocker = tmp_path / "markers"
    blocker.write_text("a file where the marker folder should be", encoding="utf-8")
    res = _night(tmp_path, TUE)
    assert http.calls == [] and res["requests"] == 0 and res["status"] == "failed"
    assert "marker" in res["exchange"]["error"]


def test_the_summary_line_names_both_files(rawstore_tmp_db, tmp_path, http):
    _serve(http, ex_status=503)
    line = sec_files.summary_line(_night(tmp_path, TUE))
    assert line.startswith("SEC files: ") and "exchange failed" in line and "mf ok 3 appended" in line
    assert "2 SEC requests" in line


def test_old_markers_are_pruned_and_nothing_else(rawstore_tmp_db, tmp_path, http):
    _serve(http)
    markers = tmp_path / "markers"
    markers.mkdir()
    for name in ("2026-09-30_exchange", "2026-10-09_mf", "2026-09-30_notes", "keep.txt"):
        (markers / name).write_text("", encoding="utf-8")
    _night(tmp_path, TUE)
    assert sorted(f.name for f in markers.iterdir()) == [
        "2026-09-30_notes", "2026-10-09_mf", "2026-10-13_exchange", "2026-10-13_mf", "keep.txt"]
