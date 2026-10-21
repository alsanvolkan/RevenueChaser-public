"""FDIC BankFind adapter: name-match variants, holding-company filter, failures."""
from __future__ import annotations

import httpx

from RevenueChaser.core.identity.sources import fdic
from RevenueChaser.core.identity.tests.helpers import FakeResp, fixture_json


def route(http):
    """Answer by query: TowneBank only for the spaces-removed variant, like the real API."""
    towne, ozk = fixture_json("fdic_towne.json"), fixture_json("fdic_ozk.json")

    def handler(call):
        q = call["params"]["filters"]
        if 'NAME:"TowneBank"' in q:
            return towne
        if "OZK" in q.upper() and "NAME" in q:
            return ozk
        return {"data": []}

    http.add("/banks/institutions", handler)


def test_match_uses_the_spaces_removed_variant_and_returns_cert_and_holding_company(http):
    route(http)
    r = fdic.match_banks({"TOWN": "Towne Bank", "OZK": "Bank OZK"}, sleep=lambda s: None)
    assert r.source == "fdic" and r.status == "ok"
    by = {x["ticker"]: x for x in r.rows}
    assert by["TOWN"]["cert"] == 35095 and by["TOWN"]["name"] == "TowneBank"
    assert by["TOWN"]["status"] == "ok" and by["TOWN"]["score"] >= 0.6
    assert by["TOWN"]["holding_company"] == ""
    assert by["OZK"]["cert"] == 110 and by["OZK"]["name"] == "Bank OZK"


def test_the_larger_bank_wins_a_tie_on_name_score(http):
    route(http)                                   # fixture also has "Towne Bank of Arizona", $100k assets
    by = {x["ticker"]: x for x in fdic.match_banks({"TOWN": "Towne Bank"}, sleep=lambda s: None).rows}
    assert by["TOWN"]["cert"] == 35095


def test_query_variants_match_r1(http):
    route(http)
    fdic.match_banks({"TOWN": "Towne Bank"}, sleep=lambda s: None)
    filters = [c["params"]["filters"] for c in http.calls]
    assert 'NAME:"Towne Bank" AND ACTIVE:1' in filters              # exact
    assert 'NAMEHCR:"TOWNE BANK*" AND ACTIVE:1' in filters          # holding-company filter
    assert 'NAME:"TowneBank" AND ACTIVE:1' in filters               # spaces removed
    assert "NAME:Towne*Bank* AND ACTIVE:1" in filters               # token wildcard
    assert all(c["params"]["limit"] == 25 and "CERT" in c["params"]["fields"] for c in http.calls)


def test_corporate_suffixes_are_stripped_before_querying(http):
    route(http)
    fdic.match_banks({"OZK": "Bank OZK Holdings Inc."}, sleep=lambda s: None)
    assert 'NAME:"Bank OZK" AND ACTIVE:1' in [c["params"]["filters"] for c in http.calls]


def test_no_match_is_a_no_match_row(http):
    http.add("/banks/institutions", {"data": []})
    r = fdic.match_banks({"ACME": "Acme Widgets"}, sleep=lambda s: None)
    assert r.status == "ok" and r.rows[0]["status"] == "no_match" and r.rows[0]["cert"] is None


def test_a_weak_name_match_is_rejected(http):
    http.add("/banks/institutions", {"data": [{"data": {"NAME": "Zzyzx Savings", "CERT": 7, "ASSET": 5}}]})
    r = fdic.match_banks({"TOWN": "Towne Bank"}, sleep=lambda s: None)
    assert r.rows[0]["status"] == "no_match"


def test_5xx_and_timeout_are_error_rows_not_raises(http):
    http.add("/banks/institutions", {}, status=503)
    r = fdic.match_banks({"TOWN": "Towne Bank"}, sleep=lambda s: None)
    assert r.status == "error" and r.rows[0]["status"] == "error"
    http.routes.clear()
    http.add("/banks/institutions", httpx.ReadTimeout("slow"))
    r = fdic.match_banks({"TOWN": "Towne Bank"}, sleep=lambda s: None)
    assert r.status == "error" and "ReadTimeout" in r.rows[0]["error"]


def test_partial_outage_with_a_match_still_counts_as_a_match(http):
    state = {"n": 0}
    towne = fixture_json("fdic_towne.json")

    def handler(call):
        state["n"] += 1
        return FakeResp({}, 503) if state["n"] == 1 else towne

    http.add("/banks/institutions", handler)
    r = fdic.match_banks({"TOWN": "Towne Bank"}, sleep=lambda s: None)
    assert r.rows[0]["status"] == "ok" and r.rows[0]["cert"] == 35095


def test_requests_are_paced(http):
    route(http)
    sleeps = []
    fdic.match_banks({"TOWN": "Towne Bank"}, sleep=sleeps.append)
    assert sleeps and all(s >= 0.3 for s in sleeps)


def test_base_url_env_override(http, monkeypatch):
    monkeypatch.setenv("RC_FDIC_BASE", "http://127.0.0.1:9/fdic/")
    route(http)
    fdic.match_banks({"OZK": "Bank OZK"}, sleep=lambda s: None)
    assert http.calls[0]["url"] == "http://127.0.0.1:9/fdic/banks/institutions"


def test_default_base_is_the_real_fdic_host(http):
    route(http)
    fdic.match_banks({"OZK": "Bank OZK"}, sleep=lambda s: None)
    assert http.calls[0]["url"] == "https://api.fdic.gov/banks/institutions"


def test_no_names_is_ok_and_makes_no_call(http):
    r = fdic.match_banks({}, sleep=lambda s: None)
    assert r.status == "ok" and r.rows == [] and not http.calls


def test_the_institutions_api_is_read_with_the_redirect_followed(monkeypatch):
    # api.fdic.gov answers a redirect, and httpx follows none by default: the fake keeps that default, so this is
    # red until the adapter asks for the follow (the parser would otherwise see the redirect body, #447's bug).
    towne = fixture_json("fdic_towne.json")
    seen = []

    def get(url, params=None, timeout=None, follow_redirects=False, **kwargs):
        seen.append(follow_redirects)
        return FakeResp(towne if 'NAME:"TowneBank"' in (params or {}).get("filters", "") else {"data": []})

    monkeypatch.setattr("httpx.get", get)
    by = {x["ticker"]: x for x in fdic.match_banks({"TOWN": "Towne Bank"}, sleep=lambda s: None).rows}
    assert by["TOWN"]["cert"] == 35095
    assert seen, "the adapter sent no query"
    assert set(seen) == {True}                        # every query variant, not only the first

