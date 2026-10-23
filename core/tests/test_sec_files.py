"""SEC exchange and fund files (#301, D21 + D8): change history, floors, one request per file per weekday night."""
from __future__ import annotations

import json
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


def _serve(http, exchange=EXCHANGE, funds=FUNDS, ex_status=200, mf_status=200, ex_headers=None, mf_headers=None):
    http.routes.clear()
    http.add("company_tickers_exchange.json", {"fields": _EX_FIELDS, "data": exchange}, status=ex_status,
             headers=ex_headers)
    http.add("company_tickers_mf.json", {"fields": _MF_FIELDS, "data": funds}, status=mf_status,
             headers=mf_headers)


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


# ── the readers: the universe and identity jobs reuse the stored rows (#301 D21) ──────────────────────

def test_the_stored_rows_are_the_newest_present_row_per_key(rawstore_tmp_db, tmp_path, http):
    """``latest`` is what both readers get: one row per key, the newest, and never an 'absent' one."""
    _serve(http)
    _night(tmp_path, TUE)
    _serve(http, exchange=[r if r[2] != "SMOL" else [2000, "Small Co", "SMOL", "NYSE"] for r in EXCHANGE]
           + [[3000, "New Co", "NEW", "Nasdaq"]])
    _night(tmp_path, WED)
    _serve(http, exchange=[r for r in EXCHANGE if r[2] != "SMOL"] + [[3000, "New Co", "NEW", "Nasdaq"]])
    _night(tmp_path, THU)

    rows = {r["ticker"]: r for r in sec_files.latest("exchange")}
    assert "SMOL" not in rows, "an 'absent' newest row hides the ticker"
    assert rows["NEW"]["exchange"] == "Nasdaq"
    assert set(rows) == {"AAPL", "BRK-B", "OTCX", "NEW"}
    assert [r["ticker"] for r in sec_files.latest("exchange", keys=["AAPL", "SMOL"])] == ["AAPL"]


def test_the_universe_reader_uses_the_stored_rows_and_makes_no_sec_call(rawstore_tmp_db, tmp_path, http,
                                                                       monkeypatch):
    from RevenueChaser.core import universe_fetch as uf

    monkeypatch.setattr(uf, "MIN_FULL_UNIVERSE", 2)
    _serve(http)
    _night(tmp_path, TUE)
    asked = len(http.calls)

    def no_sec(*a, **k):  # pragma: no cover - the reader must not ask
        raise AssertionError("the universe reader must not pull the SEC file")

    from RevenueChaser.core.identity.sources import sec
    monkeypatch.setattr(sec, "exchange_map", no_sec)

    members, reason = uf._sec_member_list()

    assert reason is None
    assert {m["ticker"] for m in members} == {"AAPL", "BRK-B", "SMOL"}      # OTC is not a member
    assert len(http.calls) == asked


def test_the_universe_reader_falls_back_to_the_pull_when_nothing_is_stored(rawstore_tmp_db, http, monkeypatch):
    """Nothing stored is a fresh install, a restored store, or any run before the first nightly — not a reason
    to hand back an empty universe. The pull is the fallback, so membership still resolves (#301)."""
    from RevenueChaser.core import universe_fetch as uf
    monkeypatch.setattr(uf, "MIN_FULL_UNIVERSE", 3)
    _serve(http)

    members, reason = uf._sec_member_list()

    assert reason is None, reason
    # sec.exchange_map() normalises a dotted class ticker the same way on both paths, so BRK.B arrives as BRK-B
    assert [m["ticker"] for m in members] == ["AAPL", "BRK-B", "SMOL"]


def test_the_universe_reader_says_so_when_nothing_is_stored_and_the_pull_fails(rawstore_tmp_db, http, monkeypatch):
    """The other half: with no stored rows AND no answer from SEC there is genuinely no member list, and the
    caller must be told rather than shown an empty universe."""
    from RevenueChaser.core import universe_fetch as uf
    monkeypatch.setattr(uf, "MIN_FULL_UNIVERSE", 3)
    _serve(http, ex_status=500)

    members, reason = uf._sec_member_list()

    assert members == [] and reason


