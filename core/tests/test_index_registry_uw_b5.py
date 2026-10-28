"""B5 (DF100, #371): the index lists read UW through ``UWClient.get_result``, and a failed read is an error.

``_uw_get`` was a bare ``httpx.get`` with the import-time ``UW_API_KEY``: every exception was logged at debug
and answered ``[]``, with no retry, breaker, ``auth_error`` or health record. A rejected key therefore read as
"sp500 below floor (0 < 400)" in the weekly index-lists job, with no cause. Now ``_uw_get`` goes through the
client and raises ``IndexFetchError`` naming the failure, so the step's row says ``error (... HTTP 401 ...)``
and the stored list is kept, as before. Fake clients only; no request leaves the process.
"""
from __future__ import annotations

import ast
import inspect

import httpx
import pytest

from RevenueChaser.core import index_registry as ir
from RevenueChaser.core import index_lists as il
from RevenueChaser.core import rawstore
from RevenueChaser.core.providers.result import ProviderResult


class _FakeUW:
    """Stands in for ``UWClient``: answers ``get_result`` / ``etf_holdings_result`` from ``answers``."""

    def __init__(self, answers: dict, key: str = "k"):
        self.key = key
        self.answers = answers
        self.calls: list[tuple[str, dict | None]] = []
        self.closed = False

    @property
    def active(self) -> bool:
        return bool(self.key)

    def get_result(self, ep, params=None):
        self.calls.append((ep, params))
        return self.answers[ep]

    def etf_holdings_result(self, ticker):
        self.calls.append((f"/etfs/{ticker}/holdings", None))
        return self.answers[f"/etfs/{ticker}/holdings"]

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


SP = "/screener/stocks"
NQ = "/stock-directory/ticker-exchanges"
DIA = "/etfs/DIA/holdings"


def _ok(data, ep=""):
    return ProviderResult("ok", data, None, ep)


def _failed(status, ep=""):
    return ProviderResult("failed", {}, {"endpoint": ep, "status": status, "error": f"HTTP {status}",
                                         "attempts": 1}, ep)


@pytest.fixture
def fake_uw(monkeypatch):
    clients: list[_FakeUW] = []
    answers: dict = {}

    def make(key="k"):
        c = _FakeUW(answers, key=key)
        clients.append(c)
        return c

    monkeypatch.setattr(il, "_uw_client", make)

    def no_network(*a, **k):
        raise AssertionError("no bare httpx request: the index lists read through UWClient")

    monkeypatch.setattr(httpx, "get", no_network)
    return answers, clients, monkeypatch


def test_the_module_makes_no_bare_httpx_request():
    tree = ast.parse(inspect.getsource(ir))
    bare = [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "get"
            and isinstance(n.value, ast.Name) and n.value.id == "httpx"]
    assert bare == []


def test_uw_get_returns_the_body_and_closes_its_client(fake_uw):
    answers, clients, _ = fake_uw
    answers[SP] = _ok({"data": [{"ticker": "AAPL"}]})
    assert il._uw_get(SP, {"is_s_p_500": "true", "limit": 500}) == {"data": [{"ticker": "AAPL"}]}
    assert clients[0].calls == [(SP, {"is_s_p_500": "true", "limit": 500})]
    assert clients[0].closed


@pytest.mark.parametrize("status", [401, 403, 429, 500, None])
def test_uw_get_raises_with_the_cause_when_the_request_fails(fake_uw, status):
    answers, clients, _ = fake_uw
    answers[SP] = _failed(status, SP) if status else ProviderResult(
        "failed", {}, {"endpoint": SP, "status": None, "error": "ConnectError", "attempts": 3}, SP)
    with pytest.raises(il.IndexFetchError) as exc:
        il._uw_get(SP)
    msg = str(exc.value)
    assert SP in msg and (f"HTTP {status}" if status else "ConnectError") in msg
    assert clients[0].closed


