"""What every builder dataset shares: the run context, the HTTP helper, raw and Parquet files, the pacer, the gates,
and ``run_request``, the one fetch → type → build → swap sequence.

A dataset (``Dataset``) has four steps:

- ``fetch(ctx, as_of)`` downloads raw files under ``<MARKET_RAW_DIR>/raw/<source>/<dataset>/...``. A raw file that
  exists is never fetched again: it is the resume marker. Raw files are written to ``<name>.tmp`` and renamed, so a
  file under its real name is always whole.
- ``to_parquet(ctx, raw)`` types a raw file into its Parquet partition under ``<MARKET_RAW_DIR>/parquet/...``, also
  through ``.tmp`` and a rename (``write_parquet``), so a partition that exists is complete; ``to_parquet_all`` does
  a run's raw files together, so a partition several of them touch is written once.
- ``build(ctx, con)`` creates the dataset's tables in the new serving file from its Parquet (no network).
- ``check(ctx, con)`` compares the new tables with the live file and returns the ``dataset_check`` row.

``run_request`` takes one queued request: it skips a paused dataset and refuses a provider at its day cap (a
``skipped`` check either way, no call), runs the dataset's fetch and typing, builds the whole serving kind from
Parquet (every dataset of the kind, so one dataset's run never drops another's tables), swaps it in, and writes the
check row, the run row, the provider call counts and UW's rate headers. An exception anywhere is a ``failed`` check row; the live file is untouched.
"""
from __future__ import annotations

import contextlib
import contextvars
import gzip
import json
import logging
import os
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

import duckdb
import httpx

from ...core import calendar, config, job_lock, rawstore
from .. import caps, ops, serving

logger = logging.getLogger(__name__)

# Keyless sources: a small retry ladder of their own (429, 5xx and transport errors), every attempt counted.
HTTP_ATTEMPTS = 3
HTTP_BACKOFF_S = 2.0
HTTP_TIMEOUT_S = 60.0
USER_AGENT = "RevenueChaser market-data builder"
# A contact for the User-Agent (BLS refuses clients without one): from the environment, never written in the code.
CONTACT_ENV = "RC_HTTP_CONTACT"
# The builder's own job-lock name: a second builder is refused while one runs.
BUILDER_LOCK = "rcdata_build"
PARQUET_COMPRESSION = "zstd"
PARQUET_ROW_GROUP = 100_000
# The call counter is written to ops.sqlite every this many calls of a provider (and at the end of the run, and on
# a kill), so a run killed mid-way still leaves its calls counted against the day cap (S1).
COUNT_FLUSH_EVERY = 25
# UW politeness pacing (#519 finding 8). Politeness, not a provider limit (``caps.py``): UW publishes no request
# window, but every answer says how many requests its minute has left and when it resets. At or below the floor the
# run waits that reset, capped, before its next request, instead of running into the 429 ladder.
UW_POLITE_FLOOR = 5
UW_POLITE_MAX_S = 60.0
_uw_pause = time.sleep          # patch point for tests


class SourceError(RuntimeError):
    """A provider answer the dataset cannot use (an HTTP failure after retries, an empty or malformed body)."""


class NothingInScope(SourceError):
    """The request's scope names nothing this dataset can ask for: a ``skipped`` check row, not a failure.

    ``tiers=["B"]`` on a float dataset is a legitimate request whose true answer is "a fund has no float", and a
    failed row blaming the spine reads on the Data screen as a broken build (review F4 of #294).
    """


class CapReached(SourceError):
    """A provider reached its day cap during the run: nothing more is sent to it today."""


class Paused(BaseException):
    """The dataset was paused while its run was going (#492 PR (ii)): the run stops before its next provider call.

    A ``BaseException``, like ``KeyboardInterrupt``: several datasets catch ``SourceError`` or ``Exception`` per
    name or per file and go on with the next one, and a pause must stop the whole run, not one name. ``_run``
    catches it by name and ends the run ``skipped``. Limits, by design:

    - a build that has started finishes, so a serving file is never half-written (the pause is read only at a
      provider call and between the fetch and the build);
    - a dataset with no ``check_cap`` inside its fetch (WRDS runs SQL) stops only at the fetch/build gap;
    - the raw and Parquet files fetched before the pause stay; nothing deletes them;
    - a request a dead builder left ``running`` is still reclaimed by ``ops.fail_orphans`` at the next start.
    """


#: A running fetch reads its pause at most this often (seconds of the run's own clock), not once per call.
PAUSE_READ_EVERY_S = 5.0


class NotIdle(RuntimeError):
    """Another job holds a job lock, or another builder runs: this run does not start."""


def user_agent() -> str:
    """The one User-Agent of the builder's keyless requests, with ``RC_HTTP_CONTACT`` when it is set."""
    contact = (os.getenv(CONTACT_ENV) or "").strip()
    return f"{USER_AGENT} ({contact})" if contact else f"{USER_AGENT} (no contact: set {CONTACT_ENV})"


def raw_root() -> Path:
    return Path(config.MARKET_RAW_DIR) / "raw"


def parquet_root() -> Path:
    return Path(config.MARKET_RAW_DIR) / "parquet"


# ── What one request asks for ──────────────────────────────────────────────────────────────────────────────────

# The spine's tiers, in the order they are reported (``rcdata/build/spine/tier.py``).
TIERS = ("A", "B", "C", "D")
# More names than the whole listed universe (about 7,700) is a mistake, not an ask. Checked here, where the one
# reader is, so the CLI's ``--names`` and a hand-written queue row are bounded too (review F5 of #294).
NAMES_MAX = 20_000
# The named groups a request may ask for besides the tiers and the names: the benchmarks and the rotation ETFs
# (``anchors``), the stock watch list, and the other listed classes of a Tier A issuer. A request that names
# nothing is the daily run and gets all three; a narrowed one gets only what it names (avo's Karar 82,
# 30 Oct 2026, review F2 of #294).
SCOPE_GROUPS = ("anchors", "watchlist", "cover_classes")


@dataclass(frozen=True)
class Scope:
    """The names one request asks for: spine tiers, an explicit list, or neither (the dataset's own default).

    A name list wins over the tiers: a person naming ten tickers means those ten, not those ten within a tier.
    Empty (``not scope``) is the daily run, which asks whatever the dataset asks by itself.
    """
    tiers: tuple[str, ...] = ()
    names: tuple[str, ...] = ()
    include: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.tiers or self.names or self.include)

    def text(self) -> str:
        parts = [f"tiers {'+'.join(self.tiers)}" if self.tiers else "",
                 f"{len(self.names)} name{'s' if len(self.names) != 1 else ''}" if self.names else "",
                 "+".join(self.include) if self.include else ""]
        return ", ".join(p for p in parts if p) or "no scope"


