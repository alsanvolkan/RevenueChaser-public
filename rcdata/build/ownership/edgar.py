"""The EDGAR walk the SEC filing datasets share (``sec/13f``, ``sec/form4``, ``sec/13dg``): the daily index, one
request per filing, and a time budget per run.

**The daily index.** ``GET <RC_SEC_WWW_BASE>/Archives/edgar/daily-index/<YYYY>/QTR<q>/master.<YYYYMMDD>.idx`` lists the
day's filings: a preamble, the header ``CIK|Company Name|Form Type|Date Filed|File Name`` and a rule, then one row per
filing and filer (a Form 4 or a Schedule 13D/G is listed under each of its parties; one accession is fetched once).
An answer without that header is refused. The index is kept under ``raw/sec/daily_index/`` and read by every feed:
one request per day for all three. A day is asked once it has ended (the walk stops at the day before the run's
as-of). A day with no index object (a weekend is never asked; EDGAR keeps the **federal** holidays, so Columbus Day
and Veterans Day have no index although the NYSE trades) answers **403**, not 404 — see ``base.is_missing`` — and is
marked absent (``.none``) once it is more than ``SETTLED`` old; a younger one is asked again on the next run. The
SEC's own 403 (an undeclared User-Agent, the fair-access rate) is told apart by its body and stops the run, and so
does a stretch of ``MISSING_RUN`` missing weekdays EDGAR was open. That many of a day's filings in a row with no
object is not a refusal by itself -- the index does list filings EDGAR has removed, and a filer's are adjacent --
so the archive is asked for a filing it is known to hold, and only its silence stops the run (``Feed._holds``).
A marker that writes a day or a filing off is written only once the archive has answered something in the same run
(``_Held``), so a refusal of the whole bucket leaves nothing written off and the next run asks again.

**The filings.** ``GET <base>/Archives/<File Name>``: the full submission (``.txt``): the SGML header (accession, form,
period, filed date, and each party's CIK and name under ``FILER``, ``SUBJECT COMPANY``, ``FILED BY``,
``REPORTING-OWNER`` or ``ISSUER``) and every document, the XML ones between ``<XML>`` and ``</XML>``. One request a
filing, through ``sec.Pacer``. A filing the index lists but the archive does not hold (the same 403, or a 404 —
EDGAR does remove filings it has already disseminated and indexed) is marked (``.none``) and not asked again.

**The walk.** A feed walks the days from its first day (``start.json``: the run's as-of less ``FIRST_LOOKBACK`` on the
first run, moved back by a backfill) to the day before its as-of, newest first, skipping the days it finished
(``.done``, written once a day's filings are all fetched and typed). A daily run stops at its time budget
(``budget_s``) and asks to be due again the same day (``next_check_due`` = its as-of); a backfill has no budget. A
filing's raw file is its resume marker, so a stopped run loses nothing.

Checked against sec.gov on 2026-10-28: the index of 2026-10-27 and that day's 13F (202 filings), Form 4 (134, a run
stopped at its budget) and Schedule 13D/G (248) filings, every one typed. Checked again on 2026-10-30: every weekday
index from 2025-01-01 answers 200 except the federal holidays, which answer the 403 below.
"""
from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import NamedTuple

import duckdb

from ....core import calendar
from .. import base
from . import KIND, sec

INDEX_PATH = "/Archives/edgar/daily-index/{year}/QTR{q}/master.{ymd}.idx"
INDEX_HEADER = "CIK|Company Name|Form Type|Date Filed|File Name"
SETTLED = timedelta(days=3)
FIRST_LOOKBACK = timedelta(days=3)
DONE, MANIFEST, START = ".done", "manifest.jsonl", "start.json"
ROLES = ("FILER", "SUBJECT COMPANY", "FILED BY", "REPORTING-OWNER", "ISSUER")


# ── The daily index ────────────────────────────────────────────────────────────────────────────────────────────

def index_dir() -> Path:
    return base.raw_root() / "sec" / "daily_index"


def index_path(day: date) -> Path:
    return index_dir() / f"master.{day:%Y%m%d}.idx.gz"


