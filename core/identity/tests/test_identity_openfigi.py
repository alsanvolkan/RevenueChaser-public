"""OpenFIGI adapter: batching, keyed vs keyless, share classes, rate limits, failures."""
from __future__ import annotations

import httpx

from RevenueChaser.core.identity.sources import openfigi
from RevenueChaser.core.identity.tests.helpers import FakeResp, fixture_json

TICKERS = ["PDI", "DNP", "ARCC", "OZK", "TOWN", "LAZR", "ASML", "AZN", "AAPL", "GOOGL", "BRK-B"]


def figi_handler(call):
    """Answer each job from R1's recorded mapping (BRK/B is stored under BRK-B)."""
    mapping = fixture_json("openfigi_mapping.json")
    out = []
    for job in call["json"]:
        t = job["idValue"].replace("/", "-")
        out.append(mapping.get(t) or {"warning": "No identifier found."})
    return out


def test_happy_path_keyless_small_batches(http):
    http.add("/v3/mapping", figi_handler)
    sleeps = []
    r = openfigi.security_types(TICKERS, sleep=sleeps.append)
    assert r.source == "openfigi" and r.status == "ok"
    by = {x["ticker"]: x for x in r.rows}
    assert set(by) == set(TICKERS)
    assert by["PDI"]["security_type"] == "Closed-End Fund" and by["PDI"]["security_type2"] == "Mutual Fund"
    assert by["LAZR"]["security_type"] == "ETP"
    assert by["ASML"]["security_type"] == "NY Reg Shrs" and by["ASML"]["security_type2"] == "Depositary Receipt"
    assert by["AAPL"]["market_sector"] == "Equity" and by["AAPL"]["name"] == "APPLE INC"
    assert http.calls[0]["url"] == "https://api.openfigi.com/v3/mapping"


def test_the_composite_and_share_class_figi_the_exchange_and_the_description_reach_the_row(http):
    http.add("/v3/mapping", figi_handler)
    r = openfigi.security_types(["AAPL", "BRK-B"], sleep=lambda s: None)
    by = {x["ticker"]: x for x in r.rows}
    assert by["AAPL"]["figi"] == "BBG000B9XRY4"
    assert by["AAPL"]["composite_figi"] == "BBG000B9XRY4"
    assert by["AAPL"]["share_class_figi"] == "BBG001S5N8V8"
    assert by["AAPL"]["exch_code"] == "US"
    assert by["AAPL"]["security_description"] == "AAPL"
    mapping = fixture_json("openfigi_mapping.json")["BRK-B"]["data"][0]
    assert by["BRK-B"]["composite_figi"] == mapping["compositeFIGI"]
    assert by["BRK-B"]["share_class_figi"] == mapping["shareClassFIGI"]
    assert by["BRK-B"]["security_description"] == mapping["securityDescription"]


def test_an_entry_without_the_extra_fields_gives_none_for_them(http):
    http.add("/v3/mapping", lambda call: [{"data": [{"figi": "BBG000X", "ticker": "AAA", "exchCode": "US",
                                                     "securityType": "Common Stock"}]}] * len(call["json"]))
    row = openfigi.security_types(["AAA"], sleep=lambda s: None).rows[0]
    assert row["status"] == "ok" and row["figi"] == "BBG000X" and row["security_type"] == "Common Stock"
    assert (row["composite_figi"], row["share_class_figi"], row["security_description"]) == (None, None, None)
    assert row["exch_code"] == "US"


def test_share_class_ticker_is_sent_with_a_slash_and_mapped_back(http):
    http.add("/v3/mapping", figi_handler)
    r = openfigi.security_types(["BRK-B"], sleep=lambda s: None)
    job = http.calls[0]["json"][0]
    assert job == {"idType": "TICKER", "idValue": "BRK/B", "exchCode": "US"}
    assert r.rows[0]["ticker"] == "BRK-B" and r.rows[0]["security_type"] == "Common Stock"


def test_unmatched_ticker_is_a_no_match_row(http):
    http.add("/v3/mapping", figi_handler)
    r = openfigi.security_types(["ZZZZ"], sleep=lambda s: None)
    assert r.status == "ok" and r.rows[0]["status"] == "no_match"
    assert r.rows[0]["security_type"] is None


def test_batch_size_is_100_with_a_key_and_header_is_sent(http, monkeypatch):
    monkeypatch.setenv("OPENFIGI_API_KEY", "figi-test-key")
    http.add("/v3/mapping", lambda call: [{"warning": "none"}] * len(call["json"]))
    openfigi.security_types([f"T{i}" for i in range(250)], sleep=lambda s: None)
    sizes = [len(c["json"]) for c in http.calls]
    assert sizes == [100, 100, 50] and max(sizes) <= 100
    assert all(c["headers"].get("X-OPENFIGI-APIKEY") == "figi-test-key" for c in http.calls)


def test_batch_size_is_10_without_a_key_and_no_key_header(http):
    http.add("/v3/mapping", lambda call: [{"warning": "none"}] * len(call["json"]))
    openfigi.security_types([f"T{i}" for i in range(25)], sleep=lambda s: None)
    sizes = [len(c["json"]) for c in http.calls]
    assert sizes == [10, 10, 5]
    assert all("X-OPENFIGI-APIKEY" not in c["headers"] for c in http.calls)


def test_keyless_requests_are_paced_slower_than_keyed(http, monkeypatch):
    http.add("/v3/mapping", lambda call: [{"warning": "none"}] * len(call["json"]))
    keyless = []
    openfigi.security_types([f"T{i}" for i in range(30)], sleep=keyless.append)
    monkeypatch.setenv("OPENFIGI_API_KEY", "figi-test-key")
    keyed = []
    openfigi.security_types([f"T{i}" for i in range(30)], sleep=keyed.append)
    assert sum(keyless) >= 2 * 2.4 and sum(keyed) < sum(keyless)   # 25 req/min without a key


