"""#166: UW client plumbing that lets a caller tell "no data" from "the request failed".

Three things live here:

* every interpolated path segment is percent-encoded, so a ticker (or session, or candle size)
  can never add a path segment or a query string;
* the additive ``*_result`` methods (``etf_ohlc_result``, ``ohlc_daily_result``,
  ``flow_recent_result``, ``option_contracts_result``) return a ``ProviderResult`` whose
  ``failed`` / ``partial`` status is what the boards use to keep stored data;
* the list-returning methods keep their old contract (``[]`` on failure).
"""
from __future__ import annotations

import subprocess
import sys
from datetime import date
from pathlib import Path

import httpx
import pytest

from RevenueChaser.core.providers import unusual_whales as uwmod
from RevenueChaser.core.providers.result import ProviderResult
from RevenueChaser.core.providers.unusual_whales import UWClient

EVIL = "A/B?x=1"
EVIL_SEG = "A%2FB%3Fx%3D1"


class _Resp:
    def __init__(self, status: int, payload=None, text: str = "", headers=None):
        self.status_code = status
        self._payload = payload if payload is not None else {"data": []}
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._payload


def _scripted(*outcomes, key: str = "k") -> tuple[UWClient, list]:
    """A client whose transport answers ``outcomes`` in order (the last one repeats)."""
    c = UWClient(key=key, sleep=lambda s: None)
    seq = list(outcomes)
    calls: list[tuple[str, dict]] = []

    def _get(url, params=None):
        calls.append((url, dict(params or {})))
        out = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(out, BaseException):
            raise out
        return out

    c._client.get = _get  # type: ignore[method-assign]
    return c, calls


# ── path encoding ────────────────────────────────────────────────────────────────────────

# Every client method that puts its first argument into the URL path.
TICKER_METHODS = [
    "earnings_data", "earnings_history", "stock_earnings", "earnings_rows", "fundamentals",
    "financials", "income_statements", "balance_sheets", "cash_flows", "flow_recent", "dark_pool",
    "greeks", "gex_levels", "term_structure", "greek_exposure", "greek_exposure_by_strike",
    "greek_exposure_by_expiry", "spot_exposures_by_strike", "oi_per_expiry", "flow_per_expiry",
    "iv_rank", "expiry_breakdown", "option_contracts", "short_interest", "insider", "etf_holdings",
    "etf_ohlc", "ohlc_daily", "ohlc_latest", "ticker_info", "options_volume", "etf_flow",
    "etf_ohlc_result", "ohlc_daily_result", "flow_recent_result", "option_contracts_result",
]


def _path_of(method: str, ticker: str) -> str:
    c = UWClient(key="k")
    seen: list[str] = []

    def fake_get_result(ep, params=None):
        seen.append(ep)
        return ProviderResult("ok", {"data": []}, None, ep)

    c.get_result = fake_get_result  # type: ignore[method-assign]
    c._get = lambda ep, params=None: (seen.append(ep) or {"data": []})  # type: ignore[method-assign]
    getattr(c, method)(ticker)
    assert seen, f"{method} made no request"
    return seen[0]


@pytest.mark.parametrize("method", TICKER_METHODS)
def test_a_ticker_with_a_slash_and_a_query_is_one_encoded_path_segment(method):
    clean = _path_of(method, "AAPL")
    evil = _path_of(method, EVIL)
    assert EVIL_SEG in evil and "?" not in evil and EVIL not in evil
    assert evil.replace(EVIL_SEG, "AAPL") == clean         # same shape: one segment, nothing added


@pytest.mark.parametrize("method", TICKER_METHODS)
def test_a_dotted_ticker_is_left_alone(method):
    assert _path_of(method, "BRK.B") == _path_of(method, "AAPL").replace("AAPL", "BRK.B")


def test_the_candle_size_and_the_calendar_session_are_encoded_too():
    c = UWClient(key="k")
    seen: list[str] = []
    c.get_result = lambda ep, params=None: (seen.append(ep)  # type: ignore[method-assign]
                                            or ProviderResult("ok", {"data": []}, None, ep))
    c.etf_ohlc("SPY", days=3, candle_size="1d/../x")
    c.calendar_day_raw(date(2026, 10, 2), "pre/market")
    assert seen[0] == "/stock/SPY/ohlc/1d%2F..%2Fx"
    assert seen[1] == "/earnings/pre%2Fmarket"