def index_none(day: date) -> Path:
    return index_dir() / f"master.{day:%Y%m%d}.none"


def index_url(day: date) -> str:
    return sec.www_base() + INDEX_PATH.format(year=day.year, q=(day.month - 1) // 3 + 1, ymd=f"{day:%Y%m%d}")


# An index day the ``/Archives/`` bucket does not hold answers 403, not 404: ``base.is_missing`` reads the answer's
# body, and the comment above it says why that, and not the calendar, is what decides. A *filing* EDGAR has removed
# answers a plain 404 instead (measured 2026-11-06: all six SCHEDULE 13G rows of CIK 2147005 in the index of
# 2026-08-17), which ``is_missing`` reads the same way, so neither shape alone says whether the path still works.
MISSING_RUN = base.MISSING_RUN   # consecutive missing weekdays EDGAR does not keep closed: a refusal, not days
# Absences on one day past which no answer about one object can speak for the rest, so the run stops and says what
# the cluster looked like. A chosen ceiling, not a measurement: the incident of 2026-11-06 was six filings, and the
# accession prefix is the filing agent's CIK rather than the filer's, so adjacency groups an agent's batch and the
# shape of a legitimate removal cluster over twelve years is not known. The histogram in the message is what lets
# an operator judge; this only stops the walk writing off a path failure while nobody is watching.
CLUSTER_MAX = 25
PROBE_CANDIDATES = 3             # filing URLs ``_known_filing`` looks for; one of them may itself have been removed
SUBMISSION_HEAD = (b"<SEC-DOCUMENT", b"<SEC-HEADER>")   # what a full submission begins with; a 200 that has neither
                                                        # is not a filing (a throttle page has been served as one)


def _shape(absent: list[tuple[str, int]]) -> str:
    """What a cluster of absences looks like: the answers it got, and how many filing agents it spans. All one
    status and one agent is a removal; many agents, or an ``AccessDenied`` among 404s, is the path."""
    by_status = Counter(status for _, status in absent)
    agents = {acc.split("-", 1)[0] for acc, _ in absent}
    return (", ".join(f"{n}x{status}" for status, n in sorted(by_status.items()))
            + f"; {len(agents)} accession prefix" + ("es" if len(agents) != 1 else ""))


def is_submission(body: bytes) -> bool:
    """Whether a 200 carries a full submission. The index path refuses an answer that is not an index
    (``parse_index``); the data path needs the same, or a host that answers 200 with an error body for everything
    poisons the raw store, passes the archive check, and seeds it with URLs that keep passing it."""
    return any(mark in body[:4096] for mark in SUBMISSION_HEAD)


def ymd(text) -> date | None:
    """``20261027`` or ``2026-10-27`` as a date; None for anything else."""
    digits = str(text or "").strip().replace("-", "")
    if len(digits) != 8 or not digits.isdigit():
        return None
    try:
        return date(int(digits[:4]), int(digits[4:6]), int(digits[6:]))
    except ValueError:
        return None


def parse_index(body: bytes) -> list[dict]:
    """The index rows as ``{cik, company, form, filed, path, accession}``; ``SourceError`` when the answer is not
    the index."""
    lines = body.decode("latin-1").splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.strip() == INDEX_HEADER), None)
    if start is None:
        raise base.SourceError(f"sec: not the EDGAR daily index ({(lines or [''])[0][:60]!r})")
    rows = []
    for ln in lines[start + 1:]:
        parts = [p.strip() for p in ln.split("|")]
        if len(parts) != 5 or not parts[0].isdigit():
            continue
        cik, company, form, filed, path = parts
        rows.append({"cik": str(int(cik)), "company": company, "form": form.upper(), "filed": ymd(filed),
                     "path": path, "accession": Path(path).stem})
    return rows


class Index(NamedTuple):
    """One day's index. ``rows`` is None when the archive holds no index object for the day, and ``[]`` for a day
    already marked absent. ``answered`` is whether the archive itself answered this day in this run (a 200), which
    is what says it is not refusing; a kept file and a ``.none`` marker answer from disk and say nothing."""

    rows: list[dict] | None
    answered: bool = False


