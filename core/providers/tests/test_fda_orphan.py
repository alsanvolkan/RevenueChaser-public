"""Tests for the FDA OOPD (orphan drug) provider: fetch + parse + filter.

Fixtures (``core/providers/tests/fixtures/``) are real saved OOPD pages
(copied read-only from ``.cache/research/186`` for research issue #186), not
synthesised HTML -- including a real zero-hit results page
(``oopd_zero_hits.html``) and a real page returned without the search
session cookie (``oopd_no_session.html``, no ``total=``/``Designations:``
marker at all).
"""
from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from RevenueChaser.core.biotech.sponsor import normalise_sponsor
from RevenueChaser.core.providers import fda_orphan

FIXTURES = Path(__file__).parent / "fixtures"
TENAYA_HTML = (FIXTURES / "oopd_tenaya_detailed.html").read_text(encoding="utf-8")
AMGEN_HTML = (FIXTURES / "oopd_amgen_page1.html").read_text(encoding="utf-8")
ZERO_HITS_HTML = (FIXTURES / "oopd_zero_hits.html").read_text(encoding="utf-8")
NO_SESSION_HTML = (FIXTURES / "oopd_no_session.html").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    """Every test is instant by default; ``_record_sleep`` overrides this."""
    monkeypatch.setattr(fda_orphan, "_THROTTLE_INTERVAL", 0.0)
    monkeypatch.setattr(fda_orphan.time, "sleep", lambda _s: None)


@pytest.fixture
def _record_sleep(monkeypatch):
    """Record every ``time.sleep`` call (still doesn't actually sleep)."""
    calls: list[float] = []
    monkeypatch.setattr(fda_orphan.time, "sleep", lambda s: calls.append(s))
    return calls


def _make_client(handler):
    transport = httpx.MockTransport(handler)
    return httpx.Client(transport=transport)


# ---------------------------------------------------------------------------
# parse_results: pure parsing, no I/O
# ---------------------------------------------------------------------------

def test_parse_results_tenaya_returns_three_rows():
    rows = fda_orphan.parse_results(TENAYA_HTML)
    assert len(rows) == 3


def test_parse_results_tenaya_row1():
    rows = fda_orphan.parse_results(TENAYA_HTML)
    row = rows[0]
    assert "MYBPC3" in row["generic_name"]
    assert row["date_designated"] == "2021-05-12"
    assert row["designation_status"] == "Designated"
    assert row["approval_status"] == "Not FDA Approved for Orphan Indication"
    assert row["sponsor"] == "Tenaya Therapeutics"
    assert row["marketing_approval_date"] is None
    assert row["trade_name"] is None


def test_parse_results_tenaya_row2():
    rows = fda_orphan.parse_results(TENAYA_HTML)
    row = rows[1]
    assert "PKP2" in row["generic_name"] or "ARVC" in row["designation"]
    assert row["date_designated"] == "2022-11-22"
    assert row["sponsor"] == "Tenaya Therapeutics"


def test_parse_results_tenaya_row3():
    rows = fda_orphan.parse_results(TENAYA_HTML)
    row = rows[2]
    assert "Duchenne" in row["designation"]
    assert row["date_designated"] == "2026-04-17"
    assert row["sponsor"] == "Tenaya Therapeutics, Inc."


def test_parse_results_amgen_returns_25_rows():
    rows = fda_orphan.parse_results(AMGEN_HTML)
    assert len(rows) == 25


def test_parse_results_amgen_approved_record_has_date_and_trade_name():
    """Record 8's page reuses its ``id`` for a second, unrelated ``<th>``
    ("Approved Labeled Indication") -- the first occurrence must win so the
    real marketing-approval date survives instead of being clobbered."""
    rows = fda_orphan.parse_results(AMGEN_HTML)
    otezla = next(r for r in rows if r["trade_name"] == "Otezla XR")
    assert otezla["marketing_approval_date"] == "2025-08-29"


def test_parse_results_amgen_most_records_have_no_trade_name():
    rows = fda_orphan.parse_results(AMGEN_HTML)
    with_trade = [r for r in rows if r["trade_name"] is not None]
    without_trade = [r for r in rows if r["trade_name"] is None]
    assert with_trade
    assert without_trade


def test_parse_results_zero_hits_fixture_returns_no_rows():
    assert fda_orphan.parse_results(ZERO_HITS_HTML) == []