def test_the_identity_reader_uses_the_stored_rows_and_makes_no_sec_call(rawstore_tmp_db, tmp_path, http):
    from RevenueChaser.core.identity import refresh

    _serve(http)
    _night(tmp_path, TUE)
    asked = len(http.calls)

    res = refresh._default_clients()["sec_exchange"]()

    assert res.ok and len(http.calls) == asked
    assert {r["ticker"] for r in res.rows} == {"AAPL", "BRK-B", "OTCX", "SMOL"}
    assert {r["cik"] for r in res.rows} >= {"320193"}


def test_the_identity_reader_is_an_error_not_an_empty_ok_when_nothing_is_stored(rawstore_tmp_db):
    from RevenueChaser.core.identity import refresh

    res = refresh._default_clients()["sec_exchange"]()
    assert res.status == "error" and "stored" in res.error


def test_health_summary_presents_the_store_against_the_file(rawstore_tmp_db, tmp_path, http, monkeypatch):
    """Data Health: present rows vs the file, members vs the floor, rows appended and Last-Modified."""
    monkeypatch.setattr(sec_files, "MIN_MEMBERS", 3)
    _serve(http, ex_headers={"Last-Modified": "Sun, 12 Oct 2026 20:30:00 GMT"})
    _night(tmp_path, TUE)

    h = sec_files.health_summary(marker_dir=tmp_path / "markers")

    ex = h["exchange"]
    assert (ex["present"], ex["file_rows"], ex["members"], ex["floor"]) == (4, 4, 3, 3)
    assert (ex["appended"], ex["absent"]) == (4, 0)
    assert ex["last_modified"] == "Sun, 12 Oct 2026 20:30:00 GMT"
    mf = h["mf"]
    assert (mf["present"], mf["file_rows"], mf["floor"]) == (3, 3, sec_files.MIN_FUND_CLASSES)


def test_health_summary_without_a_pull_still_reports_the_store(rawstore_tmp_db, tmp_path):
    h = sec_files.health_summary(marker_dir=tmp_path / "markers")
    assert h["exchange"]["present"] == 0 and h["exchange"]["file_rows"] is None
    assert h["exchange"]["last_modified"] is None and h["exchange"]["floor"] == sec_files.MIN_MEMBERS


def test_health_summary_separates_the_three_pull_outcomes(rawstore_tmp_db, tmp_path, http, monkeypatch):
    """avo 2026-10-22: the row must tell "pulled, unchanged" from "never written" from "pull failed".

    The tables append on change only, so a stale row and an unchanged file look identical without the pull's
    own outcome: a nightly that never ran and one that ran and found nothing new both append zero.
    """
    markers = tmp_path / "markers"
    monkeypatch.setattr(sec_files, "MIN_MEMBERS", 3)

    # 1. nothing pulled yet
    assert sec_files.health_summary(marker_dir=markers)["exchange"]["last_pull"] == "unknown"

    # 2. a good pull
    _serve(http)
    _night(tmp_path, TUE)
    ex = sec_files.health_summary(marker_dir=markers)["exchange"]
    assert ex["last_pull"] == "ok" and ex["appended"] == 4 and ex["error"] is None

    # 3. a failed pull leaves its own receipt, so the row stops claiming the good one
    _serve(http, ex_status=500)
    _night(tmp_path, WED)
    ex = sec_files.health_summary(marker_dir=markers)["exchange"]
    assert ex["last_pull"] == "failed" and ex["error"]
    assert ex["present"] == 4, "a failed pull must not touch the stored rows"


def test_old_markers_are_pruned_and_nothing_else(rawstore_tmp_db, tmp_path, http):
    _serve(http)
    markers = tmp_path / "markers"
    markers.mkdir()
    for name in ("2026-09-30_exchange", "2026-10-09_mf", "2026-09-30_notes", "keep.txt"):
        (markers / name).write_text("", encoding="utf-8")
    _night(tmp_path, TUE)
    assert sorted(f.name for f in markers.iterdir()) == [
        "2026-09-30_notes", "2026-10-09_mf", "2026-10-13_exchange", "2026-10-13_mf", "keep.txt"]


