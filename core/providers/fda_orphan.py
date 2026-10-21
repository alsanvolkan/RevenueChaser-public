"""FDA OOPD (Office of Orphan Products Development) orphan-drug search.

Fetches the public OOPD sponsor search (``accessdata.fda.gov``), parses the
"Detailed" results page with regexes (no HTML parser in requirements.txt,
and ``sec_edgar.py`` makes the same choice), and matches sponsors against a
SEC filer name through :mod:`core.biotech.sponsor`.

Scope: fetch + parse + sponsor-name matching only. No storage, no job, no UI.
"""
from __future__ import annotations

import html
import logging
import os
import re
import threading
import time
from datetime import date, datetime

import httpx

from .result import ProviderResult
from ..biotech.sponsor import _STATE_SUFFIX_RE, normalise_sponsor

logger = logging.getLogger(__name__)

OOPD_BASE = os.getenv("RC_FDA_OOPD_BASE") or "https://www.accessdata.fda.gov/scripts/opdlisting/oopd"
FDA_USER_AGENT_DEFAULT = "RevenueChaser research"

_THROTTLE_INTERVAL = 1.0
_MAX_RETRIES = 3
_RETRY_AFTER_MAX = 60
_RETRY_BASE = 2.0

_throttle_lock = threading.Lock()
_throttle_last = [0.0]


def _headers() -> dict:
    return {"User-Agent": (os.getenv("RC_FDA_USER_AGENT") or "").strip()
            or FDA_USER_AGENT_DEFAULT}


def _throttle() -> None:
    with _throttle_lock:
        wait = _THROTTLE_INTERVAL - (time.monotonic() - _throttle_last[0])
        if wait > 0:
            time.sleep(wait)
        _throttle_last[0] = time.monotonic()


def _is_retryable_status(status: int) -> bool:
    return 500 <= status < 600 or status == 429


def _retry_delay(resp: httpx.Response, attempt: int) -> float:
    """Honor ``Retry-After`` on a 429, else exponential backoff."""
    if resp is not None and resp.status_code == 429:
        raw = (resp.headers.get("Retry-After") or "").strip()
        if raw:
            try:
                return min(max(0, int(raw)), _RETRY_AFTER_MAX)
            except ValueError:
                pass
    return _RETRY_BASE ** attempt


def _do_request(method: str, url: str, client: httpx.Client, counter: list, **kwargs) -> httpx.Response:
    """One throttled GET/POST with exponential backoff on 5xx/429.

    ``client`` carries the cookie jar between the GET and the POST, so the
    caller always passes one (its own, or one it built for this call).
    ``counter`` is a shared one-element list the caller passes in; it is
    incremented once per HTTP try (even on failure), so the caller can report
    a total attempt count across the GET and the POST in one error dict.

    A 4xx other than 429 raises immediately (no retry). Only
    ``httpx.TransportError`` (connect/read/write/timeout) is retried as a
    network failure; any other exception propagates as-is. Raises the last
    exception/``HTTPStatusError`` once retries are exhausted; callers turn
    that into a ``ProviderResult(status="failed")``.
    """
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        _throttle()
        counter[0] += 1
        try:
            r = getattr(client, method)(url, headers=_headers(), follow_redirects=True, **kwargs)
        except httpx.TransportError as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES - 1:
                time.sleep(_RETRY_BASE ** attempt)
                continue
            raise
        if _is_retryable_status(r.status_code) and attempt < _MAX_RETRIES - 1:
            time.sleep(_retry_delay(r, attempt))
            continue
        r.raise_for_status()
        return r
    raise last_exc or RuntimeError(f"FDA OOPD request failed: {url}")  # pragma: no cover


_FIELD_RE = re.compile(
    r'id="(?P<label>[^"]+?) for Record Number (?P<n>\d+)"[^>]*>.*?</th>\s*<td[^>]*>(?P<val>.*?)</td>',
    re.S,
)
_TOTAL_MARKER_RE = re.compile(r"start=\d+;\s*page=\d+;\s*total=(\d+);")
_NO_RECORDS = "No records found"
_REQUIRED = ("generic_name", "date_designated", "designation", "designation_status", "sponsor")

_LABEL_KEYS = {
    "Generic name": "generic_name",
    "Date designated": "date_designated",
    "Orphan designation": "designation",
    "Orphan designation status": "designation_status",
    "FDA Orphan Approval status": "approval_status",
    "Marketing Approval Date": "marketing_approval_date",
    "Trade Name": "trade_name",
    "Sponsor": "sponsor",
}


def _clean_text(raw: str) -> str:
    text = html.unescape(raw)
    return re.sub(r"\s+", " ", text).strip()


def _text_or_none(raw: str | None) -> str | None:
    if raw is None:
        return None
    return _clean_text(raw) or None