def test_parse_results_500_records_under_one_second():
    """A synthetic 500-record page (the Tenaya record-1 shape, renumbered)
    parses in well under a second -- this is a single regex pass, not one
    regex search per field per record."""
    record_tmpl = (
        '<tr><th id="Generic name for Record Number {n}">Generic name:</th>'
        '<td>MYBPC3-associated hypertrophic cardiomyopathy gene therapy</td></tr>'
        '<tr><th id="Date designated for Record Number {n}">Date designated:</th>'
        '<td>05/12/2021</td></tr>'
        '<tr><th id="Orphan designation for Record Number {n}">Orphan designation:</th>'
        '<td>Treatment of MYBPC3-associated hypertrophic cardiomyopathy</td></tr>'
        '<tr><th id="Orphan designation status for Record Number {n}">status:</th>'
        '<td>Designated</td></tr>'
        '<tr><th id="FDA Orphan Approval status for Record Number {n}">approval:</th>'
        '<td>Not FDA Approved for Orphan Indication</td></tr>'
        '<tr><th id="Sponsor for Record Number {n}">Sponsor:</th>'
        '<td>Tenaya Therapeutics<BR/>701 Oyster Point Blvd</td></tr>'
    )
    doc = "<html><body>" + "".join(record_tmpl.format(n=n) for n in range(1, 501)) + "</body></html>"

    start = time.perf_counter()
    rows = fda_orphan.parse_results(doc)
    elapsed = time.perf_counter() - start

    assert len(rows) == 500
    assert rows[0]["sponsor"] == "Tenaya Therapeutics"
    assert elapsed < 1.0


# ---------------------------------------------------------------------------
# _total: the page's own result count, or None if the page is unrecognised
# ---------------------------------------------------------------------------

def test_total_from_js_marker():
    assert fda_orphan._total(TENAYA_HTML) == 3


@pytest.mark.parametrize("doc", [
    "<html><body><p>Designations: 42</p></body></html>",
    "<html><script>var subtotal=0;</script></html>",
    "<html><script>cart_total = 0;</script></html>",
    "<html><script>total=3;</script></html>",
])
def test_total_needs_the_anchored_pager_marker(doc):
    assert fda_orphan._total(doc) is None


def test_total_zero_on_real_zero_hit_fixture():
    assert fda_orphan._total(ZERO_HITS_HTML) == 0


def test_total_none_on_real_no_session_fixture():
    assert fda_orphan._total(NO_SESSION_HTML) is None


# ---------------------------------------------------------------------------
# _do_request: throttled GET/POST with 5xx/429 retry, no retry on other 4xx
# ---------------------------------------------------------------------------

def test_do_request_retries_500_twice_then_succeeds(_record_sleep):
    responses = [httpx.Response(500, text="err"), httpx.Response(500, text="err"),
                 httpx.Response(200, text="ok")]
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return responses[len(calls) - 1]

    client = _make_client(handler)
    counter = [0]
    r = fda_orphan._do_request("post", "https://example.test/x", client, counter)

    assert r.status_code == 200
    assert len(calls) == 3
    assert all(c.method == "POST" for c in calls)
    assert counter[0] == 3


def test_do_request_404_fails_after_exactly_one_try(_record_sleep):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    client = _make_client(handler)
    counter = [0]
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        fda_orphan._do_request("post", "https://example.test/x", client, counter)

    assert exc_info.value.response.status_code == 404
    assert counter[0] == 1
    assert _record_sleep == []


def test_do_request_429_sleeps_for_retry_after_seconds(_record_sleep):
    responses = [httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, text="ok")]
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return responses[len(calls) - 1]

    client = _make_client(handler)
    counter = [0]
    r = fda_orphan._do_request("post", "https://example.test/x", client, counter)

    assert r.status_code == 200
    assert counter[0] == 2
    assert _record_sleep == [7]


def test_do_request_sends_a_user_agent_header():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        return httpx.Response(200, text="ok")

    client = _make_client(handler)
    fda_orphan._do_request("get", "https://example.test/x", client, [0])

    assert captured["headers"].get("user-agent") == fda_orphan.FDA_USER_AGENT_DEFAULT


# ---------------------------------------------------------------------------
# search_sponsor: GET (cookie) then POST, through an injected client
# ---------------------------------------------------------------------------

def test_search_sponsor_does_get_then_post_once_each():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, text="<html>cookie page</html>")
        return httpx.Response(200, text=TENAYA_HTML)

    client = _make_client(handler)
    result = fda_orphan.search_sponsor("Tenaya Therapeutics", client=client)

    assert len(calls) == 2
    assert calls[0].method == "GET"
    assert "index.cfm" in str(calls[0].url)
    assert calls[1].method == "POST"
    assert "OOPD_Results.cfm" in str(calls[1].url)
    assert result.status == "ok"
    assert len(result.data) == 3