# ── the typed numbers (#124 S4) ──────────────────────────────────────────────

def test_health_is_the_summary_numbers_typed(rawstore_tmp_db, tmp_path, http):
    """Both files summed: present rows against what the last good pull read, the last good pull's session as
    ``newest`` and the last attempt's as ``expected_as_of``."""
    from RevenueChaser.core import datahealth

    markers = tmp_path / "markers"
    _serve(http, ex_headers={"Last-Modified": "Sun, 12 Oct 2026 20:30:00 GMT"})
    _night(tmp_path, TUE)

    n = sec_files.health_summary(marker_dir=markers)
    h = sec_files.health(marker_dir=markers)

    assert h.covered == n["exchange"]["present"] + n["mf"]["present"] == 7
    assert h.expected == n["exchange"]["file_rows"] + n["mf"]["file_rows"] == 7
    assert (h.newest, h.expected_as_of) == ("2026-10-13", "2026-10-13")
    assert h.source_share is None
    assert datahealth.coverage_text(h) == "100.0% (7 of 7)"


def test_health_keeps_the_three_pull_outcomes_verbatim(rawstore_tmp_db, tmp_path, http):
    """avo 2026-10-22: the row must tell "pulled, unchanged" from "never written" from "pull failed".

    The wording is the note's, byte for byte; ``Health.problems`` carries it so S5 derives the note from the
    typed record instead of re-reading the receipts.
    """
    markers = tmp_path / "markers"

    # 1. nothing pulled yet
    assert sec_files.health(marker_dir=markers).problems == (
        "exchange: no pull recorded yet", "mf: no pull recorded yet")

    # 2. a good pull that appended rows
    _serve(http)
    _night(tmp_path, TUE)
    assert sec_files.health(marker_dir=markers).problems == (
        "exchange: pulled 2026-10-13, appended 4 (+0 absent)",
        "mf: pulled 2026-10-13, appended 3 (+0 absent)")

    # 3. a good pull of an unchanged file
    _serve(http)
    _night(tmp_path, WED)
    assert sec_files.health(marker_dir=markers).problems == (
        "exchange: pulled 2026-10-14, unchanged", "mf: pulled 2026-10-14, unchanged")

    # 4. a failed night names its own night, keeps the last good pull, and dates the store by the last good pull
    _serve(http, ex_status=500, mf_status=500)
    _night(tmp_path, THU)
    h = sec_files.health(marker_dir=markers)
    assert h.problems[0].startswith("exchange: pull FAILED 2026-10-15 (")
    assert h.problems[1].startswith("mf: pull FAILED 2026-10-15 (")
    assert h.problems[0].endswith("), last good pull 2026-10-14"), h.problems[0]
    assert (h.newest, h.expected_as_of) == ("2026-10-14", "2026-10-15"), "a failing night makes the store stale"

    # 5. the sentence itself, byte for byte, from a receipt that recorded SEC's own error
    (markers / "2026-10-16_exchange").write_text(
        json.dumps({"session": "2026-10-16", "status": "failed", "error": "SEC 500"}), encoding="utf-8")
    assert sec_files.health(marker_dir=markers).problems[0] == (
        "exchange: pull FAILED 2026-10-16 (SEC 500), last good pull 2026-10-14")


def test_health_without_a_pull_is_na_and_says_so(rawstore_tmp_db, tmp_path):
    """No receipt: no expected population and no dates, so ``n/a`` and ``unknown`` — never a percentage."""
    from RevenueChaser.core import datahealth

    h = sec_files.health(marker_dir=tmp_path / "markers")

    assert (h.covered, h.expected) == (0, None)
    assert (h.newest, h.expected_as_of) == (None, None)
    assert datahealth.coverage_text(h) == "n/a" and "%" not in datahealth.coverage_text(h)
    assert datahealth.freshness_text(h, "2026-10-23") == "unknown"