def parse_scope(params: dict) -> Scope:
    """``{"tiers": ["A", "B"], "names": ["KO", "MSFT"]}`` of a request's params, normalised.

    Upper-cased, de-duplicated, the tiers in ``TIERS`` order, so two requests for the same set are the same scope.
    An unknown tier or an empty name is a ``ValueError``: the request is refused with a note instead of fetching
    something nobody asked for.
    """
    raw_tiers = params.get("tiers") or ()
    raw_names = params.get("names") or ()
    raw_include = params.get("include") or ()
    for value, what in ((raw_tiers, "tiers"), (raw_names, "names"), (raw_include, "include")):
        if isinstance(value, str) or not isinstance(value, (list, tuple)):
            raise ValueError(f"{what} must be a list, not {type(value).__name__}")
    tiers = {str(t).strip().upper() for t in raw_tiers}
    unknown = sorted(t for t in tiers if t not in TIERS)
    if unknown:
        raise ValueError(f"unknown tier {', '.join(unknown)}: the spine has {', '.join(TIERS)}")
    if len(raw_names) > NAMES_MAX:
        raise ValueError(f"names holds {len(raw_names)} names, more than {NAMES_MAX}")
    names, seen = [], set()
    for n in raw_names:
        name = str(n).strip().upper()
        if not name:
            raise ValueError("a name in names is empty")
        if name not in seen:
            seen.add(name)
            names.append(name)
    include = {str(g).strip().lower() for g in raw_include}
    unknown = sorted(g for g in include if g not in SCOPE_GROUPS)
    if unknown:
        raise ValueError(f"unknown include group {', '.join(unknown)}: one of {', '.join(SCOPE_GROUPS)}")
    return Scope(tiers=tuple(t for t in TIERS if t in tiers), names=tuple(names),
                 include=tuple(g for g in SCOPE_GROUPS if g in include))


def honours_scope(ds) -> bool:
    """Whether the dataset asks the names a request's scope names. A dataset whose names come from somewhere else
    (the SEC's daily index, a whole-market page, a calendar day) declares nothing and gets the note instead."""
    return bool(getattr(ds, "honours_scope", False))


# ── The run context ────────────────────────────────────────────────────────────────────────────────────────────

@dataclass
class RunContext:
    run_id: str
    dataset: str
    trigger: str = "cli"
    fetched_at: float = field(default_factory=time.time)
    sleep: Callable[[float], Any] = time.sleep
    clock: Callable[[], float] = time.monotonic
    as_of: date | None = None
    calls: dict[str, list[int]] = field(default_factory=dict)       # provider -> [calls, failures, http_429]
    notes: list[str] = field(default_factory=list)
    uw_sampled: bool = False
    allow_shrink: bool = False                                      # a request's explicit "allow_shrink": true
    scope: Scope = field(default_factory=Scope)                     # a request's "tiers" / "names" (empty: daily)
    pending: dict[str, Any] = field(default_factory=dict)          # a build's results for its after_swap step
    # #446: the Parquet files changed since the live file was built (``dirty.plan``; None: not known), and the plan
    # of a build made on the live copy (None: a whole build, every table made from the whole Parquet tree).
    changed: Any = None
    incremental: Any = None
    _pending: dict[str, list[int]] = field(default_factory=dict)    # counted, not yet written to ops.sqlite
    _http: httpx.Client | None = None
    _uw: Any = None
    _pause_read_at: float | None = None                             # the clock when the pause was last read
    _paused: str | None = None                                      # once seen, every later call stops too

    def count(self, provider: str, calls: int = 0, failures: int = 0, http_429: int = 0) -> None:
        for book in (self.calls, self._pending):
            c = book.setdefault(provider, [0, 0, 0])
            c[0] += calls
            c[1] += failures
            c[2] += http_429
        if self._pending[provider][0] >= COUNT_FLUSH_EVERY:
            self.flush_counts(provider)

    def flush_counts(self, provider: str | None = None) -> None:
        """Write the pending counts (of ``provider``, or all) to ``provider_calls_daily``."""
        for name in [provider] if provider else sorted(self._pending):
            calls, failures, http_429 = self._pending.get(name, [0, 0, 0])
            if calls or failures or http_429:
                # Written first, dropped after: a store error (a locked ops.sqlite) keeps the counts pending for
                # the next flush, so the day cap never loses them (#519 finding 4).
                ops.count_calls(name, calls, failures=failures, http_429=http_429)
            self._pending.pop(name, None)

    def check_pause(self, *, force: bool = False) -> None:
        """Raise ``Paused`` when the dataset was paused since the run began. The schedule is read at most once
        every ``PAUSE_READ_EVERY_S`` of the run's clock (``force`` reads now); a pause once seen stays seen."""
        if self._paused is None:
            now = self.clock()
            if not force and self._pause_read_at is not None and now - self._pause_read_at < PAUSE_READ_EVERY_S:
                return
            self._pause_read_at = now
            off = ops.switch_off("fetch")          # #527: the board's fetch switch stops every run, the same way
            if off is not None:
                self._paused = f"stopped, {ops.switch_words('fetch', off)}"
                raise Paused(self._paused)
            row = ops.schedule_get(self.dataset)
            if not row or row.get("paused_at") is None:
                return
            self._paused = f"stopped, {_paused_words(row)}"
        raise Paused(self._paused)

    def check_cap(self, provider: str) -> None:
        """Raise ``Paused`` when the dataset was paused during the run (``check_pause``), then ``CapReached`` when
        today's calls of ``provider``, written and pending, reached its cap."""
        self.check_pause()
        cap = caps.cap_for(provider)
        if cap is None:
            return
        used = ops.calls_today(provider) + self._pending.get(provider, [0])[0]
        if used >= cap:
            raise CapReached(f"day cap reached: {provider} {used}/{cap}")

    def total_calls(self) -> int:
        return sum(c[0] for c in self.calls.values())

    def issue(self, kind: str, key: str | None, detail: str | None) -> None:
        logger.warning("%s %s %s: %s", self.dataset, kind, key or "", detail or "")
        ops.record_issue(self.dataset, kind, key, detail, run_id=self.run_id)

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=HTTP_TIMEOUT_S, follow_redirects=True,
                                      headers={"User-Agent": user_agent()})
        return self._http

    def uw_client(self):
        """One UW client per run (its retry, 429 handling and breaker), with the header and call observer."""
        if self._uw is None:
            from ...core.providers.unusual_whales import UWClient
            # The client's own backoff sleep (``unusual_whales._sleep``), not the context's: its retry ladder
            # stays exactly what every other UW caller gets.
            self._uw = UWClient()
            self._uw.on_request = self._on_uw_request
            self._uw.on_response = self._on_uw_response
        return self._uw

    def _on_uw_request(self, ep: str) -> None:
        # Counted on send, as the keyless path is: a request cut off by a timeout was still made and UW counted
        # it, and the client retries it (#519 finding 3).
        self.count("uw", calls=1)

    def _on_uw_response(self, ep: str, response) -> None:
        from ...core.providers.unusual_whales import is_rejected
        status = getattr(response, "status_code", 0)
        if status == 429:
            self.count("uw", http_429=1)
        if is_rejected(status):
            # A rejected request is final (never retried) and never reaches ``health()``, so ``close()`` does not
            # see it: counted here, once, for every UW dataset alike (#519 finding 2). The rest are ``close()``'s.
            self.count("uw", failures=1)
        headers = getattr(response, "headers", None) or {}
        if not self.uw_sampled and ops.sample_uw_headers(headers, run_id=self.run_id):
            self.uw_sampled = True
        try:
            left = int(headers.get(ops.UW_MINUTE_REMAINING_HEADER))
            reset_s = int(headers.get(ops.UW_MINUTE_RESET_HEADER)) / 1000
        except (TypeError, ValueError):
            return                                          # no header, or not a number: nothing to pace off
        # Not on a 429: the client's Retry-After backoff already waits before its retry (review of #570).
        if status != 429 and left <= UW_POLITE_FLOOR and reset_s > 0:
            _uw_pause(min(reset_s, UW_POLITE_MAX_S))

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None
        if self._uw is not None:
            health = self._uw.health()
            # Final failures; a call the breaker refused sent nothing, so it is not a provider failure here. A
            # rejected request (``is_rejected``) is not in ``health()`` and was counted by ``_on_uw_response``.
            self.count("uw", failures=max(0, health["failed"] - health["skipped_calls"]))
            self._uw.close()
            self._uw = None