def test_the_encoded_path_is_what_reaches_the_wire():
    wire: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        wire.append(request)
        return httpx.Response(200, json={"data": {}})

    c = UWClient(key="k")
    c._client = httpx.Client(transport=httpx.MockTransport(handler), headers=c.headers)
    c.ticker_info(EVIL)
    assert wire[0].url.raw_path.endswith(f"/stock/{EVIL_SEG}/info".encode())
    assert wire[0].url.query == b""


# ── the *_result methods ─────────────────────────────────────────────────────────────────

BARS = [{"date": "2026-09-29", "market_time": "r", "close": 40.0},
        {"date": "2026-09-30", "market_time": "r", "close": 50.0}]


def test_etf_ohlc_result_is_ok_with_bars():
    c, calls = _scripted(_Resp(200, {"data": BARS}))
    res = c.etf_ohlc_result("XLK", days=2)
    assert res.status == "ok" and [b["close"] for b in res.data] == [40.0, 50.0]
    assert calls[0][0].endswith("/stock/XLK/ohlc/1d")


def test_etf_ohlc_result_is_ok_and_empty_when_the_provider_says_there_is_nothing():
    c, _ = _scripted(_Resp(200, {"data": []}))
    res = c.etf_ohlc_result("XLK", days=2)
    assert res.status == "ok" and res.data == []           # a real empty answer, not a failure


@pytest.mark.parametrize("outcome", [_Resp(500, text="boom"), _Resp(429), _Resp(404),
                                     httpx.ConnectError("offline"), _Resp(200, ValueError("x"))])
def test_etf_ohlc_result_is_failed_when_the_request_fails(outcome):
    if isinstance(outcome, _Resp) and isinstance(outcome._payload, ValueError):
        def _bad_json():
            raise ValueError("not json")
        outcome.json = _bad_json
    c, _ = _scripted(outcome)
    res = c.etf_ohlc_result("XLK", days=2)
    assert res.status == "failed" and res.data == [] and res.error


def test_the_list_methods_keep_returning_an_empty_list_on_failure():
    c, _ = _scripted(_Resp(500))
    assert c.etf_ohlc("XLK", days=2) == []
    assert c.ohlc_daily("XLK", days=2) == []
    assert c.flow_recent("XLK") == []
    assert c.option_contracts("XLK") == []


def test_ohlc_daily_result_is_ok_and_failed_like_etf_ohlc_result():
    c, _ = _scripted(_Resp(200, {"data": BARS}))
    assert [b["close"] for b in c.ohlc_daily_result("XLK", days=500).data] == [40.0, 50.0]
    c, _ = _scripted(_Resp(500))
    assert c.ohlc_daily_result("XLK", days=500).status == "failed"


def test_a_weekly_result_is_not_session_filtered():
    rows = [{"date": "2026-09-28", "close": 2.0}, {"date": "2026-09-21", "close": 1.0}]
    c, calls = _scripted(_Resp(200, {"data": rows}))
    res = c.etf_ohlc_result("SPY", days=156, candle_size="1w")
    assert res.status == "ok" and [b["close"] for b in res.data] == [1.0, 2.0]
    assert calls[0][1]["limit"] == 156


def test_flow_recent_result_ok_empty_and_failed():
    prints = [{"premium": "100", "size": 20, "option_type": "call"}]
    c, _ = _scripted(_Resp(200, {"data": prints}))
    assert c.flow_recent_result("AAPL").data == prints and c.flow_recent_result("AAPL").status == "ok"
    c, _ = _scripted(_Resp(200, {"data": []}))
    res = c.flow_recent_result("AAPL")
    assert res.status == "ok" and res.data == []
    c, _ = _scripted(_Resp(503))
    res = c.flow_recent_result("AAPL")
    assert res.status == "failed" and res.data == []


def test_flow_recent_result_that_is_not_a_list_is_empty_not_a_crash():
    c, _ = _scripted(_Resp(200, {"data": {"oops": 1}}))
    res = c.flow_recent_result("AAPL")
    assert res.status == "ok" and res.data == []


def _pages(*payloads):
    return [_Resp(200, p) if not isinstance(p, _Resp) else p for p in payloads]


FULL = {"data": [{"option_symbol": f"X{i}"} for i in range(500)]}
SHORT = {"data": [{"option_symbol": "last"}]}


