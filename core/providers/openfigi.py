"""OpenFIGI CUSIP mapping client (#207 F1, D19): ``POST /v3/mapping`` with one ``ID_CUSIP`` job per CUSIP.

Each job is ``{"idType": "ID_CUSIP", "idValue": <cusip>, "exchCode": "US"}``: the US composite listing, the one the
app's tickers name (the identity adapter, ``core/identity/sources/openfigi.py``, asks its TICKER jobs the same way).
A request carries at most 100 jobs, and requests are spaced to stay under 25 per 6 s: the keyed limits (OpenFIGI
documentation, read on the PC 2026-10-03). The key is ``OPENFIGI_API_KEY`` (the PC ``.env``, read through
:mod:`core.credentials`). It is sent only in the ``X-OPENFIGI-APIKEY`` header and never logged or returned. Without
a key the client asks nothing: the keyless limits (10 jobs per request, 25 requests a minute) need ten times the
requests for the same map.

**Answers.** A job answered with ``data`` is 'present': the first US entry with a ticker gives the ticker (in the
app's spelling, ``BRK/B`` -> ``BRK-B``), FIGI, share-class FIGI, name, exchange code and security type. A job answered
with a ``warning`` ("No identifier found.") is 'absent'. A job answered with an ``error`` is neither: it is counted in
``job_errors`` and stays unasked for the store, so the next run asks it again.

**Limits.** A 429 is waited out (``ratelimit-reset`` seconds, plus one) and retried, as
``scripts/research/35_identity_r1.py`` does. Every HTTP request, a retried one included, counts toward
``max_requests``: 400 per run, avo's cap for #207 (typed 2026-10-14). The run stops at the cap or at the first
failed request. The CUSIPs it did not ask are returned in ``unasked``, never guessed.

Base URL: ``RC_OPENFIGI_BASE`` (default ``https://api.openfigi.com``), read at call time so tests and the e2e harness
can redirect it. The client does no store I/O; :mod:`core.identity_cusip` writes the answers.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

from RevenueChaser.core import credentials
from RevenueChaser.core.providers.result import ProviderHealth

PROVIDER = "openfigi"
ENDPOINT = "/v3/mapping"
DEFAULT_BASE = "https://api.openfigi.com"
JOBS_PER_REQUEST = 100      # with a key
REQUEST_GAP_S = 0.25        # 25 requests / 6 s with a key = one per 0.24 s
MAX_REQUESTS = 400          # per run, avo's cap for #207 (typed 2026-10-14, B6)
MAX_ATTEMPTS = 8            # per batch: the first request and up to 7 retries after a 429
_LOW_REMAINING = 5          # wait out the window when fewer requests than this remain in it
PRESENT, ABSENT = "present", "absent"

# OpenFIGI's suffix -> the app's; the inverse of the identity adapter's ``_SUFFIXES`` (units, warrants). Any other
# class separator is a slash there and a dash here.
_SUFFIXES = (("/U", "-UN"), ("/WS", "-WT"))


def _sleep(seconds: float) -> None:
    """The one wait of this module (tests replace it)."""
    time.sleep(seconds)


def _post(url: str, jobs: list[dict], headers: dict):
    """One mapping request; returns the ``httpx`` response (tests replace it with a fake)."""
    import httpx
    return httpx.post(url, json=jobs, headers=headers, timeout=90)


def base_url() -> str:
    return (os.getenv("RC_OPENFIGI_BASE") or DEFAULT_BASE).strip().rstrip("/")


def app_ticker(figi_ticker: str | None) -> str | None:
    """OpenFIGI's ticker in the app's spelling: ``BRK/B`` -> ``BRK-B``, ``AAC/U`` -> ``AAC-UN``."""
    t = (figi_ticker or "").strip().upper()
    if not t:
        return None
    for theirs, ours in _SUFFIXES:
        if t.endswith(theirs):
            return t[: -len(theirs)] + ours
    return t.replace("/", "-")


@dataclass
class MapResult:
    """What one mapping run produced.

    ``status``: ``ok`` (every CUSIP was asked and answered), ``partial`` (stopped at the cap or a failed request
    after some answers), ``failed`` (stopped before any answer) or ``skipped`` (no key, nothing asked).
    ``answers``: ``{cusip: answer}`` for the CUSIPs OpenFIGI answered (``status`` 'present' or 'absent').
    ``unasked``: the CUSIPs not asked, in the order given. ``requests`` counts every HTTP request (429 retries
    included), ``rate_limited`` the 429s among them. ``error`` is redacted (never the key).
    """

    status: str
    answers: dict[str, dict] = field(default_factory=dict)
    unasked: list[str] = field(default_factory=list)
    requests: int = 0
    rate_limited: int = 0
    job_errors: int = 0
    error: str | None = None

    @property
    def complete(self) -> bool:
        return self.status == "ok"


def _redact(message, key: str) -> str:
    text = str(message)
    return text.replace(key, "<redacted>") if key else text


def _wait_seconds(resp) -> float:
    try:
        return float(resp.headers.get("ratelimit-reset", "10") or 10) + 1
    except (TypeError, ValueError):
        return 11.0


def _answer(entry: dict, fetched_at: float) -> dict | None:
    """One job's answer as a store row body, or ``None`` for a job-level error."""
    data = (entry or {}).get("data")
    if data:
        hit = next((d for d in data if (d or {}).get("ticker")), data[0]) or {}
        return {"status": PRESENT, "ticker": app_ticker(hit.get("ticker")), "figi": hit.get("figi"),
                "share_class_figi": hit.get("shareClassFIGI"), "share_class": None, "name": hit.get("name"),
                "exch_code": hit.get("exchCode"), "security_type": hit.get("securityType"),
                "fetched_at": fetched_at}
    if (entry or {}).get("error"):
        return None
    return {"status": ABSENT, "ticker": None, "figi": None, "share_class_figi": None, "share_class": None,
            "name": None, "exch_code": None, "security_type": None, "fetched_at": fetched_at}


