"""SEC adapters: the exchange map and per-CIK submissions (recorded R1 responses, no network)."""
from __future__ import annotations

import copy
import re

import httpx

from RevenueChaser.core.identity.sources import sec
from RevenueChaser.core.providers import sec_edgar

from RevenueChaser.core.identity.tests.helpers import FakeResp, fixture_json

CIK = {"AAPL": 320193, "GOOGL": 1652044, "ARCC": 1287750, "PDI": 1510599, "DNP": 806628,
       "OZK": 1569650, "TCPC": 1370755, "ASML": 937966, "AZN": 901832, "BRK-B": 1067983,
       "BEAG": 1852207}


def _sub(cik: int):
    return fixture_json(f"submissions/CIK{cik:010d}.json")


def route_submissions(http, overrides: dict[int, dict] | None = None):
    """Serve the recorded main pages; ``_older`` becomes the ``-submissions-001`` page."""
    pages = {c: _sub(c) for c in CIK.values()}
    pages.update(overrides or {})

    def main(call):
        m = re.search(r"CIK(\d{10})\.json$", call["url"])
        page = pages.get(int(m.group(1))) if m else None
        if page is None:
            return FakeResp({}, 404)
        return {k: v for k, v in page.items() if k != "_older"}

    def older(call):
        m = re.search(r"CIK(\d{10})-submissions-001\.json$", call["url"])
        page = pages.get(int(m.group(1))) if m else None
        if page is None or "_older" not in page:
            return FakeResp({}, 404)
        return page["_older"]

    http.add("-submissions-001.json", older)
    http.add("/submissions/CIK", main)
    return pages


# -- exchange map -------------------------------------------------------------
def test_exchange_map_happy_path(http):
    http.add("/files/company_tickers_exchange.json", fixture_json("sec_exchange.json"))
    r = sec.exchange_map()
    assert r.source == "sec_exchange" and r.status == "ok" and r.error is None
    by = {x["ticker"]: x for x in r.rows}
    assert by["AAPL"]["cik"] == "320193" and by["AAPL"]["exchange"] == "Nasdaq"
    assert by["AAPL"]["name"] == "Apple Inc."
    assert "BRK-B" in by and "BRK.B" not in by          # dotted tickers are normalised
    assert http.calls[0]["url"].startswith("https://www.sec.gov/")
    assert "User-Agent" in http.calls[0]["headers"]     # the shared SEC client's headers


def test_exchange_map_5xx_is_an_error_not_a_raise(http):
    http.add("company_tickers_exchange.json", {"error": "x"}, status=503)
    r = sec.exchange_map()
    assert r.status == "error" and r.rows == [] and "503" in r.error
    assert len(http.calls) == sec_edgar._MAX_RETRIES    # the shared backoff ran


def test_exchange_map_timeout_is_an_error(http):
    http.add("company_tickers_exchange.json", httpx.ReadTimeout("slow"))
    r = sec.exchange_map()
    assert r.status == "error" and "ReadTimeout" in r.error


def test_exchange_map_empty_or_malformed_body_is_an_error(http):
    http.add("company_tickers_exchange.json", {"fields": ["cik"], "data": []})
    assert sec.exchange_map().status == "error"
    http.routes.clear()
    http.add("company_tickers_exchange.json", {"unexpected": 1})
    assert sec.exchange_map().status == "error"


def test_exchange_map_base_url_override(http, monkeypatch):
    monkeypatch.setattr(sec_edgar, "SEC_WWW_BASE", "http://mock-sec-www")
    http.add("company_tickers_exchange.json", fixture_json("sec_exchange.json"))
    sec.exchange_map()
    assert http.calls[0]["url"] == "http://mock-sec-www/files/company_tickers_exchange.json"


def test_exchange_map_with_one_attempt_asks_once(http):
    """#301: the nightly SEC-file step allows itself one request per file, so no retry."""
    http.add("company_tickers_exchange.json", {"error": "x"}, status=503)
    r = sec.exchange_map(attempts=1)
    assert r.status == "error" and "503" in r.error and len(http.calls) == 1


# -- fund file (#301, D8) -----------------------------------------------------
_MF = {"fields": ["cik", "seriesId", "classId", "symbol"],
       "data": [[2110, "S000009184", "C000024954", "LACAX"], [2110, "S000009184", "C000024956", ""],
                [2110, "S000009184", "C000024954", "DUPE"], [None, None, "", "NOCLASS"]]}