def _sponsor_name(raw_td: str) -> str:
    """The sponsor's name only: text before the first ``<BR/>``/``<br>``."""
    first_piece = re.split(r"(?i)<br\s*/?>", raw_td, maxsplit=1)[0]
    return _clean_text(first_piece)


def _to_iso_date(raw: str) -> str | None:
    text = _clean_text(raw)
    if not text:
        return None
    try:
        return datetime.strptime(text, "%m/%d/%Y").date().isoformat()
    except ValueError:
        logger.warning(f"FDA OOPD: could not parse date {text!r}")
        return None


def parse_results(html_doc: str) -> list[dict]:
    """Parse one OOPD "Detailed" results page into a list of record dicts.

    Pure function: one regex pass, no I/O. The live page reuses one record's
    ``id`` attribute for two different ``<th>`` labels at least once (seen on
    "Marketing Approval Date" / "Approved Labeled Indication"), so the first
    occurrence of a (label, record) pair wins and later duplicates are
    ignored, instead of silently overwriting a real value.

    Each dict has ``generic_name``, ``date_designated`` (ISO), ``designation``,
    ``designation_status``, ``approval_status``, ``marketing_approval_date``
    (ISO or ``None``), ``trade_name`` (``str`` or ``None`` -- only approved
    records carry it) and ``sponsor`` (name only, not the full address block).
    """
    records: dict[int, dict] = {}
    order: list[int] = []
    for m in _FIELD_RE.finditer(html_doc):
        key = _LABEL_KEYS.get(m.group("label"))
        if key is None:
            continue
        n = int(m.group("n"))
        if n not in records:
            records[n] = {}
            order.append(n)
        if key not in records[n]:
            records[n][key] = m.group("val")

    rows = []
    for n in order:
        raw = records[n]
        date_raw = raw.get("date_designated")
        approval_date_raw = raw.get("marketing_approval_date")
        approval_status_raw = raw.get("approval_status")
        trade_name_raw = raw.get("trade_name")
        sponsor_raw = raw.get("sponsor")
        rows.append({
            "generic_name": _text_or_none(raw.get("generic_name")),
            "date_designated": _to_iso_date(date_raw) if date_raw is not None else None,
            "designation": _text_or_none(raw.get("designation")),
            "designation_status": _text_or_none(raw.get("designation_status")),
            "approval_status": _text_or_none(approval_status_raw),
            "marketing_approval_date": _to_iso_date(approval_date_raw) if approval_date_raw is not None else None,
            "trade_name": _text_or_none(trade_name_raw),
            "sponsor": (_sponsor_name(sponsor_raw) or None) if sponsor_raw is not None else None,
        })
    return rows


def _is_complete(row: dict) -> bool:
    return all(row.get(k) for k in _REQUIRED)


def _total(doc: str) -> int | None:
    """The result count from the page's JS pager (``start=N; page=N; total=N;``).

    Every real results page carries that pager, zero-hit pages included. A page
    without it (dropped session cookie, maintenance or bot page, markup change)
    is unrecognised: ``None``. Looser markers (``Designations: N``, any
    ``total=``) are not accepted, because other scripts on a page can match them.
    """
    m = _TOTAL_MARKER_RE.search(doc)
    return int(m.group(1)) if m else None


def _search_form(query: str) -> dict:
    return {
        "Product_name": "",
        "sponsor_name": query,
        "Designation": "",
        "Designation_Start_Date": "01/01/1983",
        "Designation_End_Date": date.today().strftime("%m/%d/%Y"),
        "Search_param": "DESDATE",
        "Output_Format": "Detailed",
        "Sort_order": "GENERIC_NAME",
        "RecordsPerPage": "500",
        "newSearch": "Run Search",
    }


def _failed(endpoint: str, status: int | None, error: str, detail: str, attempts: int) -> ProviderResult:
    return ProviderResult("failed", [], {
        "endpoint": endpoint,
        "status": status,
        "error": error,
        "detail": detail,
        "attempts": attempts,
    }, endpoint)


