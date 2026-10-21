"""B5 (DF100, #371): every UW endpoint method reads through ``get_result`` and has a ``*_result`` twin.

Before B5, 36 endpoint methods called ``self._get``, which answers ``{}`` on a final failure, so a failed
call read as an empty market. Now:

* no ``UWClient`` method calls ``self._get`` (only the wrapper itself remains, for old outside callers);
* each endpoint method has a ``*_result`` method returning a ``ProviderResult``: ``ok`` with the shaped
  data (possibly empty), ``failed`` with the same empty placeholder the plain method returns, or
  ``partial`` for the methods that make several requests and lost some of them;
* the plain methods keep their old return values, so no caller changes in this slice.
"""
from __future__ import annotations

import ast
import inspect
import textwrap
from datetime import date

import pytest

from RevenueChaser.core.providers import unusual_whales as uwmod
from RevenueChaser.core.providers.result import ProviderResult
from RevenueChaser.core.providers.unusual_whales import UWClient

D = date(2026, 10, 9)

# (plain method, its *_result twin, args, kwargs) for every endpoint method that made a request via _get.
METHODS = [
    ("calendar_day", "calendar_day_result", (D,), {}),
    ("earnings_rows", "earnings_rows_result", ("AAPL",), {}),
    ("stock_earnings", "stock_earnings_result", ("AAPL",), {}),
    ("fundamentals", "fundamentals_result", ("AAPL",), {}),
    ("financials", "financials_result", ("AAPL",), {}),
    ("income_statements", "income_statements_result", ("AAPL",), {"report_type": "annual"}),
    ("balance_sheets", "balance_sheets_result", ("AAPL",), {}),
    ("cash_flows", "cash_flows_result", ("AAPL",), {}),
    ("flow_recent", "flow_recent_result", ("AAPL",), {}),
    ("dark_pool", "dark_pool_result", ("AAPL",), {"limit": 50}),
    ("greeks", "greeks_result", ("AAPL",), {}),
    ("gex_levels", "gex_levels_result", ("AAPL",), {"date": "2026-10-09"}),
    ("term_structure", "term_structure_result", ("AAPL",), {}),
    ("greek_exposure", "greek_exposure_result", ("AAPL",), {"timeframe": "1M"}),
    ("greek_exposure_by_strike", "greek_exposure_by_strike_result", ("AAPL",), {}),
    ("greek_exposure_by_expiry", "greek_exposure_by_expiry_result", ("AAPL",), {}),
    ("spot_exposures_by_strike", "spot_exposures_by_strike_result", ("AAPL",), {"min_strike": 1.0}),
    ("oi_per_expiry", "oi_per_expiry_result", ("AAPL",), {}),
    ("flow_per_expiry", "flow_per_expiry_result", ("AAPL",), {}),
    ("iv_rank", "iv_rank_result", ("AAPL",), {}),
    ("market_oi_change", "market_oi_change_result", (), {"limit": 10}),
    ("option_trades", "option_trades_result", (), {"ticker": "AAPL", "min_dte": 365}),
    ("flow_alerts", "flow_alerts_result", (), {"min_premium": 1e6}),
    ("expiry_breakdown", "expiry_breakdown_result", ("AAPL",), {}),
    ("short_interest", "short_interest_result", ("AAPL",), {}),
    ("short_screener", "short_screener_result", (["AAPL", "MSFT"],), {}),
    ("insider", "insider_result", ("AAPL",), {}),
    ("etf_holdings", "etf_holdings_result", ("XLK",), {}),
    ("etf_ohlc", "etf_ohlc_result", ("XLK",), {"days": 2}),
    ("ticker_info", "ticker_info_result", ("AAPL",), {}),
    ("options_volume", "options_volume_result", ("AAPL",), {}),
    ("calendar_day_raw", "calendar_day_raw_result", (D, "premarket"), {}),
    ("etf_flow", "etf_flow_result", ("XLK",), {}),
    ("screener_batch", "screener_batch_result", (["AAPL", "MSFT"],), {"date": "2026-10-09"}),
    ("screener_stocks", "screener_stocks_result", (), {"min_marketcap": 3e8}),
    ("raw_endpoint", "raw_endpoint_result", ("/market/market-tide",), {}),
]