def test_uw_get_with_no_key_raises_and_sends_nothing(fake_uw):
    _, clients, monkeypatch = fake_uw
    monkeypatch.setattr(il, "_uw_client", lambda: clients.append(_FakeUW({}, key="")) or clients[-1])
    with pytest.raises(il.IndexFetchError) as exc:
        il._uw_get(SP)
    assert "UNUSUAL_WHALES_API_KEY" in str(exc.value) or "key" in str(exc.value).lower()
    assert clients[0].calls == []


def test_an_empty_answer_is_an_empty_list_not_an_error(fake_uw):
    answers, _, _ = fake_uw
    answers[SP] = _ok({"data": []})
    answers[NQ] = _ok([])
    assert il.fetch_sp500() == set()
    assert il.fetch_nasdaq_composite() == set()


def test_each_uw_fetcher_raises_on_a_failed_read(fake_uw):
    answers, _, _ = fake_uw
    answers[SP], answers[NQ], answers[DIA] = _failed(401, SP), _failed(500, NQ), _failed(503, DIA)
    for fetch, cause in ((il.fetch_sp500, "HTTP 401"), (il.fetch_nasdaq_composite, "HTTP 500"),
                         (il.fetch_dow, "HTTP 503")):
        with pytest.raises(il.IndexFetchError, match=cause):
            fetch()


def test_fetchers_still_parse_a_good_answer(fake_uw):
    answers, _, _ = fake_uw
    answers[SP] = _ok({"data": [{"ticker": "aapl"}, {"ticker": "MSFT"}, {"ticker": ""}]})
    answers[NQ] = _ok({"data": [{"ticker": "msft", "exchange": "NASDAQ"}, {"ticker": "IBM", "exchange": "nyse"}]})
    answers[DIA] = _ok([{"ticker": "ibm"}, {"ticker": None}])
    assert il.fetch_sp500() == {"AAPL", "MSFT"}
    assert il.fetch_nasdaq_composite() == {"MSFT"}
    assert il.fetch_dow() == {"IBM"}


def test_the_weekly_step_reports_a_rejected_key_as_an_error_and_keeps_the_stored_list(rawstore_tmp_db, fake_uw):
    answers, _, _ = fake_uw
    il.save_index("sp500", {f"S{i:03d}" for i in range(450)})
    answers[SP], answers[NQ], answers[DIA] = _failed(401, SP), _failed(401, NQ), _failed(401, DIA)
    res = il.refresh_lists(run_id="r1")
    for slug in ("sp500", "dow", "nasdaq"):
        row = res["slugs"][slug]
        assert row["status"] == "error", row                     # was "below_floor" with 0 members
        assert "HTTP 401" in row["error"]
    assert not il.lists_ok(res)
    assert "sp500 error (" in il.summary_line(res) and "HTTP 401" in il.summary_line(res)
    assert len(il.stored_index("sp500")) == 450                     # nothing written over the stored list
    assert rawstore.query("SELECT count(*) AS n FROM index_constituents_changes WHERE run_id = 'r1'")[0]["n"] == 0


def test_a_short_but_successful_answer_is_still_below_floor(rawstore_tmp_db, fake_uw):
    answers, _, _ = fake_uw
    answers[SP] = _ok({"data": [{"ticker": "AAPL"}]})
    answers[NQ] = _ok({"data": []})
    answers[DIA] = _ok([])
    res = il.refresh_lists(run_id="r2")
    assert res["slugs"]["sp500"]["status"] == "below_floor" and res["slugs"]["sp500"]["members"] == 1


def test_the_cache_first_read_keeps_the_cache_when_the_fetch_fails(rawstore_tmp_db, fake_uw):
    answers, _, _ = fake_uw
    answers[SP] = _failed(500, SP)
    il.save_index("sp500", {"AAPL"})
    assert il.fetch_index("sp500", refresh=True) == {"AAPL"}


def test_the_cache_first_read_with_no_cache_is_empty_when_the_fetch_fails(rawstore_tmp_db, fake_uw):
    answers, _, _ = fake_uw
    answers[DIA] = _failed(500, DIA)
    assert il.fetch_index("dow") == set()