def test_option_contracts_result_ok_over_two_pages():
    c, calls = _scripted(*_pages(FULL, SHORT))
    res = c.option_contracts_result("SPY", expiry="2026-10-17", limit=500)
    assert res.status == "ok" and len(res.data) == 501 and len(calls) == 2


def test_option_contracts_result_ok_and_empty():
    c, _ = _scripted(*_pages({"data": []}))
    res = c.option_contracts_result("SPY")
    assert res.status == "ok" and res.data == []


def test_option_contracts_result_is_failed_when_the_first_page_fails():
    c, _ = _scripted(_Resp(500))
    res = c.option_contracts_result("SPY")
    assert res.status == "failed" and res.data == []
    assert res.error["page"] == 1


def test_option_contracts_result_is_partial_when_a_later_page_fails():
    c, calls = _scripted(_Resp(200, FULL), _Resp(500))
    res = c.option_contracts_result("SPY", limit=500)
    assert res.status == "partial" and len(res.data) == 500   # page 1's rows are kept
    assert res.error["page"] == 2 and res.error["status"] == 500
    assert len(calls) == 1 + uwmod.MAX_ATTEMPTS               # page 2 was retried, then gave up


def test_option_contracts_still_returns_the_rows_it_got_when_a_page_fails():
    c, _ = _scripted(_Resp(200, FULL), _Resp(500))
    assert len(c.option_contracts("SPY", limit=500)) == 500


def test_option_contracts_stops_at_max_pages_and_is_ok():
    c, calls = _scripted(_Resp(200, FULL))
    res = c.option_contracts_result("SPY", limit=500, max_pages=3)
    assert res.status == "ok" and len(res.data) == 1500 and len(calls) == 3


# ── institution_ownership is gone (P15) ──────────────────────────────────────────────────

def test_the_dead_institution_ownership_method_is_deleted():
    assert not hasattr(UWClient, "institution_ownership")


def test_the_dead_uw_client_wrapper_module_is_deleted():
    here = Path(uwmod.__file__).parent
    assert not (here / "uw_client.py").exists()


# ── the key is read per client, and .env still loads ─────────────────────────────────────

def test_the_key_is_read_when_the_client_is_built_not_at_import(monkeypatch):
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "set-after-import")
    assert UWClient().key == "set-after-import"
    assert UWClient().headers["Authorization"] == "Bearer set-after-import"
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "changed-later")
    assert UWClient().key == "changed-later"


def test_an_explicit_key_wins_over_the_environment(monkeypatch):
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "from-env")
    assert UWClient(key="explicit").key == "explicit"


def test_an_empty_key_sends_no_authorization_header(monkeypatch):
    monkeypatch.delenv("UNUSUAL_WHALES_API_KEY", raising=False)
    c = UWClient()
    assert c.key == "" and c.active is False
    assert "Authorization" not in c.headers
    assert "authorization" not in {k.lower() for k in c._client.headers}


def test_an_empty_key_never_puts_bearer_on_the_wire(monkeypatch):
    monkeypatch.delenv("UNUSUAL_WHALES_API_KEY", raising=False)
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(401, json={})

    c = UWClient()
    c._client = httpx.Client(transport=httpx.MockTransport(handler), headers=c.headers)
    c._get("/stock/AAPL/info")
    assert "authorization" not in {k.lower() for k in seen[0].headers}


def test_a_key_in_the_environment_is_sent_as_a_bearer_token(monkeypatch):
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "  padded-key  ")
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"data": {}})

    c = UWClient()
    c._client = httpx.Client(transport=httpx.MockTransport(handler), headers=c.headers)
    c._get("/stock/AAPL/info")
    assert seen[0].headers["authorization"] == "Bearer padded-key"


def test_importing_the_client_still_loads_dotenv_through_config(tmp_path):
    """The client no longer imports a key from ``core.config``, but the import itself loads
    ``.env`` (``config`` calls ``load_dotenv``); without it a key kept only in ``.env`` is lost."""
    repo = Path(uwmod.__file__).resolve().parents[2]
    code = ("import sys; import RevenueChaser.core.providers.unusual_whales; "
            "print('RevenueChaser.core.config' in sys.modules)")
    env = {"PATH": "", "PYTHONPATH": str(repo.parent), "RC_STORE_DIR": str(tmp_path / "s"),
           "RC_CACHE_DIR": str(tmp_path / "c"), "RC_EDGAR_DOCS_DIR": str(tmp_path / "e")}
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True,
                         cwd=str(tmp_path), timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "True"