def test_fund_map_reads_one_row_per_class(http):
    http.add("/files/company_tickers_mf.json", _MF)
    r = sec.fund_map()
    assert r.source == "sec_mf" and r.status == "ok"
    assert r.rows == [{"class_id": "C000024954", "cik": "2110", "series_id": "S000009184", "symbol": "LACAX"},
                      {"class_id": "C000024956", "cik": "2110", "series_id": "S000009184", "symbol": None}]
    assert http.calls[0]["url"] == "https://www.sec.gov/files/company_tickers_mf.json"
    assert "User-Agent" in http.calls[0]["headers"]


def test_fund_map_failure_is_an_error_not_a_raise(http):
    http.add("company_tickers_mf.json", {"fields": ["cik", "seriesId", "classId", "symbol"], "data": []})
    assert sec.fund_map().status == "error"
    http.routes.clear()
    http.add("company_tickers_mf.json", {"unexpected": 1})
    assert sec.fund_map().status == "error"
    http.routes.clear()
    http.calls.clear()
    http.add("company_tickers_mf.json", {"error": "x"}, status=503)
    r = sec.fund_map(attempts=1)
    assert r.status == "error" and "503" in r.error and len(http.calls) == 1


# -- submissions --------------------------------------------------------------
def test_submissions_summarise_each_cik(http):
    route_submissions(http)
    r = sec.submissions([CIK["AAPL"], CIK["ASML"], CIK["PDI"], CIK["OZK"]],
                        ticker_by_cik={CIK["AAPL"]: "AAPL"})
    assert r.status == "ok"
    by = {x["cik"]: x for x in r.rows}
    aapl = by["320193"]
    assert aapl["status"] == "ok" and aapl["ticker"] == "AAPL"
    assert aapl["sic"] == "3571" and aapl["name"] == "Apple Inc."
    assert aapl["entity_type"] == "operating" and "10-K" in aapl["forms"]
    assert aapl["shell_closed"] is False and isinstance(aapl["forms"], list)
    assert aapl["tickers"] == ["AAPL"] and aapl["exchanges"] == ["Nasdaq"]
    assert "5.02" in aapl["items_8k"]
    assert "20-F" in by["937966"]["forms"]                       # ASML: foreign filer
    assert by["1510599"]["sic"] == ""                            # PDI: a fund, no SIC
    assert any(f.startswith("486") or f.startswith("40-") for f in by["1510599"]["forms"])
    assert "ticker" not in by["937966"]                          # no mapping supplied
    assert r.fetched_at


def test_bdc_that_also_files_ncsr_keeps_both_form_families(http):
    route_submissions(http)
    row = sec.submissions([CIK["TCPC"]]).rows[0]
    assert "N-54A" in row["forms"]
    assert {"N-CSR", "N-CSRS", "NPORT-P"} & set(row["forms"])
    assert "10-K" in row["forms"]


def test_older_pages_are_fetched_when_recent_lacks_fund_forms(http):
    """ARCC shows fund-ish forms on its recent page but no N-54A: R1 fetched the older page."""
    route_submissions(http)
    row = sec.submissions([CIK["ARCC"]]).rows[0]
    assert row["older_fetched"] is True
    assert http.urls("-submissions-001.json")
    assert "13F-HR" in row["forms"]                              # only on the older page in R1


def test_older_pages_are_not_fetched_when_recent_is_enough(http):
    route_submissions(http)
    row = sec.submissions([CIK["AAPL"]]).rows[0]
    assert row["older_fetched"] is False
    assert not http.urls("-submissions-001.json")


def _spac(items_recent=None, items_older=None, cik=9900001):
    page = {"cik": str(cik), "name": "Example Acquisition Corp.", "sic": "6770",
            "sicDescription": "Blank Checks", "entityType": "operating", "tickers": ["EXAC"],
            "exchanges": ["Nasdaq"],
            "filings": {"recent": {"form": ["S-1", "8-K"], "items": ["", items_recent or "1.01,9.01"]},
                        "files": []}}
    if items_older is not None:
        page["filings"]["files"] = [{"name": f"CIK{cik:010d}-submissions-001.json"}]
        page["_older"] = {"form": ["8-K", "8-K"], "items": ["2.01", items_older]}
    return cik, page