def read_index(ctx: base.RunContext, pacer: sec.Pacer, day: date) -> Index:
    """The index of ``day``, from the raw file once fetched. Writes no marker of its own: whether a day with no
    index object is written off is the walk's decision (``_Held``). A 403 that is a refusal and not a missing
    object raises ``SourceError``."""
    if index_none(day).exists():
        return Index([])
    if index_path(day).exists():
        return Index(parse_index(base.read_raw(index_path(day))))
    url = index_url(day)
    r = sec.get(ctx, pacer, url, accept=(403, 404))
    if base.is_missing(r):
        return Index(None)
    if r.status_code >= 400:
        raise base.SourceError(f"{sec.PROVIDER} {url}: HTTP {r.status_code}{base.refusal_note()}")
    rows = parse_index(r.content)
    base.write_raw(index_path(day), r.content)
    return Index(rows, True)


def kept_index_days() -> list[date]:
    """The days whose index file is kept, oldest first: objects the archive is known to hold."""
    days = [ymd(p.name[len("master."):len("master.") + 8]) for p in index_dir().glob("master.*.idx.gz")]
    return sorted(d for d in days if d is not None)


# ── A full submission ──────────────────────────────────────────────────────────────────────────────────────────

_FIELD = re.compile(r"^\s*([A-Z][A-Z0-9 \-/]*?):\s*(.*?)\s*$")
_DOC = re.compile(r"<DOCUMENT>(.*?)</DOCUMENT>", re.S)
_TAG = {name: re.compile(rf"<{name}>([^\n<]*)") for name in ("TYPE", "FILENAME")}
_XML = re.compile(r"<XML>(.*?)</XML>", re.S)
# The text is already decoded, so its XML declaration (which may name another encoding) is dropped before parsing.
_DECL = re.compile(r"^\s*<\?xml[^>]*\?>")


def parse_submission(text: str) -> dict:
    """``{"fields": {name: value} (the first of each), "roles": {role: [{"cik", "name"}]}, "docs": [{"type",
    "filename", "xml" or None, "text"}]}`` of a full submission's text."""
    end = text.find("</SEC-HEADER>")
    fields: dict[str, str] = {}
    roles: dict[str, list[dict]] = {}
    role = None
    for ln in (text[:end] if end >= 0 else "").splitlines():
        stripped = ln.strip()
        if stripped.rstrip(":") in ROLES and stripped.endswith(":"):
            role = stripped.rstrip(":")
            roles.setdefault(role, []).append({"cik": None, "name": None})
            continue
        m = _FIELD.match(ln)
        if not m or not m.group(2):
            continue
        name, value = m.group(1).strip(), m.group(2)
        if role and name == "CENTRAL INDEX KEY" and roles[role][-1]["cik"] is None:
            roles[role][-1]["cik"] = str(int(value)) if value.isdigit() else value
        elif role and name == "COMPANY CONFORMED NAME" and roles[role][-1]["name"] is None:
            roles[role][-1]["name"] = value
        fields.setdefault(name, value)
    docs = []
    for m in _DOC.finditer(text):
        body = m.group(1)
        typ, fname = (_TAG[t].search(body) for t in ("TYPE", "FILENAME"))
        xml = _XML.search(body)
        docs.append({"type": typ.group(1).strip().upper() if typ else "",
                     "filename": fname.group(1).strip() if fname else "",
                     "xml": xml.group(1).strip() if xml else None, "text": body})
    return {"fields": fields, "roles": roles, "docs": docs}


def tree(xml: str) -> ET.Element:
    """The XML document with every tag reduced to its lower-case local name (namespaces and their prefixes vary
    between filers and form versions). ``ET.ParseError`` for a document that does not parse."""
    root = ET.fromstring(_DECL.sub("", xml, count=1))
    for el in root.iter():
        if isinstance(el.tag, str):
            el.tag = el.tag.rsplit("}", 1)[-1].lower()
    return root


def text(el: ET.Element | None, path: str) -> str | None:
    """The stripped text at ``path`` (lower-case local names) under ``el``, or None."""
    if el is None:
        return None
    hit = el.find(path)
    if hit is None or hit.text is None:
        return None
    return hit.text.strip() or None