def test_429_waits_for_ratelimit_reset_then_retries(http):
    state = {"n": 0}

    def handler(call):
        state["n"] += 1
        if state["n"] == 1:
            return FakeResp({"error": "rate"}, 429, {"ratelimit-reset": "7"})
        return figi_handler(call)

    http.add("/v3/mapping", handler)
    sleeps = []
    r = openfigi.security_types(["AAPL"], sleep=sleeps.append)
    assert r.status == "ok" and r.rows[0]["security_type"] == "Common Stock"
    assert 8 in sleeps and state["n"] == 2


def test_low_ratelimit_remaining_makes_it_wait(http):
    http.add("/v3/mapping", lambda call: FakeResp(
        [{"warning": "none"}] * len(call["json"]), 200,
        {"ratelimit-remaining": "1", "ratelimit-reset": "5"}))
    sleeps = []
    openfigi.security_types([f"T{i}" for i in range(20)], sleep=sleeps.append)
    assert 6 in sleeps


def test_persistent_429_is_an_error(http):
    http.add("/v3/mapping", {"error": "rate"}, status=429, headers={"ratelimit-reset": "1"})
    r = openfigi.security_types(["AAPL"], sleep=lambda s: None)
    assert r.status == "error" and "429" in r.error


def test_5xx_is_an_error_not_a_raise_and_keeps_earlier_batches(http):
    state = {"n": 0}

    def handler(call):
        state["n"] += 1
        return figi_handler(call) if state["n"] == 1 else FakeResp({}, 503)

    http.add("/v3/mapping", handler)
    r = openfigi.security_types(TICKERS, sleep=lambda s: None)       # keyless: 10 + 1
    assert r.status == "error" and "503" in r.error
    assert len(r.rows) == 10 and all(x["status"] in ("ok", "no_match") for x in r.rows)


def test_timeout_is_an_error(http):
    http.add("/v3/mapping", httpx.ReadTimeout("slow"))
    r = openfigi.security_types(["AAPL"], sleep=lambda s: None)
    assert r.status == "error" and "ReadTimeout" in r.error and r.rows == []


def test_key_never_appears_in_an_error(http, monkeypatch):
    monkeypatch.setenv("OPENFIGI_API_KEY", "figi-test-key")
    http.add("/v3/mapping", httpx.ConnectError("failed for figi-test-key"))
    r = openfigi.security_types(["AAPL"], sleep=lambda s: None)
    assert r.status == "error" and "figi-test-key" not in r.error


def test_base_url_env_override(http, monkeypatch):
    monkeypatch.setenv("RC_OPENFIGI_BASE", "http://127.0.0.1:9/figi/")
    http.add("/v3/mapping", figi_handler)
    openfigi.security_types(["AAPL"], sleep=lambda s: None)
    assert http.calls[0]["url"] == "http://127.0.0.1:9/figi/v3/mapping"


def test_no_tickers_is_ok_and_makes_no_call(http):
    r = openfigi.security_types([], sleep=lambda s: None)
    assert r.status == "ok" and r.rows == [] and not http.calls


def test_prefers_the_us_listing_matching_the_ticker(http):
    http.add("/v3/mapping", lambda call: [{"data": [
        {"ticker": "ABC", "exchCode": "LN", "securityType": "ADR", "securityType2": "Depositary Receipt",
         "marketSector": "Equity", "name": "ABC LN"},
        {"ticker": "ABC", "exchCode": "US", "securityType": "Common Stock", "securityType2": "Common Stock",
         "marketSector": "Equity", "name": "ABC US"}]}])
    r = openfigi.security_types(["ABC"], sleep=lambda s: None)
    assert r.rows[0]["security_type"] == "Common Stock" and r.rows[0]["name"] == "ABC US"


def _answer(by_symbol):
    """Answer each job from ``{openfigi_symbol: (securityType, securityType2)}``."""
    def handler(call):
        out = []
        for job in call["json"]:
            hit = by_symbol.get(job["idValue"])
            out.append({"data": [{"ticker": job["idValue"], "exchCode": "US", "securityType": hit[0],
                                  "securityType2": hit[1], "marketSector": "Equity", "name": "X"}]}
                       if hit else {"warning": "No identifier found."})
        return out
    return handler


def test_unit_and_warrant_suffixes_use_openfigi_spelling(http):
    """Measured 2026-10-01: OpenFIGI knows units as ``/U`` and warrants as ``/WS``; the
    plain ``-`` -> ``/`` swap (``AAC/UN``, ``BESS/WT``) matched none of them."""
    http.add("/v3/mapping", _answer({"AAC/U": ("Unit", "Unit"), "BESS/WS": ("Equity WRT", "Warrant")}))
    r = openfigi.security_types(["AAC-UN", "BESS-WT"], sleep=lambda s: None)
    sent = sorted(j["idValue"] for j in http.calls[0]["json"])
    assert sent == ["AAC/U", "BESS/WS"]
    by = {x["ticker"]: x for x in r.rows}
    assert by["AAC-UN"]["security_type"] == "Unit"
    assert by["BESS-WT"]["security_type2"] == "Warrant"


def test_other_tickers_keep_the_plain_slash_spelling(http):
    http.add("/v3/mapping", _answer({}))
    openfigi.security_types(["BRK-B", "WFC-PA", "AAPL"], sleep=lambda s: None)
    assert sorted(j["idValue"] for j in http.calls[0]["json"]) == ["AAPL", "BRK/B", "WFC/PA"]