# An object sec.gov does not hold under ``/Archives/`` answers **403**, not 404: that bucket grants the read of an
# object and not the listing, so S3 answers ``AccessDenied`` for one that is not there (measured 2026-10-30:
# ``master.20261012.idx``, Columbus Day, and a made-up file name; 200 for the open days around them). The SEC's own
# refusal — an undeclared User-Agent, or the fair-access rate — is also a 403, but Akamai's HTML page ("Your Request
# Originates from an Undeclared Automated Tool"), never the XML error. So the answer's own body tells an object the
# host does not hold from a refusal that must stop a run; the day's place in the calendar does not, because the
# closures are not only the statutory holidays (24 and 26 December 2025 were closed by executive order), and neither
# does its age. ``/files/`` (nginx) and ``data.sec.gov`` answer a real 404, which is covered here too.
MISSING_CODE = b"<Code>AccessDenied</Code>"


def is_missing(r: httpx.Response) -> bool:
    """Whether the answer says the host holds no object for the URL: a 404, or a bucket's XML ``AccessDenied``."""
    if r.status_code == 404:
        return True
    return (r.status_code == 403 and "xml" in str(r.headers.get("content-type") or "").lower()
            and MISSING_CODE in r.content)


MISSING_RUN = 3     # consecutive open weekdays with no object: a refusal of the whole path, not that many days
# Open weekdays in stretches one run lets pass after the path handed over a kept object (#507); isolated days do not
# count. A chosen
# ceiling, not a measurement: the calendar explains every absent index day 2014 onwards that was measured, so a run
# that finds more than this is looking at something the calendar and the probe cannot account for, and stops.
UNEXPLAINED_MAX = 10


# Stretches EDGAR keeps no daily index for although it was open, so no calendar explains them: (first, last, name).
# Measured 2026-11-08 (#507) by asking for every weekday index 2014-01-01..2026-11-04: all 65 weekdays of 2014 Q2 are
# absent, the only such stretch. A walk writes these days off as it does a closed one; their filings are not in the
# daily index at all.
INDEX_GAPS: tuple[tuple[date, date, str], ...] = (
    (date(2014, 4, 1), date(2014, 6, 30), "2014 Q2"),
)


def index_gap(day: date) -> str | None:
    """The name of the known gap in EDGAR's daily index that ``day`` falls in, None for a day outside every one."""
    for first, last, name in INDEX_GAPS:
        if first <= day <= last:
            return name
    return None


def open_weekdays(first: date, last: date) -> list[date]:
    """The days from ``first`` to ``last`` EDGAR was open: weekdays that are no federal holiday."""
    out, day = [], first
    while day <= last:
        if day.weekday() < 5 and not calendar.is_federal_holiday(day):
            out.append(day)
        day += timedelta(days=1)
    return out


def stretch(unexplained: list[date | None], limit: int = MISSING_RUN) -> bool:
    """Whether the last ``limit`` entries of a walk's unexplained days are contiguous open weekdays. ``None`` marks
    where a stretch the probe answered for ended, so it can never be part of the next one."""
    tail = unexplained[-limit:]
    if len(tail) < limit or None in tail:
        return False
    run = sorted(tail)
    return open_weekdays(run[0], run[-1]) == run


class MissingStreak:
    """The open weekdays one run found no object for, in a reader that walks a day at a time.

    ``is_missing`` reads the answer's body, which separates the SEC's own refusal from an object the bucket does not
    hold — it cannot separate that from a path or bucket that answers ``AccessDenied`` for *everything* (a wrong
    ``RC_SEC_WWW_BASE``, an EDGAR path change, a bucket-policy change). So a reader that writes a day off on that
    answer alone would walk a wholesale refusal and never ask those days again. A day EDGAR was closed on is
    written off; a day nothing explains is only noted, and ``MISSING_RUN`` of them in a row stops the run with
    nothing written off, so the next run asks them again.

    ``what`` names the thing in the notes and the error ("daily index"). One streak per walk.

    ``probe`` (#507), when given, asks the path for an object it is known to hold before a stretch stops the run: a
    real answer says the path is not refusing everything, so the stretch is days the archive truly has no object for
    -- a closure the calendar does not know yet -- and the walk goes on instead of stopping at the same place every
    run. Those days are still not written off here. More than ``UNEXPLAINED_MAX`` of them in one run stops it anyway.
    """

    def __init__(self, what: str, limit: int = MISSING_RUN, probe: Callable[[], bool] | None = None) -> None:
        self.what, self.limit, self.probe = what, limit, probe
        self.unexplained: list[date | None] = []      # None: a stretch the probe answered for ends here
        self.passed = 0                                # days in stretches the probe answered for, this run

    @staticmethod
    def closed(day: date) -> bool:
        """Whether EDGAR was closed on ``day``: a weekend, or a federal holiday (not an NYSE one)."""
        return day.weekday() >= 5 or calendar.is_federal_holiday(day)

    def missing(self, ctx: "RunContext", day: date) -> bool:
        """``day`` has no object. True when that is explained and may be written off; False when it is not, and a
        stretch of ``limit`` unexplained open weekdays raises instead of writing any of them off."""
        if self.closed(day):
            ctx.notes.append(f"{day}: no {self.what}, EDGAR closed")
            return True
        gap = index_gap(day)
        if gap is not None:
            ctx.notes.append(f"{day}: no {self.what}, a gap in EDGAR's daily index ({gap})")
            return True
        self.unexplained.append(day)
        if stretch(self.unexplained, self.limit):
            run = sorted(self.unexplained[-self.limit:])
            seen = self.passed + self.limit
            if self.probe is not None and seen <= UNEXPLAINED_MAX and self.probe():
                self.passed = seen
                ctx.notes.append(f"{', '.join(str(d) for d in run)}: no {self.what} for {self.limit} weekdays EDGAR "
                                 "was open, but the path handed over one it holds, so the walk goes on")
                self.unexplained.append(None)
                return False
            why = (f"{seen} such weekdays in this run, more than the {UNEXPLAINED_MAX} a closure the calendar "
                   "does not know can explain" if self.probe is not None and seen > UNEXPLAINED_MAX
                   else "the bucket may be refusing every request")
            raise SourceError(f"sec: no {self.what} for {self.limit} weekdays EDGAR was open "
                              f"({', '.join(str(d) for d in run)}); {why}, so none of them is written off and the "
                              "next run asks them again")
        ctx.notes.append(f"{day}: no {self.what} although EDGAR was open")
        return False


class MissingObjects:
    """The objects in a row a host answered ``AccessDenied`` for, where no calendar can explain one.

    ``MissingStreak``'s sibling for a thing with no closed days: a filing's document either is in the archive or
    is not, and the ambiguous 403 (see ``is_missing``) is also what a path refusing everything answers. So the
    marker that writes one off is **held**, not written: an answer that carries an object (``answered``) clears
    the run and writes the held markers, while ``limit`` misses in a row stop the run with none of them written,
    so the next run asks them again. ``release`` ends a walk: it writes what is held only if the host's last word
    was an object, and otherwise leaves the trailing few to the next run.
    """

    def __init__(self, what: str, limit: int = MISSING_RUN) -> None:
        self.what, self.limit = what, limit
        self.run: list[str] = []
        self.held: list[tuple[str, Callable[[], Any]]] = []

    def answered(self) -> None:
        """The host handed over an object, so it is not refusing everything: what is held can be written."""
        self.run = []
        held, self.held = self.held, []
        for _, write in held:
            write()

    def gone(self, name: str, write: Callable[[], Any]) -> None:
        """One the host says it holds no object for; ``write`` marks it, once the host has answered. Raises with
        nothing written once ``limit`` of them came in a row."""
        self.run.append(name)
        self.held.append((name, write))
        if len(self.run) >= self.limit:
            raise SourceError(f"sec: the archive holds no {self.what} for {self.limit} in a row "
                              f"({', '.join(self.run)}); it may be refusing every request, so none of them is "
                              "written off and the next run asks them again")

    def release(self, ctx: "RunContext") -> None:
        """End of the walk: write what is held if the host's last answer carried an object, else leave it."""
        if not self.run:
            self.answered()
        elif self.held:
            ctx.notes.append(f"{len(self.held)} {self.what} markers held back: the archive's last answers carried "
                             "no object, so the next run asks for them again")
            self.held = []