# These two hand back the decoded body unshaped.
UNSHAPED = {"greeks_result", "raw_endpoint_result"}

ROWS = {"data": [
    {"ticker": "AAPL", "symbol": "AAPL", "weight": "2", "updated": "2026-10-09", "notional": "5",
     "date": "2026-10-09", "market_time": "r", "close": 1.0},
    {"ticker": "MSFT", "symbol": "MSFT", "weight": "1", "updated": "2026-10-08", "notional": "3",
     "date": "2026-10-08", "market_time": "r", "close": 2.0},
]}


def _client(answer) -> tuple[UWClient, list]:
    """A client whose ``get_result`` answers ``answer(ep, params)``; records each request."""
    c = UWClient(key="k", sleep=lambda s: None)
    calls: list[tuple[str, dict]] = []

    def fake(ep, params=None):
        calls.append((ep, dict(params or {})))
        return answer(ep, params)

    c.get_result = fake  # type: ignore[method-assign]
    return c, calls


def _ok(payload):
    return lambda ep, params=None: ProviderResult("ok", payload, None, ep)


def _failed(status=500):
    return lambda ep, params=None: ProviderResult(
        "failed", {}, {"endpoint": ep, "status": status, "error": f"HTTP {status}", "attempts": 3}, ep)


# ── no endpoint method reads through the {}-on-failure wrapper ───────────────────────────

def test_no_uwclient_method_calls_the_compatibility_wrapper():
    tree = ast.parse(textwrap.dedent(inspect.getsource(UWClient)))
    callers = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef) or fn.name == "_get":
            continue
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "_get" and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "self"):
                callers.append(fn.name)
    assert callers == []


def test_every_endpoint_method_has_a_result_twin():
    missing = [twin for _, twin, _, _ in METHODS if not callable(getattr(UWClient, twin, None))]
    assert missing == []


# ── ok / failed for every method ────────────────────────────────────────────────────────

@pytest.mark.parametrize("plain,twin,args,kwargs", METHODS, ids=[m[0] for m in METHODS])
def test_the_result_twin_is_ok_with_the_same_data_the_plain_method_returns(plain, twin, args, kwargs):
    c, calls = _client(_ok(ROWS))
    res = getattr(c, twin)(*args, **kwargs)
    assert isinstance(res, ProviderResult) and res.status == "ok" and res.error is None
    n = len(calls)
    assert res.data == getattr(c, plain)(*args, **kwargs)
    assert len(calls) == 2 * n                               # the plain method makes the same requests


@pytest.mark.parametrize("plain,twin,args,kwargs", METHODS, ids=[m[0] for m in METHODS])
def test_the_result_twin_is_failed_and_the_plain_method_keeps_its_old_placeholder(plain, twin, args, kwargs):
    c, _ = _client(_failed(500))
    res = getattr(c, twin)(*args, **kwargs)
    assert res.status == "failed"
    assert res.error and res.error.get("status") == 500       # the cause travels with the result
    placeholder = getattr(c, plain)(*args, **kwargs)
    assert res.data == placeholder
    assert not placeholder                                    # [] / {} as before B5: no caller changes


@pytest.mark.parametrize("plain,twin,args,kwargs", METHODS, ids=[m[0] for m in METHODS])
def test_an_empty_answer_is_ok_not_failed(plain, twin, args, kwargs):
    c, _ = _client(_ok({"data": []}))
    res = getattr(c, twin)(*args, **kwargs)
    assert res.status == "ok"                                 # a quiet market is a real, empty answer
    assert res.data == ({"data": []} if twin in UNSHAPED else type(res.data)())