def number(value) -> float | None:
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def integer(value) -> int | None:
    n = number(value)
    return int(n) if n is not None else None


# ── The feed ───────────────────────────────────────────────────────────────────────────────────────────────────

class _Held:
    """What a walk has found absent but not written yet: a day with no index, and a filing the archive answered
    nothing for. Nothing is written until the archive has answered something in this run, because a refusal of the
    whole bucket answers every day and every filing the same way, and a marker written under one is never asked
    again. What is still held when the run ends is simply left: the next run asks those days and filings again.

    The evidence is kept apart by path, because the two paths fail apart: ``/Archives/edgar/daily-index/`` answered
    200 all through the incident of 2026-11-06 while ``/Archives/edgar/data/`` had lost a filer's filings. An index
    answer therefore writes off index days only; a filing marker waits for the data path itself to hand something
    over (a filing fetched, or ``Feed._holds``). Without that, a data path that answers nothing for anything would
    have every day's held filings written off by the next day's index, and a day with fewer filings than
    ``MISSING_RUN`` never reaches the check at all."""

    def __init__(self):
        self.days: list[tuple[date, dict]] = []
        self.filings: list[tuple[date, str, dict]] = []
        self.finished: list[date] = []       # days whose filings are all in, waiting on their markers

    def count(self) -> int:
        return len(self.days) + len(self.filings)

    def flush_days(self) -> list[date]:
        """Write the held index markers, on an answer from the index path; the days they finish."""
        for day, payload in self.days:
            base.write_raw_json(index_none(day), payload)
        done = [d for d, _ in self.days]
        self.days = []
        return done

    def flush_filings(self, feed: "Feed") -> list[date]:
        """Write the held filing markers, on an answer from the data path; the days they finish. A day whose
        filings were all held is finished only here, never by an index answer."""
        for day, acc, payload in self.filings:
            base.write_raw_json(feed.filing_none(day, acc), payload)
        done = self.finished
        self.filings, self.finished = [], []
        return done