def refusal_note(keyed: bool = False) -> str:
    """What a 403 points at: the provider's key when the request carries one, else ``RC_HTTP_CONTACT``. A source
    that accepts a 403 itself (``accept``) and then decides it is a real refusal raises with the same words."""
    if keyed:
        return " (refused: check the key and what it is allowed to read)"
    return (f" (refused: set {CONTACT_ENV} to a contact for the User-Agent)"
            if not (os.getenv(CONTACT_ENV) or "").strip() else " (refused although a contact is set)")


def http_get(ctx: RunContext, provider: str, url: str, params: dict | None = None,
             headers: dict | None = None, keyed: bool = False, accept: tuple[int, ...] = ()) -> httpx.Response:
    """GET with the keyless retry ladder; every attempt is counted for ``provider``. Raises ``SourceError``.

    ``params`` may hold a key (FRED): neither the query nor the headers are ever logged. ``keyed``: the request
    carries the provider's key (Alpaca's headers), so a 403 points at the key, not at ``RC_HTTP_CONTACT``. A status in
    ``accept`` is answered as it is, neither retried nor raised: a source whose 404 means "not published yet" reads it
    itself.
    """
    last = ""
    for attempt in range(1, HTTP_ATTEMPTS + 1):
        ctx.check_cap(provider)
        ctx.count(provider, calls=1)        # counted when sent: a request cut off by a kill was still made
        try:
            r = ctx.http.get(url, params=params, headers=headers)
        except httpx.HTTPError as e:
            last = type(e).__name__
        else:
            if r.status_code == 429:
                ctx.count(provider, http_429=1)
            if r.status_code < 400 or r.status_code in accept:
                return r
            last = f"HTTP {r.status_code}"
            if r.status_code == 403:
                last += refusal_note(keyed)
            if not (r.status_code == 429 or r.status_code >= 500):
                break
        if attempt < HTTP_ATTEMPTS:
            ctx.sleep(HTTP_BACKOFF_S * 2 ** (attempt - 1))
    ctx.count(provider, failures=1)
    raise SourceError(f"{provider} {url}: {last}")


def http_post(ctx: RunContext, provider: str, url: str, json: Any = None, headers: dict | None = None,
              pacer: "Pacer | None" = None) -> httpx.Response:
    """POST ``json`` with the same retry ladder and counting as ``http_get``; ``pacer`` waits before every attempt.

    A 429 is retried after OpenFIGI's ``ratelimit-reset`` seconds when the answer names them, else the ladder's
    backoff. ``headers`` may hold a key: neither they nor the body are ever logged. Raises ``SourceError``.
    """
    last = ""
    for attempt in range(1, HTTP_ATTEMPTS + 1):
        ctx.check_cap(provider)
        if pacer is not None:
            pacer.wait()
        ctx.count(provider, calls=1)
        wait = HTTP_BACKOFF_S * 2 ** (attempt - 1)
        try:
            r = ctx.http.post(url, json=json, headers=headers)
        except httpx.HTTPError as e:
            last = type(e).__name__
        else:
            if r.status_code < 400:
                return r
            last = f"HTTP {r.status_code}"
            if r.status_code == 429:
                ctx.count(provider, http_429=1)
                reset = str(r.headers.get("ratelimit-reset") or "").strip()
                wait = float(reset) + 1 if reset.replace(".", "", 1).isdigit() else wait
            elif r.status_code < 500:
                break
        if attempt < HTTP_ATTEMPTS:
            ctx.sleep(wait)
    ctx.count(provider, failures=1)
    raise SourceError(f"{provider} {url}: {last}")


# ── Raw files ──────────────────────────────────────────────────────────────────────────────────────────────────

def _publish(tmp: Path, final: Path) -> None:
    os.replace(tmp, final)


def write_raw(path: Path, data: bytes) -> Path:
    """Write a raw file whole or not at all (``.tmp`` then rename); gzip when the name ends in ``.gz``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(gzip.compress(data) if path.suffix == ".gz" else data)
    _publish(tmp, path)
    return path


def read_raw(path: Path) -> bytes:
    data = path.read_bytes()
    return gzip.decompress(data) if path.suffix == ".gz" else data


def write_raw_json(path: Path, obj: Any) -> Path:
    return write_raw(path, json.dumps(obj, sort_keys=True).encode("utf-8"))


def read_raw_json(path: Path) -> Any:
    return json.loads(read_raw(path))


# ── Parquet ────────────────────────────────────────────────────────────────────────────────────────────────────

def _copy(con: duckdb.DuckDBPyConnection, select: str, path: Path) -> None:
    """``COPY (select) TO path`` as zstd Parquet. The one place a Parquet file is written."""
    con.execute(f"COPY ({select}) TO '{rawstore._sql_text(path)}' "
                f"(FORMAT parquet, COMPRESSION {PARQUET_COMPRESSION}, ROW_GROUP_SIZE {PARQUET_ROW_GROUP})")


#: True while a raw-only run (``--no-parquet``, #498) is fetching: ``write_parquet`` writes nothing, so a dataset
#: whose ``backfill`` converts inside itself (``market_series``, the fundamentals datasets) stages raw files only.
#: A ContextVar and not a ``RunContext`` field, because those conversions reach ``write_parquet`` without the
#: context in hand; one run per process sets it, and ``raw_only_run`` resets it even when the run raises.
_RAW_ONLY: contextvars.ContextVar[bool] = contextvars.ContextVar("rcdata_raw_only", default=False)


def raw_only() -> bool:
    """Is this run raw-only (``--no-parquet``)? Read by ``write_parquet``."""
    return _RAW_ONLY.get()


@contextlib.contextmanager
def raw_only_run(on: bool = True):
    """Hold the raw-only switch for the fetch of one run (``on=False``: an ordinary run, nothing changes)."""
    token = _RAW_ONLY.set(bool(on))
    try:
        yield
    finally:
        _RAW_ONLY.reset(token)


def write_parquet(con: duckdb.DuckDBPyConnection, select: str, final: Path) -> Path:
    """Write ``select`` to ``final`` through ``<final>.tmp`` and a rename, so a partition that exists is whole.

    In a raw-only run (``--no-parquet``) nothing is written and ``final`` is returned as it is: the callers collect
    the paths into the list their ``to_parquet_all`` returns, and in that mode the files are simply not there.
    """
    if raw_only():
        return final
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = final.with_name(final.name + ".tmp")
    _copy(con, select, tmp)
    _publish(tmp, final)
    return final


def sql_path(path: Path) -> str:
    """A path as a quoted SQL string literal (through rawstore's one escaper)."""
    return f"'{rawstore._sql_text(path)}'"


def rows_table(con: duckdb.DuckDBPyConnection, name: str, schema: list[tuple[str, str]], rows: list[dict]) -> None:
    """Create table ``name`` with ``schema`` in ``con`` (an in-memory connection) and load ``rows`` into it typed."""
    cols = ", ".join(f'"{c}" {t}' for c, t in schema)
    con.execute(f"CREATE OR REPLACE TABLE {name} ({cols})")
    if not rows:
        return
    import pandas as pd
    frame = pd.DataFrame([{c: r.get(c) for c, _ in schema} for r in rows], columns=[c for c, _ in schema])
    con.register("_rows_frame", frame)
    try:
        casts = ", ".join(f'CAST("{c}" AS {t})' for c, t in schema)
        con.execute(f"INSERT INTO {name} SELECT {casts} FROM _rows_frame")
    finally:
        con.unregister("_rows_frame")


def with_all(con: duckdb.DuckDBPyConnection, schema: list[tuple[str, str]], src: str) -> str:
    """``src`` with every ``schema`` column it lacks added as a typed NULL (Parquet written before it was added)."""
    have = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()}
    missing = [f'CAST(NULL AS {t}) AS "{c}"' for c, t in schema if c not in have]
    return f"(SELECT *, {', '.join(missing)} FROM {src})" if missing else src