def test_some_shaped_values_are_unchanged():
    c, _ = _client(_ok(ROWS))
    assert [r["ticker"] for r in c.etf_holdings_result("XLK").data] == ["AAPL", "MSFT"]
    assert set(c.screener_batch_result(["AAPL", "MSFT"]).data) == {"AAPL", "MSFT"}
    c, _ = _client(_ok({"data": [{"ticker": "B", "weight": "1", "updated": "2026-10-01"},
                                 {"ticker": "A", "weight": "3", "updated": "2026-10-01"},
                                 {"ticker": "B", "weight": "5", "updated": "2026-10-09"}]}))
    assert [(r["ticker"], r["weight"]) for r in c.etf_holdings_result("XLK").data] == [("B", "5"), ("A", "3")]
    # each screener keeps its own key order: short_screener reads symbol first, screener_batch ticker first
    c, _ = _client(_ok({"data": [{"symbol": "SYM", "ticker": "TKR"}]}))
    assert list(c.short_screener_result(["X"]).data) == ["SYM"]
    assert list(c.screener_batch_result(["X"]).data) == ["TKR"]
    c, _ = _client(_ok({"data": {"symbol": "QXO", "si_float": "0.05"}}))
    assert c.short_interest_result("QXO").data["si_float"] == "0.05"
    c, _ = _client(_ok({"data": {"income_statements": []}}))
    assert c.financials_result("QXO").data == {"income_statements": []}


def test_a_failed_auth_call_also_marks_the_client():
    """The twins read through the real ``get_result``, so a 401 still sets ``auth_error`` and health."""
    class _R:
        status_code, text, headers = 401, "no", {}

    c = UWClient(key="k", sleep=lambda s: None)
    c._client.get = lambda url, params=None: _R()  # type: ignore[method-assign]
    res = c.ticker_info_result("AAPL")
    assert res.status == "failed" and res.error["status"] == 401
    assert c.auth_error and c.auth_error["status"] == 401
    assert c.health()["failed"] == 1


# ── methods that make several requests: partial ──────────────────────────────────────────

def _by_request(fail_when):
    def answer(ep, params=None):
        if fail_when(ep, params or {}):
            return _failed(503)(ep, params)
        return _ok(ROWS)(ep, params)
    return answer


def test_calendar_day_is_partial_when_one_session_fails():
    c, calls = _client(_by_request(lambda ep, p: ep.endswith("/afterhours")))
    res = c.calendar_day_result(D)
    assert res.status == "partial" and len(calls) == 2
    assert {e["earnings_time"] for e in res.data} == {"BMO"}      # the premarket events are kept
    assert res.error["session"] == "afterhours" and res.error["status"] == 503
    assert c.calendar_day(D) == res.data


def test_calendar_day_is_failed_when_both_sessions_fail():
    c, _ = _client(_failed())
    res = c.calendar_day_result(D)
    assert res.status == "failed" and res.data == []


def _tickers(n):
    return [f"T{i}" for i in range(n)]


@pytest.mark.parametrize("twin,param", [("screener_batch_result", "ticker"),
                                        ("short_screener_result", "tickers")])
def test_a_batch_is_partial_when_one_chunk_fails(twin, param):
    c, calls = _client(_by_request(lambda ep, p: p[param].startswith("T100")))
    res = getattr(c, twin)(_tickers(250))
    assert len(calls) == 3 and res.status == "partial"
    assert set(res.data) == {"AAPL", "MSFT"}                       # the other chunks' rows are kept
    assert res.error["chunk"] == 1 and res.error["status"] == 503


@pytest.mark.parametrize("twin", ["screener_batch_result", "short_screener_result"])
def test_a_batch_is_failed_when_every_chunk_fails(twin):
    c, calls = _client(_failed())
    res = getattr(c, twin)(_tickers(150))
    assert len(calls) == 2 and res.status == "failed" and res.data == {}


@pytest.mark.parametrize("twin", ["screener_batch_result", "short_screener_result"])
def test_no_tickers_is_ok_and_makes_no_request(twin):
    c, calls = _client(_failed())
    res = getattr(c, twin)([])
    assert res.status == "ok" and res.data == {} and calls == []


def test_the_module_still_exposes_the_compatibility_wrapper():
    c, _ = _client(_failed())
    assert c._get("/x") == {}
    assert uwmod.UWClient._get.__doc__