def map_cusips(cusips: Iterable[str], *, key: str | None = None, max_requests: int = MAX_REQUESTS,
               health: ProviderHealth | None = None, clock: Callable[[], float] = time.time) -> MapResult:
    """Ask OpenFIGI for each CUSIP's US listing, in the order given (the caller puts the largest holdings first).

    ``key`` defaults to ``OPENFIGI_API_KEY``. ``health`` (optional) counts each HTTP request and its failure, in the
    shape the board banners read. Never raises: a failure ends the run as ``partial`` or ``failed``.
    """
    todo = list(dict.fromkeys(str(c).strip().upper() for c in cusips if str(c or "").strip()))
    if not todo:
        return MapResult("ok")
    key = (credentials.key_for(PROVIDER) if key is None else key).strip()
    if not key:
        return MapResult("skipped", unasked=todo, error="no OpenFIGI key (OPENFIGI_API_KEY)")
    url = base_url() + ENDPOINT
    headers = {"Content-Type": "application/json", "X-OPENFIGI-APIKEY": key}
    res = MapResult("ok")

    def _stop(i: int, error: str) -> MapResult:
        res.unasked = todo[i:]
        res.error = _redact(error, key)
        res.status = "partial" if res.answers else "failed"
        return res

    for i in range(0, len(todo), JOBS_PER_REQUEST):
        chunk = todo[i:i + JOBS_PER_REQUEST]
        jobs = [{"idType": "ID_CUSIP", "idValue": c, "exchCode": "US"} for c in chunk]
        resp = None
        for attempt in range(MAX_ATTEMPTS):
            if res.requests >= max_requests:
                return _stop(i, f"stopped at the cap of {max_requests} OpenFIGI requests")
            if res.requests:
                _sleep(REQUEST_GAP_S)
            res.requests += 1
            try:
                resp = _post(url, jobs, headers)
            except Exception as exc:  # noqa: BLE001 - the run ends here, reported, never raised
                if health is not None:
                    health.call(error=_redact(f"{type(exc).__name__}: {exc}", key), endpoint=ENDPOINT)
                return _stop(i, f"OpenFIGI request failed: {type(exc).__name__}: {exc}")
            if resp.status_code != 429:
                break
            res.rate_limited += 1
            if health is not None:
                health.call(error="HTTP 429", endpoint=ENDPOINT)
            if attempt < MAX_ATTEMPTS - 1:
                _sleep(_wait_seconds(resp))
        if resp.status_code != 200:
            if resp.status_code != 429 and health is not None:
                health.call(error=f"HTTP {resp.status_code}", endpoint=ENDPOINT)
            return _stop(i, f"OpenFIGI HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError:
            body = None
        if not isinstance(body, list) or len(body) != len(chunk):
            if health is not None:
                health.call(error="unexpected body", endpoint=ENDPOINT)
            return _stop(i, "OpenFIGI returned an unexpected body")
        if health is not None:
            health.call(endpoint=ENDPOINT)
        fetched_at = clock()
        for cusip, entry in zip(chunk, body):
            answer = _answer(entry, fetched_at)
            if answer is None:
                res.job_errors += 1
            else:
                res.answers[cusip] = answer
        remaining = resp.headers.get("ratelimit-remaining")
        if remaining is not None and str(remaining).isdigit() and int(remaining) < _LOW_REMAINING:
            _sleep(_wait_seconds(resp))
    return res