def upsert_partition(rows: list[dict], schema: list[tuple[str, str]], key: list[str], final: Path) -> Path:
    """Merge ``rows`` into the Parquet partition ``final`` by ``key``; the newest ``fetched_at`` wins a key."""
    con = duckdb.connect()
    try:
        rows_table(con, "new_rows", schema, rows)
        cols = ", ".join(f'"{c}"' for c, _ in schema)
        source = "SELECT * FROM new_rows"
        if final.exists():
            old = with_all(con, schema, f"read_parquet({sql_path(final)})")
            source = f"SELECT {cols} FROM {old} UNION ALL BY NAME " + source
        keys = ", ".join(f'"{k}"' for k in key)
        select = (f"SELECT {cols} FROM ({source}) QUALIFY row_number() OVER "
                  f"(PARTITION BY {keys} ORDER BY fetched_at DESC) = 1 ORDER BY {keys}")
        return write_parquet(con, select, final)
    finally:
        con.close()


# ── Pacing ─────────────────────────────────────────────────────────────────────────────────────────────────────

class Pacer:
    """At most ``limit`` calls in any ``window_s`` window (60 s by default; ``caps.WINDOWS`` has the providers' own):
    ``wait()`` before each call sleeps when the window is full."""

    def __init__(self, limit: int, window_s: float = 60.0, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Any] = time.sleep):
        self.limit, self.window_s, self._clock, self._sleep = limit, window_s, clock, sleep
        self._sent: deque[float] = deque()

    def wait(self) -> None:
        while True:
            now = self._clock()
            while self._sent and now - self._sent[0] >= self.window_s:
                self._sent.popleft()
            if len(self._sent) < self.limit:
                break
            # At least a millisecond: a sleep that lands a hair short of the window edge must not spin.
            self._sleep(max(self.window_s - (now - self._sent[0]), 0.001))
        self._sent.append(now)


def run_pacer(ctx: RunContext, provider: str) -> Pacer:
    """The run's one pacer of ``provider`` at its published window (``caps.WINDOWS``), shared by every dataset of the
    run that asks that provider (``ctx.pending``)."""
    key = f"_pacer_{provider}"
    if key not in ctx.pending:
        ctx.pending[key] = Pacer(*caps.WINDOWS[provider], clock=ctx.clock, sleep=ctx.sleep)
    return ctx.pending[key]


# ── Sessions ───────────────────────────────────────────────────────────────────────────────────────────────────

def session_date_of(obs: date) -> date | None:
    """The New York session on or before ``obs`` (``core/calendar.py``); None outside its 1993-2028 table.

    Rule C of the calendar: no second holiday table, so an observation before 1993 has no session date here.
    """
    try:
        return calendar.to_session_date(obs)
    except ValueError:
        return None


def next_due(as_of: date) -> str | None:
    try:
        return calendar.next_trading_day(as_of).isoformat()
    except ValueError:
        return None


# ── The dataset protocol ───────────────────────────────────────────────────────────────────────────────────────

class Dataset(Protocol):
    name: str                     # "uw/spot_expiry_strike", "market_series"
    kind: str                     # the serving kind its tables live in
    providers: tuple[str, ...]    # what the day cap is checked against
    tables: tuple[str, ...]       # the serving tables it builds

    def default_as_of(self) -> date: ...
    def fetch(self, ctx: RunContext, as_of: date) -> list[Path]: ...
    def to_parquet(self, ctx: RunContext, raw: Path) -> list[Path]: ...
    def to_parquet_all(self, ctx: RunContext, raws: list[Path]) -> list[Path]: ...
    def backfill(self, ctx: RunContext, start: date | None, end: date) -> None: ...
    def build(self, ctx: RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]: ...
    def check(self, ctx: RunContext, con: duckdb.DuckDBPyConnection) -> dict: ...

    # Optional (PR 5), read with getattr so the market datasets need none of them:
    #   attended: bool              a person must be there (WRDS login and Duo): never due, and a request runs
    #                               only in a runner that has one — the CLI's own request (``requested_by``
    #                               "cli") or ``build attended`` (``attended_ok``). Anyone else's request waits
    #                               in the queue for that runner (``attended_names``).
    #   backfill_needs_from: bool   default True; False lets ``backfill`` start at the source's beginning (None)
    #   after_swap(ctx)             runs once the kind's new file is live (the spine's build-issue sync)
    #   honours_scope: bool         the fetch asks the names ``ctx.scope`` names (#265); without it a request that
    #                               carried a scope gets a note saying this dataset ignored it


ATTENDED_BY = "cli"


def attended(ds) -> bool:
    return bool(getattr(ds, "attended", False))


WRDS_UNATTENDED_ENV = "RC_WRDS_UNATTENDED"


def wrds_attended() -> bool:
    """Whether the WRDS datasets wait for a person at a terminal. ``RC_WRDS_UNATTENDED=1`` lets the hourly builder
    run them on their cadence: homesrv's ``life`` connects with no Duo push (checked 1 Nov 2026), and a login that
    asks for one fails within ``PGCONNECT_TIMEOUT`` (``spine/wrds.py``'s worker environment) instead of hanging."""
    return (os.getenv(WRDS_UNATTENDED_ENV) or "").strip() != "1"


def attended_names() -> frozenset[str]:
    """The datasets a person must be there for (the WRDS login and Duo).

    The hourly builder leaves their requests in the queue (``ops.next_request(skip=...)``) and the attended runner
    (``python -m RevenueChaser.rcdata.build attended``) claims only them, so a Fetch press on one of them waits for
    the terminal instead of being refused.
    """
    from . import DATASETS
    return frozenset(name for name, ds in DATASETS.items() if attended(ds))


def live_columns(con: duckdb.DuckDBPyConnection, table: str, columns: list[str]) -> list[str]:
    """``columns`` that ``table`` of the attached live file holds: a live file built before a column was added is
    compared on the columns both files have."""
    have = {r[0] for r in con.execute("SELECT column_name FROM duckdb_columns() WHERE database_name = 'live_cmp' "
                                      "AND table_name = ?", (table,)).fetchall()}
    return [c for c in columns if c in have]