class Feed:
    """One family of forms walked through the daily index. A subclass sets ``name``, ``forms``, ``sub`` (its raw
    and Parquet folder under ``sec/``) and ``budget_s``, and types a day (``type_day``)."""

    kind = KIND
    providers = (sec.PROVIDER,)
    cadence = "daily"
    starts_by_hand = True             # the first run is the server thread's, after a backup
    name: str
    forms: frozenset[str]
    sub: str
    budget_s: float | None

    def default_as_of(self) -> date:
        return calendar.today_ny()

    # Paths
    def raw_dir(self) -> Path:
        return base.raw_root() / "sec" / self.sub

    def parquet_dir(self) -> Path:
        return base.parquet_root() / "sec" / self.sub

    def day_dir(self, day: date) -> Path:
        return self.raw_dir() / day.isoformat()

    def filing_path(self, day: date, accession: str) -> Path:
        return self.day_dir(day) / f"{accession}.txt.gz"

    def filing_none(self, day: date, accession: str) -> Path:
        return self.day_dir(day) / f"{accession}.none"

    def is_done(self, day: date) -> bool:
        return (self.day_dir(day) / DONE).exists()

    def start_day(self, as_of: date) -> date:
        """The walk's first day: kept in ``start.json``, the as-of less ``FIRST_LOOKBACK`` when there is none."""
        path = self.raw_dir() / START
        if path.exists():
            return date.fromisoformat(base.read_raw_json(path)["start"])
        start = as_of - FIRST_LOOKBACK
        base.write_raw_json(path, {"start": start.isoformat()})
        return start

    def first_day(self) -> date | None:
        """The walk's first day (``start.json``), None before any run; read only."""
        path = self.raw_dir() / START
        return date.fromisoformat(base.read_raw_json(path)["start"]) if path.exists() else None

    def move_start(self, day: date) -> None:
        path = self.raw_dir() / START
        if not path.exists() or date.fromisoformat(base.read_raw_json(path)["start"]) > day:
            base.write_raw_json(path, {"start": day.isoformat()})

    # The walk
    def walk(self, ctx: base.RunContext, first: date, last: date, as_of: date,
             budget_s: float | None) -> tuple[list[date], list[date], bool]:
        """Walk the weekdays from ``last`` back to ``first``: ``(days finished, days begun, more left)``."""
        pacer = sec.Pacer(ctx)
        t0 = ctx.clock()
        complete, partial, fetched = [], [], 0
        day = last
        held, streak = _Held(), self._streak(ctx, pacer)
        last_ok = None                     # a filing this run fetched, to ask the archive with (see ``_holds``)
        probed: dict[date, int] = {}       # probes per day, so a bad day's notes are one line
        probes = 0                         # which candidate to ask, so a long run does not pin one in a cache
        while day >= first:
            next_probe = MISSING_RUN       # doubles after each probe: O(log n) of them on a day of absences
            absent: list[tuple[str, int]] = []      # this day's absences, for CLUSTER_MAX and the messages
            wrote = 0                               # of them already written off, after a probe answered
            if day.weekday() < 5 and not self.is_done(day):
                if budget_s is not None and ctx.clock() - t0 >= budget_s:
                    complete += self._release(ctx, pacer, held)
                    return self._stopped(ctx, complete, partial, fetched)
                index = read_index(ctx, pacer, day)
                if index.answered:
                    complete += held.flush_days()
                if index.rows is None:
                    if as_of - day > SETTLED:
                        streak.missing(ctx, day)
                        held.days.append((day, {"status": 403, "url": index_url(day), "run_id": ctx.run_id,
                                                "closed": calendar.is_federal_holiday(day)}))
                    day -= timedelta(days=1)
                    continue
                wanted: dict[str, dict] = {}
                for r in index.rows:
                    if r["form"] in self.forms:
                        wanted.setdefault(r["accession"], r)
                todo = [r for acc, r in sorted(wanted.items()) if not self.filing_path(day, acc).exists()
                        and not self.filing_none(day, acc).exists()]
                gone_run = 0
                for r in todo:
                    if budget_s is not None and ctx.clock() - t0 >= budget_s:
                        complete += self._release(ctx, pacer, held)
                        partial.append(day)
                        return self._stopped(ctx, complete, partial, fetched)
                    gone = self._fetch_filing(ctx, pacer, day, r)
                    fetched += 1
                    if gone is None:
                        gone_run = 0
                        last_ok = self.filing_url(r)
                        complete += held.flush_filings(self)
                        continue
                    held.filings.append((day, *gone))
                    gone_run += 1
                    absent.append((r["accession"], gone[1]["status"]))
                    if len(absent) >= CLUSTER_MAX:
                        # Counted over the whole day, not since the last check: an answer about one object cannot
                        # speak for a cluster this size, however many times it came back 200 on the way here.
                        raise base.SourceError(f"sec: {len(absent)} filings of {day} the archive answers nothing "
                                               f"for ({_shape(absent)}), {wrote} of them already written off; more "
                                               f"than {CLUSTER_MAX} on one day is not an agent's filings removed, "
                                               "so the run stops here")
                    if gone_run >= next_probe:
                        try:
                            holds, why = self._holds(ctx, pacer, last_ok, probes)
                        except base.SourceError as e:
                            # The probe itself could not be made (a 5xx, a dropped connection). Nothing is written
                            # off and the day stays open, but a twelve-year walk does not end over it.
                            ctx.notes.append(f"{day}: the archive could not be asked for a filing it holds "
                                             f"({e}); {len(held.filings)} filing markers stay held")
                            gone_run, next_probe = 0, next_probe * 2
                            continue
                        if not holds:
                            raise base.SourceError(f"sec: {len(absent)} filings of {day} the archive does not "
                                                   f"answer for ({_shape(absent)}), and it did not hand over one "
                                                   f"it holds either ({why}); no filing is written off")
                        probes += 1
                        probed[day] = probed.get(day, 0) + 1
                        wrote += len(held.filings)
                        complete += held.flush_filings(self)   # the probe is the data path's own answer
                        gone_run, next_probe = 0, next_probe * 2
                if probed.get(day):
                    ctx.notes.append(f"{day}: {probed[day]} run(s) of absent filings, and the archive handed over "
                                     f"one it holds each time ({_shape(absent)})")
                # A day is finished once its markers are written, never before: a ``.done`` would skip it for good.
                (held.finished if held.filings else complete).append(day)
            day -= timedelta(days=1)
        complete += self._release(ctx, pacer, held)
        ctx.notes.append(f"{fetched} filings fetched")
        return complete, partial, False

    def _release(self, ctx: base.RunContext, pacer: sec.Pacer, held: _Held) -> list[date]:
        """Write what is still held at the end of a walk. Nothing the archive answered came after it, so each path
        is asked for an object it is known to hold: the index path for the held days, the data path for the held
        filings (``_holds``). A 200 says that path is not refusing and its markers are written; anything else
        leaves them, and the next run asks those days and filings again. The two are asked apart because they fail
        apart -- on 2026-11-06 the index path answered 200 throughout while the data path had lost a filer."""
        done: list[date] = []
        if held.days:
            if self._archive_answers(ctx, pacer):
                done += held.flush_days()
            else:
                ctx.notes.append(f"{len(held.days)} absent day markers held back: the index path answered nothing, "
                                 "so the next run asks those days again")
        if held.filings:
            holds, why = self._holds(ctx, pacer, None)
            if holds:
                done += held.flush_filings(self)
            else:
                ctx.notes.append(f"{len(held.filings)} absent filing markers held back: the archive did not hand "
                                 f"over one it holds ({why}), so the next run asks those filings again")
        return done

    def _archive_answers(self, ctx: base.RunContext, pacer: sec.Pacer) -> bool:
        """Whether the archive answers at all: one more request for the newest index file already kept."""
        kept = kept_index_days()
        if not kept:
            return False
        r = sec.get(ctx, pacer, index_url(kept[-1]), accept=(403, 404))
        return r.status_code == 200

    def _streak(self, ctx: base.RunContext, pacer: sec.Pacer) -> base.MissingStreak:
        """The walk's days with no index (``base.MissingStreak``). A federal holiday explains one; a weekday that is
        none is noted, and a stretch of them stops the run, because a refusal of the whole bucket answers every day
        the same way and would otherwise walk past real days and mark them absent for good -- unless the index path
        hands over a day it is known to hold (``_archive_answers``), which says the bucket is answering and these
        days truly have no index (#507: a closure the calendar does not know yet must not wedge a long backfill)."""
        return base.MissingStreak("daily index", probe=lambda: self._archive_answers(ctx, pacer))

    def _stopped(self, ctx, complete, partial, fetched):
        ctx.notes.append(f"{fetched} filings fetched; stopped at the time budget, the rest on the next run")
        return complete, partial, True

    def filing_url(self, row: dict) -> str:
        return f"{sec.www_base()}/Archives/{row['path']}"

    def _known_filing(self, run_id: str | None = None) -> list[str]:
        """Filing URLs the archive once served, rebuilt against the base in force now: what ``_holds`` asks. Lines
        another run wrote come first, because a URL this run fetched minutes ago is the one most likely to be
        answered from an edge cache, and the failure this has to catch is the path breaking part-way through a run.
        Several, because one of them may itself be a filing EDGAR has since removed -- the very thing this guard is
        about -- and because a day folder can hold no manifest at all (a day with no filing of this feed's forms,
        or one written off whole, still gets a folder), so the budget counts candidates and not folders."""
        root = self.raw_dir()
        if not root.is_dir():
            return []
        mine, others = [], []
        for d in sorted((p for p in root.iterdir() if p.is_dir()), reverse=True):
            man = d / MANIFEST
            if not man.exists():
                continue
            for line in man.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    url, was = entry.get("url"), entry.get("run_id")
                except (ValueError, AttributeError, TypeError):
                    continue            # a killed run can leave a half-written line; it is not evidence either way
                if not url or "/Archives/" not in url:
                    continue
                here = sec.www_base() + "/Archives/" + url.split("/Archives/", 1)[1]
                (mine if run_id is not None and was == run_id else others).append(here)
                break
            if len(others) >= PROBE_CANDIDATES:
                break
        return (others + mine)[:PROBE_CANDIDATES]

    def _holds(self, ctx: base.RunContext, pacer: sec.Pacer, last_ok: str | None, nth: int = 0) -> tuple[bool, str]:
        """``(whether the archive still hands over a filing it is known to hold, what it said)``. This is the one
        check that tells a filer whose filings EDGAR removed from a path that answers nothing for anything: the
        answer to the absent filing is the same either way. Measured 2026-11-06 on the live site, which is why it
        exists: all six SCHEDULE 13G rows of CIK 2147005 in the index of 2026-08-17 answer 404, the accession
        folder answers 404, and that CIK's submissions.json holds no filing at all, while a filing of the same day
        in the same path answers 200.

        ``nth`` rotates which candidate is asked first, so a run of many hours does not ask one URL hundreds of
        times and read a warm cache as an answer about the origin. A 200 must carry a submission: a host that
        answers 200 with an error body for everything would otherwise pass this and be taken as proof."""
        candidates = self._known_filing(ctx.run_id)
        if candidates:
            candidates = candidates[nth % len(candidates):] + candidates[:nth % len(candidates)]
        said = []
        for url in [*candidates, *([last_ok] if last_ok else [])]:
            r = sec.get(ctx, pacer, url, accept=(403, 404))
            if r.status_code == 200 and is_submission(r.content):
                return True, "200"
            name = url.rsplit("/", 1)[1]
            if r.status_code == 200:
                said.append(f"{name}: 200 but not a submission")
            elif base.is_missing(r):
                said.append(f"{name}: no object ({r.status_code})")
            else:
                said.append(f"{name}: {r.status_code}{base.refusal_note()}")
        return False, "; ".join(said) or "no filing it is known to hold"

    def _fetch_filing(self, ctx: base.RunContext, pacer: sec.Pacer, day: date,
                      row: dict) -> tuple[str, dict] | None:
        """Fetch one filing. None once it is written; ``(accession, payload)`` when the archive has no object for
        it, for the caller to mark absent."""
        url = self.filing_url(row)
        r = sec.get(ctx, pacer, url, accept=(403, 404))
        acc = row["accession"]
        if base.is_missing(r):
            return acc, {"status": r.status_code, "url": url, "run_id": ctx.run_id, "form": row["form"],
                         "cik": row["cik"]}
        if r.status_code >= 400:
            raise base.SourceError(f"{sec.PROVIDER} {url}: HTTP {r.status_code}{base.refusal_note()}")
        if not is_submission(r.content):
            raise base.SourceError(f"{sec.PROVIDER} {url}: a 200 that is not a full submission "
                                   f"({len(r.content)} bytes); nothing is written")
        base.write_raw(self.filing_path(day, acc), r.content)
        with open(self.day_dir(day) / MANIFEST, "a", encoding="utf-8") as f:
            f.write(json.dumps({"accession": acc, "form": row["form"], "cik": row["cik"], "filed": str(row["filed"]),
                                "fetched_at": time.time(), "run_id": ctx.run_id, "url": url}) + "\n")
        return None

    def manifest(self, day: date) -> dict[str, dict]:
        path = self.day_dir(day) / MANIFEST
        if not path.exists():
            return {}
        out = {}
        for ln in path.read_text(encoding="utf-8").splitlines():
            if ln.strip():
                rec = json.loads(ln)
                out[rec["accession"]] = rec
        return out

    def filings_of(self, day: date) -> Iterator[tuple[str, dict, str]]:
        """``(accession, manifest entry, submission text)`` of every filing fetched for ``day``, by accession, read one
        at a time: a deadline day's 5,400 full submissions never sit in Python at once (``Appender`` does the same
        for their rows)."""
        man = self.manifest(day)
        for path in sorted(self.day_dir(day).glob("*.txt.gz")):
            acc = path.name[:-len(".txt.gz")]
            entry = man.get(acc) or {"accession": acc, "fetched_at": path.stat().st_mtime, "run_id": None}
            yield acc, entry, base.read_raw(path).decode("utf-8", errors="replace")

    # The dataset steps
    def fetch(self, ctx: base.RunContext, as_of: date) -> list[Path]:
        complete, partial, more = self.walk(ctx, self.start_day(as_of), as_of - timedelta(days=1), as_of,
                                            self.budget_s)
        ctx.pending[self.name] = {"complete": complete, "more": more}
        return [self.day_dir(d) for d in complete + partial]

    def backfill(self, ctx: base.RunContext, start: date | None, end: date) -> None:
        today = calendar.today_ny()
        first = start or end
        self.move_start(first)
        complete, partial, _ = self.walk(ctx, first, min(end, today - timedelta(days=1)), today, None)
        ctx.pending[self.name] = {"complete": complete, "more": False}
        self.to_parquet_all(ctx, [self.day_dir(d) for d in complete + partial])

    def to_parquet(self, ctx: base.RunContext, raw: Path) -> list[Path]:
        return self.to_parquet_all(ctx, [raw])

    def to_parquet_all(self, ctx: base.RunContext, day_dirs: list[Path]) -> list[Path]:
        if base.raw_only():
            # Raw-only (--no-parquet, #498): typing writes no partition (base.write_parquet), and the ``.done``
            # marker written beside it is the walk's skip test (is_done), so a marker without a partition would
            # hide the day's filings from every later run for good. The filings stay in raw and the day stays
            # unmarked, so the next ordinary run walks it again and types it at no provider call.
            days = sorted(set(day_dirs))
            ctx.notes.append(f"{self.name}: raw only, {len(days)} day(s) staged and not typed")
            return []
        out = []
        complete = set((ctx.pending.get(self.name) or {}).get("complete", []))
        for d in sorted(set(day_dirs)):
            day = date.fromisoformat(d.name)
            out += self.type_day(ctx, day)
            if day in complete:
                d.mkdir(parents=True, exist_ok=True)
                (d / DONE).write_text(json.dumps({"run_id": ctx.run_id, "at": time.time()}), encoding="utf-8")
        return out

    def type_day(self, ctx: base.RunContext, day: date) -> list[Path]:
        raise NotImplementedError

    def more_left(self, ctx: base.RunContext) -> bool:
        return bool((ctx.pending.get(self.name) or {}).get("more"))

    def write_day(self, con: duckdb.DuckDBPyConnection, table: str, sub: str, day: date, order: str) -> Path:
        """``table`` (of ``con``) as the day's partition ``<parquet_dir>/<sub>/day=<day>/part-0.parquet``."""
        final = self.parquet_dir() / sub / f"day={day.isoformat()}" / "part-0.parquet"
        return base.write_parquet(con, f"SELECT * FROM {table} ORDER BY {order}", final)