def search_sponsor(query: str, client=None) -> ProviderResult:
    """Search OOPD by sponsor name: cookie GET, then the search POST.

    ``status="failed"`` on an empty query (no request made), a network/HTTP
    failure, an unrecognised page (no session cookie), or a page that claims
    hits but yields no parsed rows. ``status="partial"`` when the page
    reports more total records than were parsed from it (OOPD paginates);
    ``status="ok"`` otherwise (``data`` is the parsed row list).
    """
    endpoint = f"{OOPD_BASE}/OOPD_Results.cfm"
    if not query or not query.strip():
        return _failed(endpoint, None, "empty_query", "empty sponsor query", 0)

    counter = [0]
    owns_client = client is None
    if owns_client:
        client = httpx.Client(timeout=60)
    try:
        _do_request("get", f"{OOPD_BASE}/index.cfm", client, counter)
        resp = _do_request("post", endpoint, client, counter, data=_search_form(query))
    except Exception as exc:  # noqa: BLE001 - any failure here is terminal
        status = getattr(getattr(exc, "response", None), "status_code", None)
        logger.warning(f"FDA OOPD search_sponsor({query!r}): {type(exc).__name__}: {exc}")
        return _failed(endpoint, status, type(exc).__name__, str(exc), counter[0])
    finally:
        if owns_client:
            client.close()

    body = resp.text
    total = _total(body)
    if total is None:
        return _failed(endpoint, None, "unrecognised_page",
                        "no total=/Designations: marker found (dropped session cookie?)", counter[0])

    if total == 0 and _NO_RECORDS not in body:
        return _failed(endpoint, None, "unrecognised_page",
                        f"total=0 without the {_NO_RECORDS!r} text", counter[0])

    parsed = parse_results(body)
    rows = [r for r in parsed if _is_complete(r)]
    if total > 0 and not rows:
        return _failed(endpoint, None, "parse_failed",
                        f"page reports total={total} but no complete rows parsed", counter[0])
    if len(rows) < len(parsed):
        logger.warning(f"FDA OOPD search_sponsor({query!r}): {len(parsed) - len(rows)} incomplete row(s) dropped")
        return ProviderResult("partial", rows, None, endpoint)
    if total > len(rows):
        return ProviderResult("partial", rows, None, endpoint)
    return ProviderResult("ok", rows, None, endpoint)


def sponsor_query(sec_name: str) -> str:
    """A short OOPD sponsor-search query derived from a raw SEC filer name.

    OOPD's sponsor search is a substring match, so a long, punctuation-heavy
    SEC filer name (``"Y-mAbs Therapeutics, Inc."``) is more likely to find
    nothing than a short distinctive piece of it (``"Y-mAbs"``). Strips a
    trailing EDGAR state suffix, then takes the first whitespace-split token
    if it carries at least 4 alphanumeric characters (distinctive enough on
    its own), else the first two tokens joined by a space; a leading "The"
    is skipped. Internal hyphens/slashes are kept (``"Y-mAbs"``), only
    leading/trailing ``,.;:()`` are stripped per token.
    """
    if not sec_name:
        return ""
    text = _STATE_SUFFIX_RE.sub("", sec_name)
    tokens = [t.strip(",.;:()") for t in text.split()]
    tokens = [t for t in tokens if t]
    if tokens and tokens[0].lower() == "the":
        tokens = tokens[1:]
    if not tokens:
        return ""

    first = tokens[0]
    alnum_count = sum(1 for c in first if c.isalnum())
    if alnum_count >= 4 or len(tokens) < 2:
        query = first
    else:
        query = f"{tokens[0]} {tokens[1]}"

    if not normalise_sponsor(query) or sum(1 for c in query if c.isalnum()) < 3:
        return ""
    return query


def designations_for(sec_name: str, client=None) -> ProviderResult:
    """Orphan designations for a SEC filer name, matched by normalised sponsor.

    ``data`` is always ``{"match", "rows", "candidates", "query"}``:
    ``match`` is ``"matched"`` (``rows`` holds only the matching records),
    ``"unmatched"`` (hits exist but none match; ``candidates`` lists the
    sponsors seen), ``"no_hits"`` (no OOPD sponsor name contains the query
    text; this is not proof the company has no designations, e.g. a filing
    under a subsidiary or a different spelling) or
    ``None`` (the query was empty, or the search failed -- see ``error``).
    """
    query = sponsor_query(sec_name)
    if not query:
        return ProviderResult("failed", {"match": None, "rows": [], "candidates": [], "query": query}, {
            "endpoint": "",
            "status": None,
            "error": "empty_query",
            "detail": "empty sponsor query",
            "attempts": 0,
        }, "")

    result = search_sponsor(query, client=client)
    if result.status == "failed":
        return ProviderResult("failed", {"match": None, "rows": [], "candidates": [], "query": query},
                               result.error, result.endpoint)

    rows = result.data
    target = normalise_sponsor(sec_name)
    if not rows:
        data = {"match": "no_hits", "rows": [], "candidates": [], "query": query}
    else:
        matched = [r for r in rows if normalise_sponsor(r.get("sponsor", "")) == target]
        candidates = sorted({r.get("sponsor", "") for r in rows
                              if normalise_sponsor(r.get("sponsor", "")) != target})
        if matched:
            data = {"match": "matched", "rows": matched, "candidates": candidates, "query": query}
        else:
            data = {"match": "unmatched", "rows": [], "candidates": candidates, "query": query}

    return ProviderResult(result.status, data, None, result.endpoint)