def compare_with_live(con: duckdb.DuckDBPyConnection, kind: str, table: str, columns: list[str]) -> tuple[int, int]:
    """``(added, removed)`` rows of ``table`` in the build file against the live file, on ``columns``.

    The live file is attached read-only for the comparison and detached before returning.
    """
    live = serving.live_path(kind)
    new_rows = con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
    if not live.exists():
        return int(new_rows), 0
    con.execute(f"ATTACH {sql_path(live)} AS live_cmp (READ_ONLY)")
    try:
        known = {r[0] for r in con.execute(
            "SELECT table_name FROM duckdb_tables() WHERE database_name = 'live_cmp'").fetchall()}
        if table not in known:
            return int(new_rows), 0
        cols = ", ".join(f'"{c}"' for c in live_columns(con, table, columns))
        added = con.execute(f'SELECT count(*) FROM (SELECT {cols} FROM "{table}" '
                            f'EXCEPT ALL SELECT {cols} FROM live_cmp."{table}")').fetchone()[0]
        removed = con.execute(f'SELECT count(*) FROM (SELECT {cols} FROM live_cmp."{table}" '
                              f'EXCEPT ALL SELECT {cols} FROM "{table}")').fetchone()[0]
        return int(added), int(removed)
    finally:
        con.execute("DETACH live_cmp")


# ── Gates ──────────────────────────────────────────────────────────────────────────────────────────────────────

def idle_problem(lock: str = BUILDER_LOCK) -> str | None:
    """Why the writer of ``lock`` must not start now, or None: a lock on a file it writes, or another live builder.

    Checked once, at start (S5): a job that takes a lock later runs beside the builder, which writes only its own
    files.

    Only the locks that can meet ``lock`` on a file count (``job_lock.blocking_holders``, which checks and never
    takes one). The builder writes the market store, so an old-store job — the Relatedness pilot, an EDGAR sync, a
    nightly step — no longer stops it: that refusal is what stopped the WRDS price backfill while the pilot ran
    (30 Oct 2026). A second builder still does, both as its own ``rcdata_build`` lock and as a live
    ``.rc-proc-<pid>`` marker in the market dir, and so does any lock whose family has not declared what it writes.
    """
    held = job_lock.blocking_holders(lock)
    if held:
        return "job lock held: " + ", ".join(f"{name} (pid {pid})" for name, pid in sorted(held))
    peers = rawstore._peer_pids(Path(config.MARKET_DIR) / "ops.sqlite")
    if peers:
        return "another builder runs: pid " + ", ".join(str(p) for p in peers)
    return None


def cap_problem(ds: Dataset) -> str | None:
    """Why the whole dataset is skipped: every one of its providers is at its day cap. One capped provider of
    several (FRED among the market series) is refused call by call instead (``RunContext.check_cap``)."""
    refused = []
    for provider in ds.providers:
        at, used, cap = caps.at_cap(provider)
        if not at:
            return None
        refused.append(f"{provider} {used}/{cap}")
    return "day cap reached: " + ", ".join(refused) if refused else None


def paused_problem(ds: Dataset) -> str | None:
    """``skipped: paused ...`` while ``ops.dataset_schedule`` holds a pause for the dataset (Settings' Pause), else
    None. Checked before anything is fetched, so a paused dataset sends no call. The board's fetch switch (#527)
    holds every dataset at once and is read first."""
    off = ops.switch_off("fetch")
    if off is not None:
        return f"skipped: {ops.switch_words('fetch', off)}"
    row = ops.schedule_get(ds.name)
    if not row or row.get("paused_at") is None:
        return None
    return f"skipped: {_paused_words(row)}"


def _paused_words(row: dict) -> str:
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(row["paused_at"]))
    return f"paused by {row.get('paused_by') or '?'} at {when}"


# ── One request ────────────────────────────────────────────────────────────────────────────────────────────────

#: The check note of a ``--no-duckdb`` run: the serving file was not touched, so the result is a skip, not an "ok".
#: It says what the fetch really staged, because the first live run read as a success while its fetch had returned
#: nothing (``uw/option_list`` outside its provider's window, 5 Nov 2026).
NO_DUCKDB_NOTE = "skipped: build skipped (--no-duckdb)"
#: The same for a raw-only run (``--no-parquet``, #498), which skips the Parquet step as well.
NO_PARQUET_NOTE = "skipped: build and Parquet skipped (--no-parquet)"


#: The params of each run stage (``ops.KIND_MODES`` but ``off``): a Fetch press's ▾ (``app/dashboard.py``) and the
#: hourly pass's kind automation (``due.py``) queue the same request.
STAGE_PARAMS = {"raw": {"no_parquet": True}, "parquet": {"no_duckdb": True}, "full": {}}


def skip_note(staged: int | None, *, raw_only: bool = False) -> str:
    """The ``--no-duckdb`` / ``--no-parquet`` check note; ``staged`` is how many raw files the fetch returned, None
    when it cannot be counted (a backfill stages inside the dataset and returns nothing)."""
    head = NO_PARQUET_NOTE if raw_only else NO_DUCKDB_NOTE
    if staged is None:
        return f"{head}; the backfill staged what it fetched"
    if staged == 0:
        return f"{head}; nothing staged (the fetch returned no file)"
    return f"{head}; {staged} raw file{'' if staged == 1 else 's'} staged"


def new_run_id() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8]


def kind_datasets(kind: str) -> list[Dataset]:
    from . import DATASETS
    return [d for d in DATASETS.values() if d.kind == kind]


def shrink_scope(ds: Dataset) -> tuple[str, ...]:
    """The tables a request's ``allow_shrink`` may let shrink (review S-1 of #220): the dataset's own tables and the
    tables the kind derives from its sources (a dataset with no provider: ``ownership/serve``, ``prices/serving``,
    ``spine/security``, ``fundamentals/tables``), never another source dataset's own table. A feeder that serves no
    table itself (a spine or fundamentals source) relaxes only the derived tables.
    shortcut: every derived table of the kind is relaxed, not only those the asking source's rows reach; upgrade when
    the datasets declare which tables each source feeds."""
    derived = [t for d in kind_datasets(ds.kind) if not tuple(d.providers) for t in d.tables]
    return tuple(dict.fromkeys([*ds.tables, *derived]))


def build_kind(ctx: RunContext, ds: Dataset) -> dict:
    """Build ``ds``'s serving kind from Parquet (every dataset of the kind), check it, swap it in; the check row."""
    return build_kind_many(ds.kind, [(ds, ctx)], lead=ctx)[ds.name]


def _dir_bytes(folder: Path) -> int:
    total = 0
    for p in folder.rglob("*"):
        try:
            total += p.stat().st_size if p.is_file() else 0
        except FileNotFoundError:                     # DuckDB freed it while listed
            pass
    return total


@contextlib.contextmanager
def spill_peak(folder: Path, every: float = 2.0):
    """The most bytes ``folder`` held while the block ran, sampled every ``every`` seconds and once at its end; yields
    a one-item list the peak is written into. shortcut: a spike shorter than ``every`` can be missed, upgrade only if
    the logged peaks look too low against the disk."""
    peak = [0]
    stop = threading.Event()

    def sample() -> None:
        while True:
            peak[0] = max(peak[0], _dir_bytes(folder))
            if stop.wait(every):
                return

    thread = threading.Thread(target=sample, name="spill-peak", daemon=True)
    thread.start()
    try:
        yield peak
    finally:
        stop.set()
        thread.join()
        peak[0] = max(peak[0], _dir_bytes(folder))


