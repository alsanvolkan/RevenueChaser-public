"""The EDGAR walk the SEC filing datasets share (``sec/13f``, ``sec/form4``, ``sec/13dg``): the daily index, one
request per filing, and a time budget per run.

**The daily index.** ``GET <RC_SEC_WWW_BASE>/Archives/edgar/daily-index/<YYYY>/QTR<q>/master.<YYYYMMDD>.idx`` lists the
day's filings: a preamble, the header ``CIK|Company Name|Form Type|Date Filed|File Name`` and a rule, then one row per
filing and filer (a Form 4 or a Schedule 13D/G is listed under each of its parties; one accession is fetched once).
An answer without that header is refused. The index is kept under ``raw/sec/daily_index/`` and read by every feed:
one request per day for all three. A day is asked once it has ended (the walk stops at the day before the run's
as-of); a 404 of a day more than ``SETTLED`` old (a weekend is never asked; an EDGAR holiday answers 404) is marked
absent (``.none``), a younger one is asked again on the next run.

**The filings.** ``GET <base>/Archives/<File Name>``: the full submission (``.txt``): the SGML header (accession, form,
period, filed date, and each party's CIK and name under ``FILER``, ``SUBJECT COMPANY``, ``FILED BY``,
``REPORTING-OWNER`` or ``ISSUER``) and every document, the XML ones between ``<XML>`` and ``</XML>``. One request a
filing, through ``sec.Pacer``. A filing the index lists but the archive answers 404 for is marked (``.none``) and
not asked again.

**The walk.** A feed walks the days from its first day (``start.json``: the run's as-of less ``FIRST_LOOKBACK`` on the
first run, moved back by a backfill) to the day before its as-of, newest first, skipping the days it finished
(``.done``, written once a day's filings are all fetched and typed). A daily run stops at its time budget
(``budget_s``) and asks to be due again the same day (``next_check_due`` = its as-of); a backfill has no budget. A
filing's raw file is its resume marker, so a stopped run loses nothing.

Checked against sec.gov on 2026-10-28: the index of 2026-10-27 and that day's 13F (202 filings), Form 4 (134, a run
stopped at its budget) and Schedule 13D/G (248) filings, every one typed.
"""
from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path

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


def read_index(ctx: base.RunContext, pacer: sec.Pacer, day: date, as_of: date) -> list[dict] | None:
    """The index rows of ``day``: from the raw file once fetched, ``[]`` for a day with no index, None while it is
    not published yet."""
    if index_none(day).exists():
        return []
    if index_path(day).exists():
        return parse_index(base.read_raw(index_path(day)))
    url = index_url(day)
    r = sec.get(ctx, pacer, url, accept=(404,))
    if r.status_code == 404:
        if as_of - day > SETTLED:
            base.write_raw_json(index_none(day), {"status": 404, "url": url, "run_id": ctx.run_id})
            return []
        return None
    rows = parse_index(r.content)
    base.write_raw(index_path(day), r.content)
    return rows


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
        while day >= first:
            if day.weekday() < 5 and not self.is_done(day):
                if budget_s is not None and ctx.clock() - t0 >= budget_s:
                    return self._stopped(ctx, complete, partial, fetched)
                rows = read_index(ctx, pacer, day, as_of)
                if rows is not None:
                    wanted: dict[str, dict] = {}
                    for r in rows:
                        if r["form"] in self.forms:
                            wanted.setdefault(r["accession"], r)
                    todo = [r for acc, r in sorted(wanted.items()) if not self.filing_path(day, acc).exists()
                            and not self.filing_none(day, acc).exists()]
                    for r in todo:
                        if budget_s is not None and ctx.clock() - t0 >= budget_s:
                            partial.append(day)
                            return self._stopped(ctx, complete, partial, fetched)
                        self._fetch_filing(ctx, pacer, day, r)
                        fetched += 1
                    complete.append(day)
            day -= timedelta(days=1)
        ctx.notes.append(f"{fetched} filings fetched")
        return complete, partial, False

    def _stopped(self, ctx, complete, partial, fetched):
        ctx.notes.append(f"{fetched} filings fetched; stopped at the time budget, the rest on the next run")
        return complete, partial, True

    def _fetch_filing(self, ctx: base.RunContext, pacer: sec.Pacer, day: date, row: dict) -> None:
        url = f"{sec.www_base()}/Archives/{row['path']}"
        r = sec.get(ctx, pacer, url, accept=(404,))
        acc = row["accession"]
        if r.status_code == 404:
            base.write_raw_json(self.filing_none(day, acc), {"status": 404, "url": url, "run_id": ctx.run_id,
                                                             "form": row["form"], "cik": row["cik"]})
            return
        base.write_raw(self.filing_path(day, acc), r.content)
        with open(self.day_dir(day) / MANIFEST, "a", encoding="utf-8") as f:
            f.write(json.dumps({"accession": acc, "form": row["form"], "cik": row["cik"], "filed": str(row["filed"]),
                                "fetched_at": time.time(), "run_id": ctx.run_id, "url": url}) + "\n")

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