def test_shell_closed_false_without_item_506(http):
    cik, page = _spac()
    route_submissions(http, {cik: page})
    assert sec.submissions([cik]).rows[0]["shell_closed"] is False


def test_shell_closed_true_with_item_506_in_recent(http):
    cik, page = _spac(items_recent="5.06,9.01")
    route_submissions(http, {cik: page})
    row = sec.submissions([cik]).rows[0]
    assert row["shell_closed"] is True and "5.06" in row["items_8k"]


def test_shell_closed_found_on_an_older_page(http):
    cik, page = _spac(items_older="5.06")
    route_submissions(http, {cik: page})
    row = sec.submissions([cik]).rows[0]
    assert row["older_fetched"] is True and row["shell_closed"] is True


def test_item_506_on_a_non_8k_form_does_not_count(http):
    cik, page = _spac()
    page = copy.deepcopy(page)
    page["filings"]["recent"]["form"].append("10-Q")
    page["filings"]["recent"]["items"].append("5.06")
    route_submissions(http, {cik: page})
    assert sec.submissions([cik]).rows[0]["shell_closed"] is False


def test_the_real_spac_fixture_without_506_is_not_shell_closed(http):
    route_submissions(http)
    row = sec.submissions([CIK["BEAG"]]).rows[0]
    assert row["sic"] == "6770" and row["shell_closed"] is False


def test_unknown_cik_is_a_not_found_row_not_a_failure(http):
    route_submissions(http)
    r = sec.submissions([CIK["AAPL"], 12345])
    assert r.status == "ok"
    by = {x["cik"]: x for x in r.rows}
    assert by["12345"]["status"] == "not_found" and by["320193"]["status"] == "ok"


def test_failed_older_page_marks_the_row_error_so_it_is_not_cached(http):
    cik, page = _spac(items_older="5.06")
    route_submissions(http, {cik: page})
    http.routes.insert(0, ("-submissions-001.json", {"x": 1}, 500, {}))
    r = sec.submissions([cik])
    assert r.rows[0]["status"] == "error" and r.status == "error"


def test_all_ciks_failing_is_an_error_result(http):
    http.add("/submissions/CIK", {}, status=503)
    r = sec.submissions([CIK["AAPL"], CIK["ASML"]])
    assert r.status == "error" and "2 of 2" in r.error
    assert all(x["status"] == "error" for x in r.rows)


def test_partial_failure_keeps_ok_rows_and_stays_ok(http):
    route_submissions(http)
    http.routes.insert(0, ("CIK0000937966.json", {}, 503, {}))
    r = sec.submissions([CIK["AAPL"], CIK["ASML"]])
    by = {x["cik"]: x for x in r.rows}
    assert by["320193"]["status"] == "ok" and by["937966"]["status"] == "error"
    assert r.status == "ok" and "1 of 2" in (r.error or "")


def test_timeout_does_not_raise(http):
    http.add("/submissions/CIK", httpx.ConnectTimeout("t"))
    r = sec.submissions([CIK["AAPL"]])
    assert r.status == "error" and "ConnectTimeout" in r.rows[0]["error"]


def test_no_ciks_is_an_ok_empty_result(http):
    r = sec.submissions([])
    assert r.status == "ok" and r.rows == [] and not http.calls


def test_submissions_go_through_the_shared_throttle(http, monkeypatch):
    route_submissions(http)
    ticks = []
    monkeypatch.setattr(sec_edgar, "_throttle", lambda: ticks.append(1))
    sec.submissions([CIK["AAPL"], CIK["ASML"], CIK["OZK"]])
    assert len(ticks) == len(http.calls) == 3


def test_submissions_base_url_override(http, monkeypatch):
    route_submissions(http)
    monkeypatch.setattr(sec_edgar, "SEC_DATA_BASE", "http://mock-sec-data")
    sec.submissions([CIK["AAPL"]])
    assert http.calls[0]["url"] == "http://mock-sec-data/submissions/CIK0000320193.json"


def test_ciks_are_deduplicated_and_coerced(http):
    route_submissions(http)
    r = sec.submissions([CIK["AAPL"], str(CIK["AAPL"]), CIK["AAPL"]])
    assert len(r.rows) == 1 and len(http.calls) == 1