def build_kind_many(kind: str, members: list[tuple[Dataset, RunContext]], *, lead: RunContext) -> dict[str, dict]:
    """Build ``kind`` once for every request a pass holds of it (PR A); ``{dataset: check row}`` per member, a member
    being a request's ``(dataset, context)``.

    Each dataset of the kind builds with its own request's context when it has one (its ``as_of``, its notes), and
    with ``lead``'s otherwise, as a single request's build always did. Every check rides the one swap, so a failed
    data check of any of them refuses the file; ``allow_shrink`` relaxes the tables of the requests that asked."""
    if not parquet_root().is_dir():
        raise SourceError(f"the Parquet root {parquet_root()} is missing (RC_MARKET_RAW_DIR unset or not mounted?)")
    ctxs = {ds.name: c for ds, c in members}
    shrink = tuple(dict.fromkeys(t for ds, c in members if c.allow_shrink for t in shrink_scope(ds)))
    from . import dirty
    plan = dirty.plan(kind, lead)
    every = [lead, *ctxs.values()]
    for c in every:
        c.changed = lead.changed
    with serving.build(kind, base="live" if plan is not None and plan.reason is None else "fresh") as b:
        on_live = b.mode == "live"
        for c in every:
            c.incremental = plan if on_live else None
        if on_live:
            # A table the code no longer builds would survive in the copy and go on being served (#451).
            for table in serving.RETIRED_TABLES.get(kind, ()):
                b.con.execute(f'DROP TABLE IF EXISTS "{table}"')
        counts: dict[str, int] = {}
        for d in kind_datasets(kind):
            started = time.monotonic()
            with spill_peak(serving.spill_dir(b.path)) as peak:
                counts.update(d.build(ctxs.get(d.name, lead), b.con))
            # What a dataset costs, so a chunked or SSD-spilling build is decided from a measure (#654 review).
            logger.info("build %s %s (%s): %.1f s, spill peak %.2f GB", kind, d.name, b.mode,
                        time.monotonic() - started, peak[0] / 1e9)
        if plan is not None:
            dirty.write_manifest(b.con, plan)
            if not on_live and plan.reason is None:
                plan.reason = "no live file to build on"
            lead.notes.append(plan.note())
        checks = {ds.name: ds.check(c, b.con) for ds, c in members}
        b.swap(counts, checks=list(checks.values()), allow_shrink=shrink)
    for d in kind_datasets(kind):
        step = getattr(d, "after_swap", None)
        if step is not None:
            step(ctxs.get(d.name, lead))
    return checks


def _parse_day(value) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def run_request(req: dict, *, trigger: str = "queue", attended_ok: bool = False,
                sleep: Callable[[float], Any] = time.sleep,
                clock: Callable[[], float] = time.monotonic) -> dict:
    """Run one queued request to its end and return the ``dataset_check`` row written for it.

    ``req["params"]``: ``{"mode": "daily", "as_of": "YYYY-MM-DD"}`` (both optional) or
    ``{"mode": "backfill", "from": "YYYY-MM-DD", "to": "YYYY-MM-DD"}``, either with the scope the request asks for
    (``{"tiers": ["A", "B"], "names": ["KO"]}``, both optional; ``parse_scope``).

    ``attended_ok`` says this runner is a person at a terminal (``build attended``, or a CLI command that named the
    dataset): only then does an attended dataset run whoever queued it. Without it, an attended request still runs
    when the CLI itself queued it (``requested_by`` ``ATTENDED_BY``), and is skipped otherwise.
    """
    from . import DATASETS
    ds = DATASETS.get(req["dataset"])
    params = req.get("params") or {}
    run_id = new_run_id()
    ctx = RunContext(run_id=run_id, dataset=req["dataset"], trigger=trigger, sleep=sleep, clock=clock,
                     allow_shrink=params.get("allow_shrink") is True)
    ops.start_run(run_id, req["dataset"], trigger, params)
    try:
        return _run(ctx, ds, req, params, attended_ok)
    except BaseException as e:
        _abort(run_id, req, e)
        raise
    finally:
        ctx.close()
        ctx.flush_counts()


def _abort(run_id: str | None, req: dict, e: BaseException) -> None:
    """End a run (when it started) and its request after ``e`` got past ``_run``.

    A kill (SIGTERM, which the CLI turns into SystemExit, or Ctrl-C) is "killed"; anything else that got past _run
    (a locked ops.sqlite while writing the check row) is "failed". No check row either way, but the calls already
    sent are counted by the caller's ``flush_counts``. A killed run ends its request too (review B7, 8 Nov): left
    "running", the next builder start would fail it as "lost: pid unknown (builder died)", which is not what
    happened. A request has no "killed" status, so it is "failed" with a note that says so."""
    status = "killed" if isinstance(e, (KeyboardInterrupt, SystemExit)) else "failed"
    why = f"{type(e).__name__}: {e}"[:500]
    if run_id is not None:
        try:
            ops.finish_run(run_id, status, why)
        except ops.STORE_ERRORS:
            logger.warning("could not mark run %s %s", run_id, status, exc_info=True)
    if status == "killed" and req.get("id") is not None:
        try:
            ops.finish_request(req["id"], "failed", note=f"killed: {why}"[:300])
        except ops.STORE_ERRORS:
            logger.warning("could not end request %s", req["id"], exc_info=True)


def _run(ctx: RunContext, ds, req: dict, params: dict, attended_ok: bool = False) -> dict:
    done, stage_note = _stage(ctx, ds, req, params, attended_ok)
    if done is not None:
        return done
    if stage_note is not None:
        # "Stage only" (--no-duckdb): the data is in raw and Parquet; raw-only (--no-parquet): in raw alone.
        # Either way the serving file is left as it is. The check row a build would have written cannot be
        # made without one, and inventing a result value would reach due.py and ops; a skip is the truth (the
        # serving file did not change) and leaves the dataset due, so the next ordinary pass builds it at no
        # provider call (the raw file is the resume marker).
        return _finish(ctx, req, {"result": "skipped", "note": stage_note}, "skipped", None)
    try:
        return _finish(ctx, req, build_kind(ctx, ds), "ok", None)
    except (Paused, NothingInScope) as e:
        return _finish(ctx, req, {"result": "skipped", "note": f"skipped: {e}"}, "skipped", None)
    except Exception as e:  # noqa: BLE001 - every failure becomes a failed check row, never a half-swapped file
        return _failed(ctx, req, e)


def _failed(ctx: RunContext, req: dict, e: Exception) -> dict:
    logger.exception("%s run %s failed", req["dataset"], ctx.run_id)
    error = f"{type(e).__name__}: {e}"[:500]
    return _finish(ctx, req, {"result": "failed", "note": error}, "failed", error)


