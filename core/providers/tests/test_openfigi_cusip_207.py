"""The OpenFIGI CUSIP mapping client (#207 F1a), against fakes only: no request leaves the process."""
from __future__ import annotations

import pytest

from RevenueChaser.core.identity.sources import openfigi as identity_openfigi
from RevenueChaser.core.providers import openfigi
from RevenueChaser.core.providers.result import ProviderHealth

KEY = "figi-test-key-0123456789"


class _Resp:
    def __init__(self, status_code=200, body=None, headers=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _hit(ticker, figi="BBG000000001", share_class="BBG001S00001", name="CO", sec_type="Common Stock"):
    return {"data": [{"figi": figi, "ticker": ticker, "shareClassFIGI": share_class, "name": name,
                      "exchCode": "US", "securityType": sec_type, "marketSector": "Equity"}]}


NO_MATCH = {"warning": "No identifier found."}


class _Fake:
    """Answers each request from ``script`` (a list of callables ``jobs -> _Resp``), recording every call."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls: list[dict] = []

    def __call__(self, url, jobs, headers):
        self.calls.append({"url": url, "jobs": jobs, "headers": dict(headers)})
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        return step(jobs)


def _all_known(jobs):
    return _Resp(200, [_hit("T" + j["idValue"][-3:]) for j in jobs])


@pytest.fixture(autouse=True)
def _no_network_no_wait(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(openfigi, "_sleep", sleeps.append)

    def _refuse(*_a, **_k):
        raise AssertionError("a test reached the real OpenFIGI client")

    monkeypatch.setattr(openfigi, "_post", _refuse)
    monkeypatch.delenv("OPENFIGI_API_KEY", raising=False)
    monkeypatch.delenv("RC_OPENFIGI_BASE", raising=False)
    return sleeps


@pytest.fixture
def sleeps(_no_network_no_wait):
    return _no_network_no_wait


def _use(monkeypatch, fake):
    monkeypatch.setattr(openfigi, "_post", fake)
    return fake


class TestRequestShape:
    def test_one_id_cusip_job_per_cusip_on_the_us_listing_with_the_key_header(self, monkeypatch):
        fake = _use(monkeypatch, _Fake(_all_known))
        openfigi.map_cusips(["02079K305"], key=KEY)
        call = fake.calls[0]
        assert call["url"] == "https://api.openfigi.com/v3/mapping"
        assert call["jobs"] == [{"idType": "ID_CUSIP", "idValue": "02079K305", "exchCode": "US"}]
        assert call["headers"]["X-OPENFIGI-APIKEY"] == KEY

    def test_the_base_url_is_read_at_call_time(self, monkeypatch):
        fake = _use(monkeypatch, _Fake(_all_known))
        monkeypatch.setenv("RC_OPENFIGI_BASE", "http://127.0.0.1:9/figi/")
        openfigi.map_cusips(["111111111"], key=KEY)
        assert fake.calls[0]["url"] == "http://127.0.0.1:9/figi/v3/mapping"

    def test_100_jobs_per_request_and_a_gap_between_requests(self, monkeypatch, sleeps):
        fake = _use(monkeypatch, _Fake(_all_known))
        cusips = [f"{i:09d}" for i in range(250)]
        res = openfigi.map_cusips(cusips, key=KEY)
        assert [len(c["jobs"]) for c in fake.calls] == [100, 100, 50]
        assert res.requests == 3 and res.complete and len(res.answers) == 250
        assert sleeps == [openfigi.REQUEST_GAP_S] * 2

    def test_the_key_comes_from_the_environment_by_default(self, monkeypatch):
        fake = _use(monkeypatch, _Fake(_all_known))
        monkeypatch.setenv("OPENFIGI_API_KEY", KEY)
        openfigi.map_cusips(["111111111"])
        assert fake.calls[0]["headers"]["X-OPENFIGI-APIKEY"] == KEY

    def test_duplicates_and_blanks_are_asked_once(self, monkeypatch):
        fake = _use(monkeypatch, _Fake(_all_known))
        openfigi.map_cusips(["111111111", " 111111111 ", "", None], key=KEY)
        assert [j["idValue"] for j in fake.calls[0]["jobs"]] == ["111111111"]


class TestAnswers:
    def test_present_absent_and_job_errors(self, monkeypatch):
        _use(monkeypatch, _Fake(lambda jobs: _Resp(200, [
            _hit("GOOGL", figi="BBG009S39JX6", share_class="BBG009S39JY5", name="ALPHABET INC-CL A"),
            NO_MATCH,
            {"error": "Invalid idValue format."}])))
        res = openfigi.map_cusips(["02079K305", "999999999", "BADCUSIP1"], key=KEY)
        assert res.answers["02079K305"] == {
            "status": "present", "ticker": "GOOGL", "figi": "BBG009S39JX6", "share_class_figi": "BBG009S39JY5",
            "share_class": None, "name": "ALPHABET INC-CL A", "exch_code": "US", "security_type": "Common Stock",
            "fetched_at": res.answers["02079K305"]["fetched_at"]}
        assert res.answers["999999999"]["status"] == "absent"
        assert res.answers["999999999"]["ticker"] is None
        assert "BADCUSIP1" not in res.answers and res.job_errors == 1
        assert res.complete and res.unasked == []

    def test_dual_class_cusips_map_to_their_own_tickers(self, monkeypatch):
        _use(monkeypatch, _Fake(lambda jobs: _Resp(200, [_hit("GOOGL", share_class="BBG009S39JY5"),
                                                         _hit("GOOG", share_class="BBG009S3NB21")])))
        res = openfigi.map_cusips(["02079K305", "02079K107"], key=KEY)
        assert res.answers["02079K305"]["ticker"] == "GOOGL"
        assert res.answers["02079K107"]["ticker"] == "GOOG"
        assert res.answers["02079K305"]["share_class_figi"] != res.answers["02079K107"]["share_class_figi"]

    def test_the_first_entry_with_a_ticker_wins(self, monkeypatch):
        _use(monkeypatch, _Fake(lambda jobs: _Resp(200, [
            {"data": [{"figi": "F0", "ticker": None, "exchCode": "US"},
                      {"figi": "F1", "ticker": "BRK/B", "exchCode": "US"}]}])))
        res = openfigi.map_cusips(["084670702"], key=KEY)
        assert res.answers["084670702"]["figi"] == "F1"
        assert res.answers["084670702"]["ticker"] == "BRK-B"

    @pytest.mark.parametrize("theirs,ours", [("BRK/B", "BRK-B"), ("AAC/U", "AAC-UN"), ("BESS/WS", "BESS-WT"),
                                             ("AAPL", "AAPL"), ("", None), (None, None)])
    def test_tickers_come_back_in_the_apps_spelling(self, theirs, ours):
        assert openfigi.app_ticker(theirs) == ours

    @pytest.mark.parametrize("ours", ["BRK-B", "AAC-UN", "BESS-WT", "AAPL"])
    def test_the_spelling_is_the_inverse_of_the_identity_adapter(self, ours):
        assert openfigi.app_ticker(identity_openfigi.figi_symbol(ours)) == ours


class TestRateLimit:
    def test_a_429_is_waited_out_and_retried_then_mapped(self, monkeypatch, sleeps):
        fake = _use(monkeypatch, _Fake(lambda jobs: _Resp(429, None, {"ratelimit-reset": "4"}), _all_known))
        res = openfigi.map_cusips(["111111111"], key=KEY)
        assert res.complete and res.answers["111111111"]["status"] == "present"
        assert res.requests == 2 and res.rate_limited == 1 and len(fake.calls) == 2
        assert 5.0 in sleeps                     # ratelimit-reset 4 s, plus one

    def test_a_low_remaining_window_is_waited_out(self, monkeypatch, sleeps):
        _use(monkeypatch, _Fake(lambda jobs: _Resp(200, [_hit("A")], {"ratelimit-remaining": "2",
                                                                      "ratelimit-reset": "3"})))
        openfigi.map_cusips(["111111111"], key=KEY)
        assert sleeps == [4.0]

    def test_429_on_every_attempt_ends_the_run_and_counts_each_request(self, monkeypatch):
        _use(monkeypatch, _Fake(lambda jobs: _Resp(429, None, {"ratelimit-reset": "1"})))
        res = openfigi.map_cusips(["111111111"], key=KEY)
        assert res.status == "failed" and res.unasked == ["111111111"]
        assert res.requests == openfigi.MAX_ATTEMPTS == res.rate_limited
        assert "429" in res.error


class TestCap:
    def test_the_run_stops_at_the_cap_and_returns_the_unasked(self, monkeypatch):
        fake = _use(monkeypatch, _Fake(_all_known))
        cusips = [f"{i:09d}" for i in range(250)]
        res = openfigi.map_cusips(cusips, key=KEY, max_requests=2)
        assert len(fake.calls) == 2 and res.requests == 2
        assert res.status == "partial" and len(res.answers) == 200
        assert res.unasked == cusips[200:]
        assert "cap of 2" in res.error

    def test_retries_count_toward_the_cap(self, monkeypatch):
        fake = _use(monkeypatch, _Fake(lambda jobs: _Resp(429, None, {"ratelimit-reset": "0"}), _all_known))
        res = openfigi.map_cusips(["111111111"], key=KEY, max_requests=1)
        assert len(fake.calls) == 1 and res.status == "failed" and res.unasked == ["111111111"]

    def test_the_default_cap_is_avos_400(self):
        assert openfigi.MAX_REQUESTS == 400


class TestFailures:
    def test_no_key_asks_nothing(self, monkeypatch):
        fake = _use(monkeypatch, _Fake(_all_known))
        res = openfigi.map_cusips(["111111111"])
        assert res.status == "skipped" and res.unasked == ["111111111"] and fake.calls == []
        assert "OPENFIGI_API_KEY" in res.error

    def test_a_server_error_stops_the_run_with_the_answers_so_far(self, monkeypatch):
        _use(monkeypatch, _Fake(_all_known, lambda jobs: _Resp(500, None)))
        cusips = [f"{i:09d}" for i in range(150)]
        res = openfigi.map_cusips(cusips, key=KEY)
        assert res.status == "partial" and len(res.answers) == 100 and res.unasked == cusips[100:]
        assert res.error == "OpenFIGI HTTP 500"

    def test_a_wrong_sized_body_answers_nothing_of_that_batch(self, monkeypatch):
        _use(monkeypatch, _Fake(lambda jobs: _Resp(200, [_hit("A")])))
        res = openfigi.map_cusips(["111111111", "222222222"], key=KEY)
        assert res.status == "failed" and res.answers == {} and res.unasked == ["111111111", "222222222"]

    def test_an_unreadable_body_is_a_failure(self, monkeypatch):
        _use(monkeypatch, _Fake(lambda jobs: _Resp(200, ValueError("not json"))))
        assert openfigi.map_cusips(["111111111"], key=KEY).status == "failed"

    def test_a_transport_error_never_raises_and_never_carries_the_key(self, monkeypatch):
        def boom(url, jobs, headers):
            raise ConnectionError(f"proxy said no to {headers['X-OPENFIGI-APIKEY']}")
        monkeypatch.setattr(openfigi, "_post", boom)
        health = ProviderHealth("openfigi")
        res = openfigi.map_cusips(["111111111"], key=KEY, health=health)
        assert res.status == "failed" and KEY not in res.error and "<redacted>" in res.error
        assert KEY not in str(health.health())


class TestHealth:
    def test_every_request_is_counted_and_failures_named(self, monkeypatch):
        _use(monkeypatch, _Fake(lambda jobs: _Resp(429, None, {"ratelimit-reset": "0"}), _all_known))
        health = ProviderHealth("openfigi")
        openfigi.map_cusips(["111111111"], key=KEY, health=health)
        h = health.health()
        assert h["provider"] == "openfigi" and h["calls"] == 2 and h["failed"] == 1
        assert h["failures"] == [{"endpoint": "/v3/mapping", "error": "HTTP 429"}]
