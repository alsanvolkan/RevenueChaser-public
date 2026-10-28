"""
Unusual Whales API client — single source for all RevenueChaser data.
Calendar, estimates, flow, dark pool, greeks, fundamentals, insider, short interest.
"""

import os
import logging
import random
import threading
import time
from datetime import date, datetime, timedelta
from typing import Callable, Optional, Any
from urllib.parse import quote
import httpx
from .. import credentials
# Imported for its side effect: core/config.py loads .env, and the key is read from the
# environment per client (credentials.key_for), so .env must be loaded before the first one.
from .. import config as _config  # noqa: F401
from ..option_prints import is_package, print_side
from .ohlc import regular_session_bars
from .result import ProviderResult

logger = logging.getLogger(__name__)
# Redirectable so the end-to-end harness can point fetches at a local mock
# server. Read as a module global inside _get, never captured on the client.
BASE = os.getenv("RC_UW_BASE") or "https://api.unusualwhales.com/api"

# Cheap authenticated endpoint used by ``verify`` to prove the key works.
_AUTH_PROBE = "/stock/AAPL/info"

# A key that just verified OK is trusted for this long across clients, so a job
# submitted from the dashboard and then started by the worker costs one probe,
# not two. Failures are never cached: a fixed key must take effect at once.
_VERIFY_TTL_S = 60.0
_VERIFIED_OK: dict[str, float] = {}

# Retry policy (PL1, the shape the SPX Gamma module used before it moved onto this client): 429, 5xx,
# timeouts and connect errors are retried with exponential backoff and full
# jitter (a random fraction of the step, so a job's parallel workers do not retry
# in lock-step); the API's Retry-After is honoured as sent, capped so one
# throttled call cannot stall a job.
MAX_ATTEMPTS = 3
BACKOFF_BASE_S = 1.0
RETRY_AFTER_CAP_S = 30.0
_RETRY_EXC = (httpx.TimeoutException, httpx.ConnectError)
# Breaker (#166): after this many consecutive *final* 429/5xx/transport failures a client sends
# nothing more, so a sustained outage costs a job ~5 retry ladders, not one per remaining call.
BREAKER_THRESHOLD = 5
# Only this many failure records are kept for ``health()``; the count is exact.
_MAX_FAILURE_RECORDS = 20

# Patch points for tests (or pass ``sleep=`` to the client).
_sleep = time.sleep
_random = random.random


def _seg(value) -> str:
    """One URL path segment: percent-encoded, so a ticker (or session, or candle size) can
    never add a segment or a query string. ``BRK.B`` and ``AAPL`` come out unchanged."""
    return quote(str(value), safe="")


# Body shapes for the endpoint methods (B5): each takes a decoded body and returns what the method hands
# back. ``shape({})`` is the empty placeholder a failed request gets, the same value ``_get``'s ``{}``
# used to turn into, so the plain methods return exactly what they returned before.
def _rows(data) -> list:
    """The ``data`` list of a ``{"data": [...]}`` body (or a bare list body)."""
    return data.get("data", []) if isinstance(data, dict) else data or []


def _list_rows(data) -> list:
    """:func:`_rows`, but anything that is not a list is empty."""
    items = _rows(data)
    return items if isinstance(items, list) else []


def _obj(data) -> dict:
    """The ``data`` object of a ``{"data": {...}}`` body; a bare list body is empty."""
    return data.get("data", {}) if isinstance(data, dict) else {}


def _obj_or_body(data):
    """The ``data`` object of a ``{"data": {...}}`` body, or the bare body itself."""
    return data.get("data", {}) if isinstance(data, dict) else data or {}


def _body(data):
    """The decoded body as is."""
    return data


def _retry_after(resp) -> Optional[float]:
    """Seconds from a numeric ``Retry-After`` header, capped; None otherwise."""
    raw = (getattr(resp, "headers", None) or {}).get("Retry-After")
    try:
        return min(max(float(raw), 0.0), RETRY_AFTER_CAP_S)
    except (TypeError, ValueError):
        return None  # absent, or an HTTP-date: use the normal backoff