class Appender:
    """Rows into a table of an in-memory connection a block at a time, so a deadline day's millions of holdings
    never sit in Python at once."""

    BLOCK = 50_000

    def __init__(self, con: duckdb.DuckDBPyConnection, table: str, schema: list[tuple[str, str]]):
        self.con, self.table, self.schema = con, table, schema
        self.rows: list[dict] = []
        self.count = 0
        cols = ", ".join(f'"{c}" {t}' for c, t in schema)
        con.execute(f"CREATE OR REPLACE TABLE {table} ({cols})")

    @property
    def total(self) -> int:
        """The rows added so far, flushed or not."""
        return self.count + len(self.rows)

    def add(self, rows: list[dict]) -> None:
        self.rows += rows
        if len(self.rows) >= self.BLOCK:
            self.flush()

    def flush(self) -> None:
        if not self.rows:
            return
        import pandas as pd
        frame = pd.DataFrame([{c: r.get(c) for c, _ in self.schema} for r in self.rows],
                             columns=[c for c, _ in self.schema])
        self.con.register("_append_frame", frame)
        try:
            casts = ", ".join(f'CAST("{c}" AS {t})' for c, t in self.schema)
            self.con.execute(f"INSERT INTO {self.table} SELECT {casts} FROM _append_frame")
        finally:
            self.con.unregister("_append_frame")
        self.count += len(self.rows)
        self.rows = []