def test_search_sponsor_post_body_fields():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            captured["form"] = parse_qs(request.content.decode("utf-8"), keep_blank_values=True)
            return httpx.Response(200, text=TENAYA_HTML)
        return httpx.Response(200, text="<html></html>")

    client = _make_client(handler)
    fda_orphan.search_sponsor("Tenaya Therapeutics", client=client)

    form = captured["form"]
    assert form["sponsor_name"] == ["Tenaya Therapeutics"]
    assert form["RecordsPerPage"] == ["500"]
    assert form["Output_Format"] == ["Detailed"]
    assert form["Search_param"] == ["DESDATE"]
    assert form["newSearch"] == ["Run Search"]
    assert form["Designation_Start_Date"] == ["01/01/1983"]
    assert form["Product_name"] == [""]
    assert form["Designation"] == [""]
    assert '"' not in form["Designation_End_Date"][0]


def test_search_sponsor_follows_the_results_redirect():
    """The live server answers the POST with 302 -> listResult.cfm (seen 2026-10-02)."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(302, headers={"Location": "listResult.cfm"})
        if "listResult.cfm" in str(request.url):
            return httpx.Response(200, text=TENAYA_HTML)
        return httpx.Response(200, text="<html>cookie page</html>")

    result = fda_orphan.search_sponsor("Tenaya Therapeutics", client=_make_client(handler))

    assert result.status == "ok"
    assert len(result.data) == 3


def test_search_sponsor_partial_when_total_exceeds_parsed():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="<html></html>")
        return httpx.Response(200, text=AMGEN_HTML)

    client = _make_client(handler)
    result = fda_orphan.search_sponsor("Amgen", client=client)

    assert result.status == "partial"
    assert len(result.data) == 25


def test_search_sponsor_ok_with_real_zero_hit_fixture():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="<html></html>")
        return httpx.Response(200, text=ZERO_HITS_HTML)

    client = _make_client(handler)
    result = fda_orphan.search_sponsor("Nonexistent Sponsor Co", client=client)

    assert result.status == "ok"
    assert result.data == []


def test_search_sponsor_empty_query_makes_no_request():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, text="<html></html>")

    client = _make_client(handler)
    result = fda_orphan.search_sponsor("   ", client=client)

    assert result.status == "failed"
    assert result.error["error"] == "empty_query"
    assert result.data == []
    assert calls == []


def test_search_sponsor_failed_when_session_cookie_dropped():
    """A page with neither ``total=`` nor ``Designations:`` (the real
    cookie-less page) is unrecognised, not a zero-hit result."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="<html></html>")
        return httpx.Response(200, text=NO_SESSION_HTML)

    client = _make_client(handler)
    result = fda_orphan.search_sponsor("Tenaya Therapeutics", client=client)

    assert result.status == "failed"
    assert result.error["error"] == "unrecognised_page"
    assert result.data == []


def test_search_sponsor_failed_when_total_positive_but_no_rows_parsed():
    broken_html = "<html><script>start=1; page=25; total=5;</script><body>no records here</body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="<html></html>")
        return httpx.Response(200, text=broken_html)

    client = _make_client(handler)
    result = fda_orphan.search_sponsor("Tenaya Therapeutics", client=client)

    assert result.status == "failed"
    assert result.error["error"] == "parse_failed"
    assert result.data == []


def test_search_sponsor_failed_on_transport_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("boom", request=request)

    client = _make_client(handler)
    result = fda_orphan.search_sponsor("Tenaya Therapeutics", client=client)

    assert result.status == "failed"
    assert result.data == []
    assert result.error is not None
    assert result.error["status"] is None


def test_search_sponsor_failed_on_persistent_500():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="<html></html>")
        return httpx.Response(500, text="server error")

    client = _make_client(handler)
    result = fda_orphan.search_sponsor("Tenaya Therapeutics", client=client)

    assert result.status == "failed"
    assert result.data == []
    assert result.error["status"] == 500


def _search_with_page(page: str) -> "fda_orphan.ProviderResult":
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="<html></html>")
        return httpx.Response(200, text=page)

    return fda_orphan.search_sponsor("Tenaya", client=_make_client(handler))


def test_search_sponsor_zero_total_without_no_records_text_is_unrecognised():
    page = ZERO_HITS_HTML.replace("No records found", "Something else")
    result = _search_with_page(page)
    assert result.status == "failed"
    assert result.error["error"] == "unrecognised_page"


def test_search_sponsor_incomplete_row_gives_partial_with_complete_rows_only():
    page = TENAYA_HTML.replace('id="Sponsor for Record Number 2"', 'id="Removed for Record Number 2"')
    result = _search_with_page(page)
    assert result.status == "partial"
    assert len(result.data) == 2
    assert all(r["sponsor"] for r in result.data)


def test_search_sponsor_all_rows_incomplete_is_parse_failed():
    page = TENAYA_HTML
    for n in (1, 2, 3):
        page = page.replace(f'id="Date designated for Record Number {n}"', f'id="Removed for Record Number {n}"')
    result = _search_with_page(page)
    assert result.status == "failed"
    assert result.error["error"] == "parse_failed"