class UWClient:
    def __init__(self, key: str = None, sleep: Optional[Callable[[float], Any]] = None):
        # Read per client, at call time (a key set after import counts). No key: no
        # Authorization header at all, never a bare "Bearer ".
        self.key = key or credentials.key_for("unusual_whales")
        self.headers = {
            "Accept": "application/json",
            "UW-CLIENT-API-ID": "100001",
        }
        if self.key:
            self.headers["Authorization"] = f"Bearer {self.key}"
        self._client = httpx.Client(timeout=15, headers=self.headers)
        # Set by ``_get`` when the API rejects our credentials (401/403), so an
        # empty board can be explained instead of just looking like "no data".
        self.auth_error: Optional[dict] = None
        self._verified: Optional[tuple[bool, Optional[str]]] = None
        self._sleep = sleep
        # Per-instance call accounting for ``health()`` (a job builds one client).
        self._calls = 0
        self._failed = 0
        self._failures: list[dict] = []
        # Breaker state; like the counters above it is only touched under ``_health_lock``.
        self._consecutive_failures = 0
        self._breaker_open = False
        self._skipped = 0
        self._health_lock = threading.Lock()
        # Optional observer of every HTTP answer (``on_response(ep, response)``), for the market-store builder's
        # rate-header sampling and call counting (#206). None by default; it never changes what a call returns,
        # and an observer that raises is ignored.
        self.on_response: Optional[Callable[[str, Any], None]] = None

    @property
    def active(self) -> bool:
        """True when a key is *present*. Says nothing about whether it works —
        use :meth:`verify` for that."""
        return bool(self.key)

    def _get(self, ep: str, params: dict = None) -> dict | list:
        """Compatibility wrapper (ADR-1): the payload, or ``{}`` on final failure.

        The failure itself is not lost: it is recorded on the client and shows
        in :meth:`health`. No client method calls it any more (B5, DF100): each
        endpoint method has a ``*_result`` twin built on :meth:`get_result`, which
        new code should call instead.
        """
        res = self.get_result(ep, params)
        return res.data if res.status == "ok" else {}

    def _shaped(self, ep: str, params: Optional[dict], shape: Callable[[Any], Any]) -> ProviderResult:
        """One request through :meth:`get_result`, its body passed through ``shape``.

        ``ok``: ``data`` is the shaped body (possibly empty: the provider has nothing).
        ``failed``: ``data`` is ``shape({})``, the placeholder the plain method has always
        returned, and ``error`` says why (status, error, attempts)."""
        res = self.get_result(ep, params)
        if res.status != "ok":
            return ProviderResult("failed", shape({}), res.error, res.endpoint)
        return ProviderResult("ok", shape(res.data), None, res.endpoint)

    def _batched(self, ep: str, tickers: list[str], ticker_param: str, extra: dict,
                 keys: tuple[str, str]) -> ProviderResult:
        """Rows of a comma-list ticker endpoint, 100 tickers per request, as ``{ticker: row}``.

        ``ok`` when every chunk answered, ``partial`` when some failed (``data`` holds the
        others' rows, ``error`` names the first failed chunk and how many failed), ``failed``
        when all did (``data`` is ``{}``). No tickers: ``ok`` and no request. A row is keyed by
        its first non-empty ``keys`` field."""
        result: dict[str, dict] = {}
        chunks = [tickers[i:i + 100] for i in range(0, len(tickers or []), 100)]
        first_error: Optional[dict] = None
        failed = 0
        for n, chunk in enumerate(chunks):
            res = self.get_result(ep, {ticker_param: ",".join(chunk), **extra})
            if res.status != "ok":
                failed += 1
                if first_error is None:
                    first_error = {**(res.error or {}), "chunk": n}
                continue
            items = _rows(res.data)
            if isinstance(items, dict):
                items = [items]
            for item in items:
                if not isinstance(item, dict):
                    continue
                t = item.get(keys[0]) or item.get(keys[1])
                if t:
                    result[t] = item
        if not failed:
            return ProviderResult("ok", result, None, ep)
        err = {**first_error, "failed_chunks": failed, "chunks": len(chunks)}
        return ProviderResult("failed" if failed == len(chunks) else "partial", result, err, ep)

    def get_result(self, ep: str, params: dict = None) -> ProviderResult:
        """GET ``ep`` with retry/backoff; a :class:`ProviderResult`, never raises."""
        with self._health_lock:
            self._calls += 1
            skip = self._breaker_open
            if skip:
                self._skipped += 1
        if skip:
            return self._failed_result(ep, None, "breaker_open", 0)
        attempts = 0
        while True:
            attempts += 1
            last = attempts >= MAX_ATTEMPTS
            try:
                r = self._client.get(f"{BASE}{ep}", params=params)
            except _RETRY_EXC as e:
                if not last:
                    self._backoff(attempts, None)
                    continue
                logger.warning(f"UW {ep}: {type(e).__name__}: {e} (after {attempts} attempts)")
                return self._failed_result(ep, None, type(e).__name__, attempts, trips_breaker=True)
            except Exception as e:  # noqa: BLE001 - anything else is final, not retried
                logger.warning(f"UW {ep}: {type(e).__name__}: {e}")
                return self._failed_result(ep, None, type(e).__name__, attempts,
                                           trips_breaker=isinstance(e, httpx.TransportError))

            if self.on_response is not None:
                try:
                    self.on_response(ep, r)
                except Exception:  # noqa: BLE001 - an observer must never change the call's outcome
                    logger.debug("UW on_response observer failed", exc_info=True)
            status = r.status_code
            if status < 400:
                try:
                    res = ProviderResult("ok", r.json(), None, ep)
                except ValueError as e:
                    logger.warning(f"UW {ep}: invalid JSON: {e}")
                    return self._failed_result(ep, status, "invalid_json", attempts)
                with self._health_lock:
                    self._consecutive_failures = 0
                return res
            body = (r.text or "")[:200].replace("\n", " ")
            if (status == 429 or status >= 500) and not last:
                self._backoff(attempts, _retry_after(r) if status == 429 else None)
                continue
            if status in (401, 403):
                # Credential failure: remember it and log loudly once. Silently
                # returning {} here is what made a dead key look like an empty
                # market.
                first = self.auth_error is None
                self.auth_error = {"status": status, "endpoint": ep, "detail": body}
                if first:
                    logger.error(credentials.rejected_message("unusual_whales", status))
                else:
                    logger.warning(f"UW {ep}: HTTP {status} (credentials rejected)")
                return self._failed_result(ep, status, f"HTTP {status}", attempts)
            logger.warning(f"UW {ep}: HTTP {status}: {body}")
            if status == 429 or status >= 500:
                return self._failed_result(ep, status, f"HTTP {status}", attempts, trips_breaker=True)
            # Any other 4xx is the request's own fault (unknown ticker, bad
            # param): a failed result for the caller, but not a provider outage,
            # so it does not count against ``health()``.
            return ProviderResult("failed", {}, {"status": status, "error": f"HTTP {status}",
                                                 "attempts": attempts}, ep)

    def _backoff(self, attempt: int, retry_after: Optional[float]) -> None:
        delay = (retry_after if retry_after is not None
                 else _random() * BACKOFF_BASE_S * 2 ** (attempt - 1))
        (self._sleep or _sleep)(delay)

    def _failed_result(self, ep: str, status: Optional[int], error: str,
                       attempts: int, *, trips_breaker: bool = False) -> ProviderResult:
        """Record a final failure for ``health()`` and wrap it as a result.

        ``trips_breaker`` is set for the failures that say the provider is down or throttling
        (429, 5xx, transport errors); a 401/403/404 or a bad body is not an outage."""
        # ``ep`` is the path only; the key travels in a header, never in here.
        rec = {"endpoint": ep, "status": status, "error": error, "attempts": attempts}
        opened = False
        with self._health_lock:
            self._failed += 1
            if len(self._failures) < _MAX_FAILURE_RECORDS:
                self._failures.append(rec)
            if trips_breaker:
                self._consecutive_failures += 1
                if self._consecutive_failures >= BREAKER_THRESHOLD and not self._breaker_open:
                    self._breaker_open = opened = True
        if opened:
            logger.warning(f"UW: {BREAKER_THRESHOLD} consecutive requests failed (last: {ep}); "
                           "no further requests from this client")
        return ProviderResult("failed", {}, dict(rec), ep)

    def health(self) -> dict:
        """Call accounting for this client: how many ``_get`` calls ran, how many
        finally failed (after retries), the first 20 failures, and the auth
        error if the key was rejected. A job reads this after its runner.

        ``breaker_open`` is True once the client stopped sending requests after
        ``BREAKER_THRESHOLD`` consecutive provider failures; ``skipped_calls`` is how many
        calls it then refused (each is also counted in ``calls`` and ``failed``)."""
        with self._health_lock:
            return {"calls": self._calls, "failed": self._failed,
                    "failures": [dict(f) for f in self._failures],
                    "auth_error": dict(self.auth_error) if self.auth_error else None,
                    "breaker_open": self._breaker_open, "skipped_calls": self._skipped}

    def verify(self) -> tuple[bool, Optional[str]]:
        """Preflight the credentials: ``(ok, message)``.

        ``ok`` is False with an actionable message when the key is missing or
        the API rejects it. A network/transport failure is *not* treated as a
        credential problem — the caller should be allowed to try and fail on its
        own terms rather than be blocked by a flaky probe.

        The result is cached per client instance: the dashboard builds a fresh
        ``UWClient`` per request, so a key fixed in .env takes effect on restart
        without a stale "invalid" verdict lingering inside a long-lived object.
        """
        if self._verified is not None:
            return self._verified
        import time as _time
        if self.key and _time.time() - _VERIFIED_OK.get(self.key, 0.0) < _VERIFY_TTL_S:
            self._verified = (True, None)
            return self._verified
        if not self.key:
            self._verified = (False, credentials.missing_message("unusual_whales"))
            return self._verified
        try:
            r = self._client.get(f"{BASE}{_AUTH_PROBE}")
        except Exception as e:  # transport problem — not a credential verdict
            logger.warning(f"UW auth probe failed to reach the API: {e}")
            self._verified = (True, None)
            return self._verified
        if r.status_code in (401, 403):
            body = (r.text or "")[:200].replace("\n", " ")
            self.auth_error = {"status": r.status_code, "endpoint": _AUTH_PROBE, "detail": body}
            self._verified = (False, credentials.rejected_message(
                "unusual_whales", r.status_code))
            return self._verified
        self._verified = (True, None)
        _VERIFIED_OK[self.key] = _time.time()
        return self._verified

    def close(self) -> None:
        """Release the HTTP connection pool (safe to call more than once)."""
        try:
            self._client.close()
        except Exception:  # noqa: BLE001 - closing is best-effort
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ── Calendar ──
    def calendar_day(self, d: date) -> list[dict]:
        return self.calendar_day_result(d).data

    def calendar_day_result(self, d: date) -> ProviderResult:
        """:meth:`calendar_day` with the outcome of its two requests: ``ok``, ``partial`` (one
        session failed: ``data`` holds the other's events, ``error`` names the session) or
        ``failed`` (both failed: ``data`` is empty)."""
        events = []
        first_error: Optional[dict] = None
        failed = 0
        for session in ("premarket", "afterhours"):
            res = self.get_result(f"/earnings/{_seg(session)}", {"date": d.isoformat(), "limit": 500})
            if res.status != "ok":
                failed += 1
                if first_error is None:
                    first_error = {**(res.error or {}), "session": session}
                continue
            for item in _rows(res.data):
                events.append({
                    "ticker": item.get("symbol", item.get("ticker", "")),
                    "earnings_date": d,
                    "earnings_time": "BMO" if session == "premarket" else "AMC",
                    "eps_estimate": item.get("eps_estimate") or item.get("estimate"),
                    "sector": item.get("sector", ""),
                    "industry": item.get("industry", ""),
                    "source": "UW",
                })
        if not failed:
            return ProviderResult("ok", events, None, "/earnings/{session}")
        return ProviderResult("failed" if failed == 2 else "partial", events, first_error,
                              "/earnings/{session}")

    def calendar_range(self, start: date, end: date) -> list[dict]:
        events = []
        d = start
        while d <= end:
            if d.weekday() < 5:
                events.extend(self.calendar_day(d))
            d += timedelta(days=1)
        return events

    # ── Estimates / Earnings History ──
    def earnings_rows(self, ticker: str) -> list[dict]:
        """The ``/earnings/{ticker}`` rows, newest first: ONE request serves both the
        upcoming-event read (``rows[0]``) and the print history (#198 E14)."""
        return self.earnings_rows_result(ticker).data

    def earnings_rows_result(self, ticker: str) -> ProviderResult:
        """:meth:`earnings_rows` with the request outcome (``ok`` or ``failed``)."""
        return self._shaped(f"/earnings/{_seg(ticker)}", None, _rows)

    def earnings_data(self, ticker: str) -> dict:
        items = self.earnings_rows(ticker)
        return items[0] if items else {}

    def earnings_history(self, ticker: str) -> list[dict]:
        return self.earnings_rows(ticker)

    def stock_earnings(self, ticker: str) -> list[dict]:
        """Raw EPS print series for R2 (guidance).

        ``GET /stock/{ticker}/earnings`` — statement-style rows carrying
        ``report_type`` (annual/quarterly), ``reported_eps`` / ``estimated_eps``,
        ``surprise_percentage`` and ``fiscal_date_ending``. This is R2's source;
        ``/earnings/{ticker}`` (calendar + price/straddle) stays R4's tape.
        """
        return self.stock_earnings_result(ticker).data

    def stock_earnings_result(self, ticker: str) -> ProviderResult:
        """:meth:`stock_earnings` with the request outcome (``ok`` or ``failed``)."""
        return self._shaped(f"/stock/{_seg(ticker)}/earnings", None, _list_rows)

    # ── Fundamentals ──
    def fundamentals(self, ticker: str) -> dict:
        return self.fundamentals_result(ticker).data

    def fundamentals_result(self, ticker: str) -> ProviderResult:
        """:meth:`fundamentals` with the request outcome (``ok`` or ``failed``)."""
        return self._shaped(f"/stock/{_seg(ticker)}/financials", None, _obj)

    def financials(self, ticker: str) -> dict:
        """Full financials: income_statements, balance_sheets, cash_flows, earnings."""
        return self.financials_result(ticker).data

    def financials_result(self, ticker: str) -> ProviderResult:
        """:meth:`financials` with the request outcome (``ok`` or ``failed``)."""
        return self._shaped(f"/stock/{_seg(ticker)}/financials", None, _obj)

    def income_statements(self, ticker: str, report_type: str = None) -> list[dict]:
        """Dedicated income statement series (report_type: e.g. 'annual')."""
        return self.income_statements_result(ticker, report_type).data

    def income_statements_result(self, ticker: str, report_type: str = None) -> ProviderResult:
        """:meth:`income_statements` with the request outcome (``ok`` or ``failed``)."""
        params = {"report_type": report_type} if report_type else None
        return self._shaped(f"/stock/{_seg(ticker)}/income-statements", params, _rows)

    def balance_sheets(self, ticker: str, report_type: str = None) -> list[dict]:
        """Dedicated balance sheet series."""
        return self.balance_sheets_result(ticker, report_type).data

    def balance_sheets_result(self, ticker: str, report_type: str = None) -> ProviderResult:
        """:meth:`balance_sheets` with the request outcome (``ok`` or ``failed``)."""
        params = {"report_type": report_type} if report_type else None
        return self._shaped(f"/stock/{_seg(ticker)}/balance-sheets", params, _rows)

    def cash_flows(self, ticker: str, report_type: str = None) -> list[dict]:
        """Dedicated cash flow statement series."""
        return self.cash_flows_result(ticker, report_type).data

    def cash_flows_result(self, ticker: str, report_type: str = None) -> ProviderResult:
        """:meth:`cash_flows` with the request outcome (``ok`` or ``failed``)."""
        params = {"report_type": report_type} if report_type else None
        return self._shaped(f"/stock/{_seg(ticker)}/cash-flows", params, _rows)

    @staticmethod
    def _num(row: dict, *keys):
        """First present, non-empty key as float, else None (never 0).

        Missing fields must NOT be coerced to 0: R3/R7 would then treat
        'never reported' as 'reported as zero' (a software firm with no
        inventory looked 'lean'; empty deferred revenue could fire
        deceleration). An explicit 0 in the payload is kept as 0.
        """
        if not isinstance(row, dict):
            return None
        for k in keys:
            v = row.get(k)
            if v is None:
                continue
            if isinstance(v, str) and v.strip() == "":
                continue
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
        return None

    def _map_financials(self, ticker: str, fin: dict | None = None) -> list[dict]:
        """Map UW financials to rule3-compatible records (revenue, gross_margin, inventory, etc.).

        Missing fields map to None (never 0); records are sorted newest-first by
        ``fiscal_date`` and windowed to the last 8, so ``[0]`` is the latest.
        ``fin`` is a ``financials`` body the caller already has (the Earnings pipeline also stores it, #297);
        without it the body is requested here.
        """
        if fin is None:
            fin = self.financials(ticker)
        if not fin:
            return []

        income = fin.get("income_statements", [])
        balance = fin.get("balance_sheets", [])
        cashflow = fin.get("cash_flows", [])

        # Index balance and cashflow by fiscal_date_ending, quarterly rows only (the income rows' rule below): at a
        # fiscal year end the annual and the Q4 row share one date, and the later one served used to win (review S3).
        bal_by_date = _quarterly_by_date(balance)
        cf_by_date = _quarterly_by_date(cashflow)

        num = self._num
        records = []
        for inc in income:
            fd = inc.get("fiscal_date_ending") or ""
            rt = (inc.get("report_type") or "").lower()
            # R7/R3 score the last filed QUARTER. Only drop rows when the
            # payload actually says a period type; rows without one keep the
            # legacy behaviour (never guess an annual/quarterly split).
            if rt and rt != "quarterly":
                continue
            bal = bal_by_date.get(fd, {})
            cf = cf_by_date.get(fd, {})

            rev = num(inc, "total_revenue")
            cost_rev = num(inc, "cost_of_revenue")
            gross_profit = num(inc, "gross_profit")
            # gross_profit may carry the direct number or be recoverable.
            gp = gross_profit if gross_profit is not None else ((rev - cost_rev) if rev is not None and cost_rev is not None else None)
            gross_margin = gp / rev if rev and rev > 0 and gp is not None else None
            operating_income = num(inc, "operating_income")
            ebit = num(inc, "ebit")
            # OM headline prefers operating_income; ebit is the fallback when OI
            # is absent (a valid negative OI is real, never replaced by EBIT).
            om_num = operating_income if operating_income is not None else ebit
            operating_margin = om_num / rev if rev and rev > 0 and om_num is not None else None
            sga = num(inc, "selling_general_and_administrative")

            records.append({
                "revenue": rev,
                "gross_margin": round(gross_margin, 3) if gross_margin is not None else None,
                "operating_margin": round(operating_margin, 3) if operating_margin is not None else None,
                "operating_income": operating_income,
                "sga": sga,
                "net_income": num(inc, "net_income"),
                # R7 period cross-check: cash-flow's own net_income must match
                # the income statement's for the same fiscal date. A YTD row
                # joined to a quarterly CFO row would make Sloan a lie.
                "cash_net_income": num(cf, "net_income"),
                "cash_from_operations": num(cf, "operating_cash_flow"),
                "total_assets": num(bal, "total_assets"),
                "inventory": num(bal, "inventory"),
                "receivables": num(bal, "current_net_receivables", "net_receivables"),
                "deferred_revenue": num(bal, "deferred_revenue", "customer_deposits"),
                "rpo": num(bal, "rpo", "remaining_performance_obligation"),
                "fiscal_date": fd,
            })

        # Newest-first when dates are known; undated rows trail (never reordered
        # against a dated series into a lie).
        records.sort(key=lambda r: (not bool(r["fiscal_date"]), r["fiscal_date"]), reverse=True)
        return records[:8]  # last 8 periods sufficient

    # ── Options Flow ──
    def flow_recent(self, ticker: str) -> list[dict]:
        return self.flow_recent_result(ticker).data

    def flow_recent_result(self, ticker: str) -> ProviderResult:
        """:meth:`flow_recent` that says whether the request worked: ``ok`` (the list may be
        empty: a quiet tape) or ``failed`` (the list is an empty placeholder)."""
        return self._shaped(f"/stock/{_seg(ticker)}/flow-recent", None, _list_rows)

    def aggregate_flow(self, ticker: str, flow: list = None) -> dict:
        """The tape's premium in buckets. ``inst_call``/``inst_put``/``retail_*`` split by size only (size >= 10 is
        institutional; R8 and Swing read these). The R5 lean (docs/rules/redesign.md R5, review S4) reads only
        institutional prints, size >= 10 AND premium >= ``INST_PRINT_MIN_PREMIUM``, by the print's side
        (``core/option_prints``): ``inst_bull_side`` = ask-side calls + bid-side puts, ``inst_bear_side`` = bid-side
        calls + ask-side puts, ``inst_mid`` dropped, ``inst_no_side`` neither. A package print (multi-leg or
        stock-tied condition code) is left out of all four and summed in ``inst_package``. ``side_served`` is True
        when at least one institutional print outside a package carries a side, False when such prints exist and
        none does (no lean), None when there is none to judge."""
        if flow is None:
            flow = self.flow_recent(ticker)
        if not flow:
            return _empty_flow()
        inst_c, inst_p, ret_c, ret_p = 0.0, 0.0, 0.0, 0.0
        unknown_p = 0.0
        sweep_p, block_p = 0.0, 0.0
        bull, bear = 0.0, 0.0
        lean = {"bull": 0.0, "bear": 0.0, "mid": 0.0, "none": 0.0, "package": 0.0}
        for t in flow:
            prem = _flow_num(t.get("premium")) or 0.0
            size = _flow_num(t.get("size"))
            ot = str(t.get("option_type", "") or "").strip().lower()
            tt = str(t.get("trade_type", "") or "").lower()
            # Classify only when BOTH size and option type are known. A missing
            # size is not a small lot and an unknown option type is not a put;
            # unclassifiable premium is tracked separately, never fabricated.
            if size is not None and ot in ("call", "put"):
                if size < 10:
                    if ot == "call": ret_c += prem
                    else: ret_p += prem
                else:
                    if ot == "call": inst_c += prem
                    else: inst_p += prem
                    if prem >= _config.INST_PRINT_MIN_PREMIUM:
                        lean[_lean_bucket(t, ot)] += prem
            else:
                unknown_p += prem
            if "sweep" in tt: sweep_p += prem
            if "block" in tt: block_p += prem
            delta = _flow_num(t.get("delta")) or 0.0
            if ot == "call" and delta > 0.20: bull += prem
            if ot == "put" and abs(delta) > 0.20: bear += prem
        return {
            "inst_call": inst_c, "inst_put": inst_p, "retail_call": ret_c, "retail_put": ret_p,
            "unknown": unknown_p,
            "sweep": sweep_p, "block": block_p, "bullish": bull, "bearish": bear,
            "inst_net": inst_c - inst_p,
            "inst_bull_side": lean["bull"], "inst_bear_side": lean["bear"], "inst_mid": lean["mid"],
            "inst_no_side": lean["none"], "inst_package": lean["package"],
            "inst_lean_net": lean["bull"] - lean["bear"],
            "side_served": (lean["bull"] + lean["bear"] + lean["mid"] > 0
                            if lean["bull"] + lean["bear"] + lean["mid"] + lean["none"] > 0 else None),
        }

    # ── Dark Pool ──
    def dark_pool(self, ticker: str, limit: int = 100) -> list[dict]:
        return self.dark_pool_result(ticker, limit).data

    def dark_pool_result(self, ticker: str, limit: int = 100) -> ProviderResult:
        """:meth:`dark_pool` with the request outcome (``ok`` or ``failed``)."""
        return self._shaped(f"/darkpool/{_seg(ticker)}", {"limit": limit}, _rows)

    def dark_pool_net(self, ticker: str) -> float:
        prints = self.dark_pool(ticker, 100)
        return sum(float(p.get("notional", 0) or 0) for p in prints)

    # ── Greeks ──
    def greeks(self, ticker: str) -> dict:
        return self.greeks_result(ticker).data

    def greeks_result(self, ticker: str) -> ProviderResult:
        """:meth:`greeks` with the request outcome (``ok`` or ``failed``); the body is unshaped."""
        return self._shaped(f"/stock/{_seg(ticker)}/greeks", None, _body)

    # ── GEX ──
    def gex_levels(self, ticker: str, date: str = None) -> dict:
        return self.gex_levels_result(ticker, date).data

    def gex_levels_result(self, ticker: str, date: str = None) -> ProviderResult:
        """:meth:`gex_levels` with the request outcome (``ok`` or ``failed``)."""
        params = {"date": date} if date else None
        return self._shaped(f"/stock/{_seg(ticker)}/gex-levels", params, _obj)

    def term_structure(self, ticker: str, date: str = None) -> list[dict]:
        """ATM IV per expiry for one ticker (/stock/{t}/volatility/term-structure).

        Rows carry ``dte`` and ``volatility``; used to measure the near-to-45-DTE
        per-day IV slope that feeds R4's term-structure points. Failures surface
        as an empty list — a missing curve never fabricates a slope.
        """
        return self.term_structure_result(ticker, date).data

    def term_structure_result(self, ticker: str, date: str = None) -> ProviderResult:
        """:meth:`term_structure` with the request outcome (``ok`` or ``failed``)."""
        params = {"date": date} if date else None
        return self._shaped(f"/stock/{_seg(ticker)}/volatility/term-structure", params, _rows)

    def greek_exposure(self, ticker: str, timeframe: str = "1Y", date: str = None) -> list[dict]:
        return self.greek_exposure_result(ticker, timeframe, date).data

    def greek_exposure_result(self, ticker: str, timeframe: str = "1Y", date: str = None) -> ProviderResult:
        """:meth:`greek_exposure` with the request outcome (``ok`` or ``failed``)."""
        params = {"timeframe": timeframe}
        if date:
            params["date"] = date
        return self._shaped(f"/stock/{_seg(ticker)}/greek-exposure", params, _rows)

    def greek_exposure_by_strike(self, ticker: str, date: str = None) -> list[dict]:
        return self.greek_exposure_by_strike_result(ticker, date).data

    def greek_exposure_by_strike_result(self, ticker: str, date: str = None) -> ProviderResult:
        """:meth:`greek_exposure_by_strike` with the request outcome (``ok`` or ``failed``)."""
        params = {"date": date} if date else None
        return self._shaped(f"/stock/{_seg(ticker)}/greek-exposure/strike", params, _rows)

    def greek_exposure_by_expiry(self, ticker: str, date: str = None) -> list[dict]:
        return self.greek_exposure_by_expiry_result(ticker, date).data

    def greek_exposure_by_expiry_result(self, ticker: str, date: str = None) -> ProviderResult:
        """:meth:`greek_exposure_by_expiry` with the request outcome (``ok`` or ``failed``)."""
        params = {"date": date} if date else None
        return self._shaped(f"/stock/{_seg(ticker)}/greek-exposure/expiry", params, _rows)

    def spot_exposures_by_strike(self, ticker: str, date: str = None,
                                 min_strike: float = None, max_strike: float = None,
                                 limit: int = 500) -> list[dict]:
        """Strike-level spot exposures via /api/stock/{ticker}/spot-exposures/strike."""
        return self.spot_exposures_by_strike_result(ticker, date, min_strike, max_strike, limit).data

    def spot_exposures_by_strike_result(self, ticker: str, date: str = None,
                                        min_strike: float = None, max_strike: float = None,
                                        limit: int = 500) -> ProviderResult:
        """:meth:`spot_exposures_by_strike` with the request outcome (``ok`` or ``failed``)."""
        params = {"limit": limit}
        if date:
            params["date"] = date
        if min_strike is not None:
            params["min_strike"] = min_strike
        if max_strike is not None:
            params["max_strike"] = max_strike
        return self._shaped(f"/stock/{_seg(ticker)}/spot-exposures/strike", params, _rows)

    # ── OI / Flow per expiry (LEAPS accumulation) ──
    def oi_per_expiry(self, ticker: str, date: str = None) -> list[dict]:
        return self.oi_per_expiry_result(ticker, date).data

    def oi_per_expiry_result(self, ticker: str, date: str = None) -> ProviderResult:
        """:meth:`oi_per_expiry` with the request outcome (``ok`` or ``failed``)."""
        params = {"date": date} if date else None
        return self._shaped(f"/stock/{_seg(ticker)}/oi-per-expiry", params, _rows)

    def flow_per_expiry(self, ticker: str) -> list[dict]:
        return self.flow_per_expiry_result(ticker).data

    def flow_per_expiry_result(self, ticker: str) -> ProviderResult:
        """:meth:`flow_per_expiry` with the request outcome (``ok`` or ``failed``)."""
        return self._shaped(f"/stock/{_seg(ticker)}/flow-per-expiry", None, _rows)

    def iv_rank(self, ticker: str) -> list[dict]:
        return self.iv_rank_result(ticker).data

    def iv_rank_result(self, ticker: str) -> ProviderResult:
        """:meth:`iv_rank` with the request outcome (``ok`` or ``failed``)."""
        return self._shaped(f"/stock/{_seg(ticker)}/iv-rank", None, _rows)

    def market_oi_change(self, limit: int = 200) -> list[dict]:
        return self.market_oi_change_result(limit).data

    def market_oi_change_result(self, limit: int = 200) -> ProviderResult:
        """:meth:`market_oi_change` with the request outcome (``ok`` or ``failed``)."""
        return self._shaped("/market/oi-change", {"limit": limit}, _rows)

    # ── Option trades (raw tape) & flow alerts (LEAPS discovery) ──
    def option_trades(self, ticker: str = None, min_dte: int = None,
                      max_dte: int = None, min_premium: float = None,
                      size_greater_oi: bool = None, limit: int = 250,
                      page: int = 1, **extra) -> list[dict]:
        """Raw option tape prints via /api/option-trades.

        Native far-dated filtering via ``min_dte``/``max_dte``; each print
        carries its own ``delta`` (no separate Greeks call needed).
        """
        return self.option_trades_result(ticker, min_dte, max_dte, min_premium, size_greater_oi,
                                         limit, page, **extra).data

    def option_trades_result(self, ticker: str = None, min_dte: int = None,
                             max_dte: int = None, min_premium: float = None,
                             size_greater_oi: bool = None, limit: int = 250,
                             page: int = 1, **extra) -> ProviderResult:
        """:meth:`option_trades` with the request outcome (``ok`` or ``failed``)."""
        params = {"limit": limit, "page": page}
        if ticker:
            params["ticker_symbol"] = ticker
        if min_dte is not None:
            params["min_dte"] = min_dte
        if max_dte is not None:
            params["max_dte"] = max_dte
        if min_premium is not None:
            params["min_premium"] = min_premium
        if size_greater_oi is not None:
            params["size_greater_oi"] = size_greater_oi
        params.update(extra)
        return self._shaped("/option-trades", params, _rows)

    def flow_alerts(self, min_premium: float = None, size_greater_oi: bool = None,
                    is_otm: bool = None, limit: int = 250, **extra) -> list[dict]:
        """Unusual-activity alerts via /api/option-trades/flow-alerts.

        Only the endpoint's documented params are sent; DTE filtering (if
        needed) is done client-side from ``expiry``.
        """
        return self.flow_alerts_result(min_premium, size_greater_oi, is_otm, limit, **extra).data

    def flow_alerts_result(self, min_premium: float = None, size_greater_oi: bool = None,
                           is_otm: bool = None, limit: int = 250, **extra) -> ProviderResult:
        """:meth:`flow_alerts` with the request outcome (``ok`` or ``failed``)."""
        params = {"limit": limit}
        if min_premium is not None:
            params["min_premium"] = min_premium
        if size_greater_oi is not None:
            params["size_greater_oi"] = size_greater_oi
        if is_otm is not None:
            params["is_otm"] = is_otm
        params.update(extra)
        return self._shaped("/option-trades/flow-alerts", params, _rows)

    # ── Option chains (full contract list + expiry breakdown) ──
    def expiry_breakdown(self, ticker: str, date: str = None) -> list[dict]:
        """Every listed expiry for a ticker with ``chains``/``open_interest``/``volume``.

        Used to pick a sensible set of expiries before fetching chains (the
        per-expiry contract endpoint is paginated at 500 rows).
        """
        return self.expiry_breakdown_result(ticker, date).data

    def expiry_breakdown_result(self, ticker: str, date: str = None) -> ProviderResult:
        """:meth:`expiry_breakdown` with the request outcome (``ok`` or ``failed``)."""
        params = {"date": date} if date else None
        return self._shaped(f"/stock/{_seg(ticker)}/expiry-breakdown", params, _rows)

    def option_contracts(self, ticker: str, expiry: str = None,
                         option_type: str = None, limit: int = 500,
                         max_pages: int = 6, **extra) -> list[dict]:
        """All contracts for a ticker/expiry via /api/stock/{ticker}/option-contracts.

        The endpoint caps at 500 rows per page, so page until a short page comes
        back (or ``max_pages`` is hit). Rows carry ``option_symbol``, NBBO,
        ``implied_volatility``, Greeks, ``open_interest`` and ``volume``. A page
        that fails ends the list there; use :meth:`option_contracts_result` to
        know it happened.
        """
        return self.option_contracts_result(ticker, expiry, option_type, limit,
                                            max_pages, **extra).data

    def option_contracts_result(self, ticker: str, expiry: str = None,
                                option_type: str = None, limit: int = 500,
                                max_pages: int = 6, **extra) -> ProviderResult:
        """:meth:`option_contracts` with the paging outcome: ``ok`` (every page answered),
        ``partial`` (a later page failed: ``data`` holds the earlier pages, ``error`` names
        the page) or ``failed`` (the first page failed: ``data`` is empty)."""
        ep = f"/stock/{_seg(ticker)}/option-contracts"
        rows: list[dict] = []
        page = 1
        while page <= max_pages:
            params = {"limit": limit, "page": page}
            if expiry:
                params["expiry"] = expiry
            if option_type:
                params["option_type"] = option_type
            params.update(extra)
            res = self.get_result(ep, params)
            if res.status != "ok":
                err = {**(res.error or {}), "page": page}
                return ProviderResult("failed" if page == 1 else "partial", rows, err, ep)
            data = res.data
            items = data.get("data", []) if isinstance(data, dict) else data or []
            if not isinstance(items, list) or not items:
                break
            rows.extend(items)
            if len(items) < limit:
                break
            page += 1
        return ProviderResult("ok", rows, None, ep)

    # ── Short Interest ──
    def short_interest(self, ticker: str) -> dict:
        return self.short_interest_result(ticker).data

    def short_interest_result(self, ticker: str) -> ProviderResult:
        """:meth:`short_interest` with the request outcome (``ok`` or ``failed``)."""
        return self._shaped(f"/shorts/{_seg(ticker)}/interest-float/v2", None, _short_row)

    def short_screener(self, tickers: list[str]) -> dict[str, dict]:
        """Batch short interest/float rows via /api/short_screener.

        Same row shape as V2 interest-float. Chunked like ``screener_batch``
        (the ``tickers`` param is a comma list); returns {ticker: row}.
        """
        return self.short_screener_result(tickers).data

    def short_screener_result(self, tickers: list[str]) -> ProviderResult:
        """:meth:`short_screener` with the outcome of its chunks: ``ok``, ``partial`` (some
        chunks failed; ``error`` names the first) or ``failed`` (all did)."""
        return self._batched("/short_screener", tickers, "tickers", {"limit": 500}, ("symbol", "ticker"))

    @staticmethod
    def _parse_short_row(row: dict) -> dict:
        """Normalise a short-interest row into R5 squeeze inputs.

        Missing fields map to ``None`` (never a 0 fill) so a missing metric is
        honestly absent, not silently neutral. ``si_float`` arrives as a decimal
        fraction (``'0.017'``) and is converted to a percentage.

        A static method (E5c, #372) so the Earnings reader can parse a stored row with the very same mapping it
        would have applied to the live answer; ``self._parse_short_row(row)`` is unchanged.
        """
        if not row:
            return {"si_pct": None, "borrow_fee": None, "utilization": None, "days_to_cover": None}

        def _f(v):
            try:
                if v is None or v == "":
                    return None
                return float(v)
            except (TypeError, ValueError):
                return None

        si = _f(row.get("si_float"))
        if si is not None:
            si = si * 100 if abs(si) < 1 else si
        shares = _f(row.get("short_interest"))
        avail = _f(row.get("short_shares_available"))
        util = None
        if shares is not None and avail is not None and shares + avail > 0:
            util = 100.0 * shares / (shares + avail)
        return {
            "si_pct": si,
            "borrow_fee": _f(row.get("fee_rate")),
            "utilization": util,
            "days_to_cover": _f(row.get("days_to_cover")),
        }

    # ── Insider ──
    def insider(self, ticker: str) -> list[dict]:
        return self.insider_result(ticker).data

    def insider_result(self, ticker: str) -> ProviderResult:
        """:meth:`insider` with the request outcome (``ok`` or ``failed``)."""
        return self._shaped(f"/insider/{_seg(ticker)}", None, _rows)

    # ── ETF Holdings ──
    def etf_holdings(self, ticker: str) -> list[dict]:
        """ETF constituents, deduped by ticker (latest `updated` wins), sorted by weight desc."""
        return self.etf_holdings_result(ticker).data

    def etf_holdings_result(self, ticker: str) -> ProviderResult:
        """:meth:`etf_holdings` with the request outcome (``ok`` or ``failed``)."""
        return self._shaped(f"/etfs/{_seg(ticker)}/holdings", None, _holdings)

    # ── OHLC / Volume ──
    # UW's candle endpoints return up to three rows per trading day (pre-market,
    # regular, after-hours) and ``limit`` counts rows, not days. Every method
    # below returns one regular-session bar per day, oldest first
    # (core/providers/ohlc.py; review X2), and over-asks so ``days`` means
    # trading sessions.
    _ROWS_PER_SESSION = 3
    _MAX_LIMIT = 2500

    def _candle_request(self, ticker: str, candle_size: str, days: int) -> tuple[str, dict, bool]:
        daily = candle_size == "1d"
        limit = min(days * self._ROWS_PER_SESSION, self._MAX_LIMIT) if daily else days
        return f"/stock/{_seg(ticker)}/ohlc/{_seg(candle_size)}", {"limit": limit}, daily

    @staticmethod
    def _shape_candles(data, daily: bool, days: int) -> list[dict]:
        items = data.get("data", []) if isinstance(data, dict) else data or []
        if not isinstance(items, list):
            return []
        bars = regular_session_bars(items, sessions_only=daily)
        return bars[-days:] if days else bars

    def _candles(self, ticker: str, candle_size: str, days: int) -> list[dict]:
        return self._candles_result(ticker, candle_size, days).data

    def _candles_result(self, ticker: str, candle_size: str, days: int) -> ProviderResult:
        """:meth:`_candles` that says whether the request worked: ``ok`` (the bars may be
        empty: the provider has none) or ``failed`` (``data`` is an empty placeholder)."""
        ep, params, daily = self._candle_request(ticker, candle_size, days)
        return self._shaped(ep, params, lambda data: self._shape_candles(data, daily, days))

    def ohlc_latest(self, ticker: str) -> dict:
        """The latest completed regular-session daily bar (not an after-hours row)."""
        return self.ohlc_latest_result(ticker).data

    def ohlc_latest_result(self, ticker: str) -> ProviderResult:
        """:meth:`ohlc_latest` with the request outcome: ``ok`` (the bar may be ``{}``: the provider has none)
        or ``failed`` (``data`` is the ``{}`` placeholder)."""
        ep, params, daily = self._candle_request(ticker, "1d", 5)

        def latest(data) -> dict:
            bars = self._shape_candles(data, daily, 5)
            return bars[-1] if bars else {}

        return self._shaped(ep, params, latest)

    def ticker_info(self, ticker: str) -> dict:
        return self.ticker_info_result(ticker).data

    def ticker_info_result(self, ticker: str) -> ProviderResult:
        """:meth:`ticker_info` with the request outcome (``ok`` or ``failed``)."""
        return self._shaped(f"/stock/{_seg(ticker)}/info", None, _obj_or_body)

    def options_volume(self, ticker: str) -> dict:
        return self.options_volume_result(ticker).data

    def options_volume_result(self, ticker: str) -> ProviderResult:
        """:meth:`options_volume` with the request outcome (``ok`` or ``failed``)."""
        return self._shaped(f"/stock/{_seg(ticker)}/options-volume", None, _obj_or_body)

    def calendar_day_raw(self, d: date, session: str) -> list[dict]:
        return self.calendar_day_raw_result(d, session).data

    def calendar_day_raw_result(self, d: date, session: str) -> ProviderResult:
        """:meth:`calendar_day_raw` with the request outcome (``ok`` or ``failed``)."""
        return self._shaped(f"/earnings/{_seg(session)}", {"date": d.isoformat(), "limit": 500}, _rows)

    def economic_calendar(self) -> list[dict]:
        return self.economic_calendar_result().data

    def economic_calendar_result(self) -> ProviderResult:
        """The economic calendar of the current and next week (``/market/economic-calendar``): rows of ``event``,
        ``type`` (fed-speaker, fomc or report), ``time`` (UTC text), ``forecast``, ``prev`` and ``reported_period``."""
        return self._shaped("/market/economic-calendar", None, _list_rows)

    # ── ETF OHLC (used by Rotation Engine) ──
    def etf_ohlc(self, ticker: str, days: int = 70, candle_size: str = "1d") -> list[dict]:
        """``days`` candles, oldest first; daily ones are regular-session bars."""
        return self._candles(ticker, candle_size, days)

    def ohlc_daily(self, ticker: str, days: int = 500) -> list[dict]:
        """Daily regular-session bars for any equity or ETF, oldest first."""
        return self._candles(ticker, "1d", days)

    def etf_ohlc_result(self, ticker: str, days: int = 70, candle_size: str = "1d") -> ProviderResult:
        """:meth:`etf_ohlc` with the request outcome (``ok`` or ``failed``)."""
        return self._candles_result(ticker, candle_size, days)

    def ohlc_daily_result(self, ticker: str, days: int = 500) -> ProviderResult:
        """:meth:`ohlc_daily` with the request outcome (``ok`` or ``failed``)."""
        return self._candles_result(ticker, "1d", days)

    def etf_flow(self, ticker: str) -> dict:
        return self.etf_flow_result(ticker).data

    def etf_flow_result(self, ticker: str) -> ProviderResult:
        """:meth:`etf_flow` with the request outcome (``ok`` or ``failed``)."""
        return self._shaped(f"/stock/{_seg(ticker)}/flow-recent", None, _obj_or_body)

    # ── Screener (batch enrichment for stock volume, options volume, OI) ──
    def screener_batch(self, tickers: list[str], date: str = None) -> dict[str, dict]:
        return self.screener_batch_result(tickers, date).data

    def screener_batch_result(self, tickers: list[str], date: str = None) -> ProviderResult:
        """:meth:`screener_batch` with the outcome of its chunks: ``ok``, ``partial`` (some
        chunks failed; ``error`` names the first) or ``failed`` (all did)."""
        # `limit` must be set — the screener default is ~50, silently
        # dropping the rest of the batch otherwise.
        extra = {"limit": 500}
        if date:
            extra["date"] = date
        return self._batched("/screener/stocks", tickers, "ticker", extra, ("ticker", "symbol"))

    def screener_stocks(self, min_marketcap: float = None, max_marketcap: float = None,
                        issue_types: list[str] = None, page: int = 1,
                        limit: int = 500) -> list[dict]:
        """Market-cap / issue-type filtered stock screener (paginated).

        Returns raw screener rows (ticker, marketcap, sector, close, volumes, OI…).
        """
        return self.screener_stocks_result(min_marketcap, max_marketcap, issue_types, page, limit).data

    def screener_stocks_result(self, min_marketcap: float = None, max_marketcap: float = None,
                               issue_types: list[str] = None, page: int = 1,
                               limit: int = 500) -> ProviderResult:
        """:meth:`screener_stocks` with the request outcome (``ok`` or ``failed``)."""
        params = {"page": page, "limit": limit}
        if min_marketcap is not None:
            params["min_marketcap"] = str(int(min_marketcap))
        if max_marketcap is not None:
            params["max_marketcap"] = str(int(max_marketcap))
        if issue_types:
            params["issue_types[]"] = issue_types
        return self._shaped("/screener/stocks", params, _list_rows)

    # ── Generic passthrough (UW Interaction lab) ──
    def raw_endpoint(self, ep: str, params: dict = None) -> dict | list:
        """Raw passthrough for catalog-defined UW Interaction lab tools.

        For a catalog tool the module already knows the exact endpoint and
        query schema, so it does not need a bespoke typed method per gap family
        (congress, prediction markets, crypto, market state…). Reads through
        ``get_result`` so credential failures still record ``auth_error`` and log
        loudly; ``{}`` on failure. :meth:`raw_endpoint_result` says which it was.
        """
        return self.raw_endpoint_result(ep, params).data

    def raw_endpoint_result(self, ep: str, params: dict = None) -> ProviderResult:
        """:meth:`raw_endpoint` with the request outcome (``ok`` or ``failed``); the body is unshaped."""
        return self._shaped(ep, params, _body)