def _stage(ctx: RunContext, ds, req: dict, params: dict, attended_ok: bool = False) -> tuple[dict | None, str | None]:
    """Everything a run does before its build: ``(finished row, None)`` when the request already ended (skipped,
    paused, failed), ``(None, note)`` for a stage-only request whose fetch is done, ``(None, None)`` when the
    request is ready for its kind's build."""
    try:
        if ds is None:
            raise ValueError(f"unknown dataset {req['dataset']!r}")
        paused = paused_problem(ds)
        if paused:
            return _finish(ctx, req, {"result": "skipped", "note": paused}, "skipped", None), None
        if attended(ds) and not attended_ok and req.get("requested_by") != ATTENDED_BY:
            note = (f"skipped: attended dataset: it waits for the runner on the server (a person logs in): "
                    f"python -m RevenueChaser.rcdata.build attended; requested by "
                    f"{req.get('requested_by') or '?'}")
            return _finish(ctx, req, {"result": "skipped", "note": note}, "skipped", None), None
        refused = cap_problem(ds)
        if refused:
            return _finish(ctx, req, {"result": "skipped", "note": refused}, "skipped", None), None
        ctx.scope = parse_scope(params)             # a bad scope is this run's failed check row, like a bad date
        if ctx.scope and not honours_scope(ds):
            ctx.notes.append(f"scope ignored: {ds.name} does not ask by name or tier ({ctx.scope.text()} "
                             "not applied)")
        mode = params.get("mode", "daily")
        staged: int | None = None
        # The two "do not build" switches (#498). ``no_parquet`` is raw-only and implies ``no_duckdb``, because
        # Parquet is the only input a build reads: a build with no new partition would be a no-op that still took
        # the builder lock. ``no_build`` is the old name of ``no_duckdb``, still read so that a request queued
        # before the rename keeps working.
        raw_only_mode = params.get("no_parquet") is True
        skip_build = raw_only_mode or params.get("no_duckdb") is True or params.get("no_build") is True
        with raw_only_run(raw_only_mode):
            if mode == "backfill":
                if params.get("from"):
                    start = _parse_day(params["from"])
                elif getattr(ds, "backfill_needs_from", True):
                    raise ValueError(f"a backfill of {ds.name} needs a first day (--from)")
                else:
                    start = None
                end = _parse_day(params["to"]) if params.get("to") else ds.default_as_of()
                ctx.as_of = end
                # A dataset's own backfill converts inside itself, so the switch reaches it through the
                # ``raw_only`` guard in ``write_parquet`` rather than by skipping a call here.
                ds.backfill(ctx, start, end)
            elif mode == "daily":
                ctx.as_of = _parse_day(params["as_of"]) if params.get("as_of") else ds.default_as_of()
                raws = ds.fetch(ctx, ctx.as_of)
                staged = len(raws)
                if not raw_only_mode:
                    ds.to_parquet_all(ctx, raws)
            else:
                raise ValueError(f"unknown mode {mode!r}")
        ctx.check_pause(force=True)                 # the last stop: a build that starts runs to its end
        if skip_build:
            return None, skip_note(staged, raw_only=raw_only_mode)
        return None, None
    except Paused as e:
        # the files fetched so far stay; the serving file was not touched
        return _finish(ctx, req, {"result": "skipped", "note": f"skipped: {e}"}, "skipped", None), None
    except NothingInScope as e:
        return _finish(ctx, req, {"result": "skipped", "note": f"skipped: {e}"}, "skipped", None), None
    except Exception as e:  # noqa: BLE001 - a failed fetch is its own failed check row, never a half-swapped file
        return _failed(ctx, req, e), None


def run_batch(reqs: list[dict], *, trigger: str = "queue", attended_ok: bool = False) -> list[dict]:
    """Run the claimed requests of one kind with one build of its serving file (PR A); one check row each.

    Each request is fetched in turn (registry order, the caller's), exactly as ``run_request`` would. Then:

    - a fetch that failed, was paused or skipped has ended already with its own row and takes no part in the build;
    - when no request that wants a build is left, nothing is built (owner's D-4, 8 Nov): a stage-only request keeps
      its ``skipped`` note and the file is not rewritten from the same Parquet;
    - otherwise the kind is built once, and every fetched request, stage-only ones included (owner's D-8), gets the
      check row of that build. A build that fails fails the requests that wanted it; a stage-only one keeps its skip.

    A single request is ``run_request`` itself, so that path is unchanged.
    """
    if len(reqs) <= 1:
        return [run_request(r, trigger=trigger, attended_ok=attended_ok) for r in reqs]
    from . import DATASETS
    rows: list[dict] = []
    ended: set[str] = set()                           # run ids whose run and request have ended

    ended_requests: set = set()                       # their request ids, for the caller's busy-store path

    def end(ctx: RunContext, row: dict) -> None:
        ended.add(ctx.run_id)
        ended_requests.add(next(r["id"] for r, c in started if c is ctx))
        rows.append(row)
    started: list[tuple[dict, RunContext]] = []       # (request, context) of every run started, ended or not
    ready: list[tuple[dict, Any, RunContext, str | None]] = []
    pending = list(reqs)
    try:
        while pending:
            req = pending.pop(0)
            ds = DATASETS.get(req["dataset"])
            params = req.get("params") or {}
            ctx = RunContext(run_id=new_run_id(), dataset=req["dataset"], trigger=trigger,
                             allow_shrink=params.get("allow_shrink") is True)
            ops.start_run(ctx.run_id, req["dataset"], trigger, params)
            started.append((req, ctx))
            done, note = _stage(ctx, ds, req, params, attended_ok)
            ctx.flush_counts()                      # the next member's day-cap read sees these calls
            if done is not None:
                end(ctx, done)
            else:
                ready.append((req, ds, ctx, note))
        wanted = [r for r in ready if r[3] is None]
        if not wanted:
            for req, ds, ctx, note in ready:
                end(ctx, _finish(ctx, req, {"result": "skipped", "note": note}, "skipped", None))
            return rows
        kind = wanted[0][1].kind
        try:
            checks = build_kind_many(kind, [(ds, ctx) for req, ds, ctx, note in ready], lead=wanted[0][2])
        except Exception as e:  # noqa: BLE001 - every failure becomes a failed check row, never a half-swapped file
            for req, ds, ctx, note in ready:
                end(ctx, _failed(ctx, req, e) if note is None else
                    _finish(ctx, req, {"result": "skipped", "note": note}, "skipped", None))
            return rows
        for req, ds, ctx, note in ready:
            end(ctx, _finish(ctx, req, checks[ds.name], "ok", None))
        return rows
    except BaseException as e:
        for req, ctx in started:
            if ctx.run_id not in ended:
                _abort(ctx.run_id, req, e)
        for req in pending:                         # claimed but not reached: no run row, the request still ends
            _abort(None, req, e)
        try:
            e.rc_ended = frozenset(ended_requests)
        except AttributeError:
            pass
        raise
    finally:
        for req, ctx in started:
            try:
                ctx.close()
                ctx.flush_counts()
            except ops.STORE_ERRORS:
                logger.warning("could not write the calls of run %s", ctx.run_id, exc_info=True)


def _finish(ctx: RunContext, req: dict, check: dict, status: str, error: str | None) -> dict:
    ctx.close()
    ctx.flush_counts()
    notes = "; ".join([n for n in [check.get("note"), *ctx.notes] if n]) or None
    row = {k: check.get(k) for k in ops.CHECK_FIELDS if k not in ("dataset", "checked_on", "run_id", "note")}
    row["calls"] = ctx.total_calls()
    if row.get("source_as_of") is None and ctx.as_of is not None:
        row["source_as_of"] = ctx.as_of.isoformat()
    if row.get("next_check_due") is None and ctx.as_of is not None:
        row["next_check_due"] = next_due(ctx.as_of)
    result = row.pop("result")
    ops.record_check(ctx.dataset, result, run_id=ctx.run_id, note=notes, **row)
    ops.finish_run(ctx.run_id, status, error)
    if req.get("id") is not None:
        ops.finish_request(req["id"], {"ok": "done", "skipped": "skipped"}.get(status, "failed"), note=notes)
    out = ops.latest_check(ctx.dataset)
    logger.info(format_check(out))
    return out


def format_check(row: dict) -> str:
    """One CLI/log line per dataset run: the ``dataset_check`` fields in order, under 80 columns where it can."""
    parts = [f"{row['dataset']} {row['result']}", f"as_of={row['source_as_of']}", f"rows={row['rows']}",
             f"+{row['added']}/-{row['removed']}", f"calls={row['calls']}"]
    if row.get("note"):
        parts.append(f"note={row['note'][:60]}")
    return " ".join(str(p) for p in parts)


def iter_sessions(start: date, end: date) -> Iterable[date]:
    """Every NYSE session from ``start`` to ``end`` inclusive (``core/calendar.py``)."""
    d = start if calendar.is_trading_day(start) else calendar.next_trading_day(start)
    while d <= end:
        yield d
        d = calendar.next_trading_day(d)