def test_parse_results_missing_fields_are_none_not_empty():
    page = TENAYA_HTML.replace('id="Sponsor for Record Number 2"', 'id="Removed for Record Number 2"')
    row = fda_orphan.parse_results(page)[1]
    assert row["sponsor"] is None


@pytest.mark.parametrize("header,expected", [("86400", 60), ("-5", 0), ("7", 7)])
def test_do_request_retry_after_is_clamped(_record_sleep, header, expected):
    responses = [httpx.Response(429, headers={"Retry-After": header}), httpx.Response(200, text="ok")]
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return responses[len(calls) - 1]

    r = fda_orphan._do_request("post", "https://example.test/x", _make_client(handler), [0])
    assert r.status_code == 200
    assert _record_sleep == [expected]


def test_do_request_retries_transport_errors_three_times(_record_sleep):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    counter = [0]
    with pytest.raises(httpx.ConnectError):
        fda_orphan._do_request("get", "https://example.test/x", _make_client(handler), counter)
    assert counter[0] == 3
    assert _record_sleep == [1.0, 2.0]


def test_designations_for_partial_page_keeps_match_state():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="<html></html>")
        return httpx.Response(200, text=AMGEN_HTML)

    result = fda_orphan.designations_for("AMGEN INC", client=_make_client(handler))
    assert result.status == "partial"
    assert result.data["match"] == "matched"
    assert result.data["rows"]


# ---------------------------------------------------------------------------
# sponsor_query: a short OOPD substring-search query from a raw SEC filer name
# ---------------------------------------------------------------------------

SPONSOR_QUERY_CASES = [
    ("Y-mAbs Therapeutics, Inc.", "Y-mAbs"),
    ("Genmab A/S", "Genmab"),
    ("BRISTOL MYERS SQUIBB CO", "BRISTOL"),
    ("ELI LILLY & Co", "ELI LILLY"),
    ("Tenaya Therapeutics, Inc.", "Tenaya"),
    ("Inc.", ""),
    ("", ""),
    ("A", ""),
    ("The Medicines Company", "Medicines"),
    ("ABC /DE/", "ABC"),
]


@pytest.mark.parametrize("raw,expected", SPONSOR_QUERY_CASES)
def test_sponsor_query(raw, expected):
    assert fda_orphan.sponsor_query(raw) == expected


# ---------------------------------------------------------------------------
# designations_for: normalises the query and classifies rows by sponsor match
# ---------------------------------------------------------------------------

def test_designations_for_matched():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="<html></html>")
        return httpx.Response(200, text=TENAYA_HTML)

    client = _make_client(handler)
    result = fda_orphan.designations_for("Tenaya Therapeutics, Inc.", client=client)

    assert result.status == "ok"
    assert result.data["match"] == "matched"
    assert len(result.data["rows"]) == 3
    assert result.data["candidates"] == []
    assert result.data["query"] == "Tenaya"
    for row in result.data["rows"]:
        assert normalise_sponsor(row["sponsor"]) == "tenaya therapeutics"


def test_designations_for_unmatched_with_candidates():
    edited = TENAYA_HTML.replace("Tenaya Therapeutics<BR/>", "Other Sponsor Co<BR/>")
    edited = edited.replace("Tenaya Therapeutics, Inc.<BR/>", "Other Sponsor Co<BR/>")
    assert "Tenaya" not in edited

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="<html></html>")
        return httpx.Response(200, text=edited)

    client = _make_client(handler)
    result = fda_orphan.designations_for("Tenaya Therapeutics, Inc.", client=client)

    assert result.status == "ok"
    assert result.data["match"] == "unmatched"
    assert result.data["rows"] == []
    assert result.data["candidates"] == ["Other Sponsor Co"]


def test_designations_for_no_hits_from_real_zero_hit_fixture():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="<html></html>")
        return httpx.Response(200, text=ZERO_HITS_HTML)

    client = _make_client(handler)
    result = fda_orphan.designations_for("Tenaya Therapeutics, Inc.", client=client)

    assert result.status == "ok"
    assert result.data["match"] == "no_hits"
    assert result.data["rows"] == []
    assert result.data["candidates"] == []


def test_designations_for_failed_propagates_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("boom", request=request)

    client = _make_client(handler)
    result = fda_orphan.designations_for("Tenaya Therapeutics, Inc.", client=client)

    assert result.status == "failed"
    assert result.data == {"match": None, "rows": [], "candidates": [], "query": "Tenaya"}
    assert result.error is not None


def test_designations_for_empty_query_makes_no_request():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, text="<html></html>")

    client = _make_client(handler)
    result = fda_orphan.designations_for("Inc.", client=client)

    assert result.status == "failed"
    assert result.data == {"match": None, "rows": [], "candidates": [], "query": ""}
    assert result.error["error"] == "empty_query"
    assert calls == []