def _short_row(data) -> dict:
    """The single V2 interest-float row of a body."""
    row = data.get("data") if isinstance(data, dict) else data
    if isinstance(row, list):
        # defensive: treat a list payload as newest-first history like
        # flow-recent; the V2 schema returns a single object.
        row = row[0] if row else {}
    return row if isinstance(row, dict) else {}


def _holdings(data) -> list[dict]:
    """ETF constituents of a body, deduped by ticker (latest ``updated`` wins), by weight desc."""
    by_ticker = {}
    for it in _rows(data):
        t = it.get("ticker")
        if not t:
            continue
        prev = by_ticker.get(t)
        if prev is None or (it.get("updated") or "") > (prev.get("updated") or ""):
            by_ticker[t] = it
    rows = list(by_ticker.values())
    rows.sort(key=lambda x: float(x.get("weight") or 0), reverse=True)
    return rows


def _flow_num(v):
    """Coerce a raw tape field to float, or None when missing/malformed."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _quarterly_by_date(rows: list) -> dict:
    """``fiscal_date_ending`` -> row, for ``_map_financials``. A row that names its period counts only when it is
    ``quarterly``; a row without a period type is kept (the legacy rule) but never replaces a quarterly one."""
    out: dict = {}
    for r in rows or []:
        d = r.get("fiscal_date_ending") or ""
        rt = (r.get("report_type") or "").lower()
        if not d or (rt and rt != "quarterly"):
            continue
        if rt or d not in out:
            out[d] = r
    return out


def _lean_bucket(item: dict, option_type: str) -> str:
    """``package``, ``bull``, ``bear``, ``mid`` or ``none`` for one institutional print (``aggregate_flow``)."""
    if is_package(item):
        return "package"
    side = print_side(item)
    if side in ("ask", "bid"):
        return "bull" if (side == "ask") == (option_type == "call") else "bear"
    return side or "none"


def _empty_flow():
    return {"inst_call": 0, "inst_put": 0, "retail_call": 0, "retail_put": 0,
            "unknown": 0, "sweep": 0, "block": 0, "bullish": 0, "bearish": 0, "inst_net": 0,
            "inst_bull_side": 0, "inst_bear_side": 0, "inst_mid": 0, "inst_no_side": 0, "inst_package": 0,
            "inst_lean_net": 0, "side_served": None}
