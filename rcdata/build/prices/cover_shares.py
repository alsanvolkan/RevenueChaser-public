"""``sec/cover_shares``: the shares outstanding per class from the cover page of each new 10-K and 10-Q.

The cover's ``dei:EntityCommonStockSharesOutstanding`` is a dimensioned fact for an issuer with several classes (one
context per ``us-gaap:StatementClassOfStockAxis`` member), and the SEC's company facts leave dimensioned facts out,
so it is read from the filing's own XBRL instance: ``<primary document stem>_htm.xml`` in the filing's folder, the
instance EDGAR extracts from an inline XBRL filing. Each class's ``dei:TradingSymbol`` (a fact of the 12(b) table
in a context with the same class member) names the symbol it trades under; a class with no symbol (Alphabet's Class
B) is kept, unlisted. A fact with any other dimension (``dei:LegalEntityAxis``, a co-registrant) is not the issuer's.

**Which filings.** A run reads the SEC's daily index (``daily-index/<year>/QTR<n>/master.<YYYYMMDD>.idx``) of every
weekday of the ``LOOKBACK`` days before ``as_of`` and takes the ``FORMS`` filings of Tier A CIKs. Until every Tier A
CIK has one filing, a run also fills ``FILL_PER_RUN`` CIKs that have none (by CIK), with the newest ``FORMS`` filing
their submissions file names. Each filing needs its submissions file (``data.sec.gov``; the primary document's name
and whether it is inline XBRL) and the instance: two requests, ``SEC_INTERVAL_S`` apart (under 5 a second) and
through the box's shared SEC bucket (``core/sec_throttle.py``), with the builder's User-Agent.

**Raw**: ``raw/sec/daily_index/master.<YYYYMMDD>.idx.gz`` (a ``.missing`` marker for a day with no index, two days
past, and only when EDGAR's closure explains it: ``base.MissingStreak``), and ``raw/sec/cover_shares/cik=<CIK>/<accession>.json.gz``, the cover facts only (an instance runs to
megabytes); ``<accession>.none.json.gz`` for a filing with no instance to read, ``_asked.json.gz`` for a CIK whose
submissions file has no ``FORMS`` filing. A filing that has a raw file is never asked again.

**Parquet**: ``parquet/sec/cover_shares/year=<year filed>/part-0.parquet`` keyed ``(accession, class_member)``
(``class_member`` empty for a single class). ``as_of`` is the cover's date (the context's instant), ``filed`` the
filing date, ``period_end`` the period the filing reports.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path

import duckdb

from ....core import calendar
from .. import base
from . import KIND, Spacer, spine_rows, stage

NAME = "sec/cover_shares"
PROVIDER = "sec"
FORMS = ("10-K", "10-Q", "20-F", "40-F", "10-KT", "10-QT")
LOOKBACK = timedelta(days=7)
FILL_PER_RUN = 500
SEC_INTERVAL_S = 0.25
# A day's index that is still missing this many days later is a day with no index (a holiday): it is not asked again.
MISSING_AFTER = timedelta(days=2)
CLASS_AXIS = "StatementClassOfStockAxis"
SCHEMA = [("cik", "TEXT"), ("accession", "TEXT"), ("form", "TEXT"), ("filed", "DATE"), ("period_end", "DATE"),
          ("class_member", "TEXT"), ("symbol", "TEXT"), ("shares", "DOUBLE"), ("as_of", "DATE"),
          ("source_as_of", "DATE"), ("fetched_at", "DOUBLE"), ("run_id", "TEXT")]
KEY = ["accession", "class_member"]


def _www() -> str:
    from ....core.providers import sec_edgar
    return sec_edgar.SEC_WWW_BASE.rstrip("/")


def _data() -> str:
    from ....core.providers import sec_edgar
    return sec_edgar.SEC_DATA_BASE.rstrip("/")


def index_url(day: date) -> str:
    return f"{_www()}/Archives/edgar/daily-index/{day.year}/QTR{(day.month - 1) // 3 + 1}/master.{day:%Y%m%d}.idx"


def submissions_url(cik: str) -> str:
    return f"{_data()}/submissions/CIK{int(cik):010d}.json"


def instance_url(cik: str, accession: str, primary_document: str) -> str:
    stem = primary_document.rsplit(".", 1)[0]
    return f"{_www()}/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{stem}_htm.xml"


def index_path(day: date) -> Path:
    return base.raw_root() / "sec" / "daily_index" / f"master.{day:%Y%m%d}.idx.gz"


def cik_dir(cik: str) -> Path:
    return base.raw_root() / "sec" / "cover_shares" / f"cik={cik}"


def filing_path(cik: str, accession: str) -> Path:
    return cik_dir(cik) / f"{accession}.json.gz"


def none_path(cik: str, accession: str) -> Path:
    return cik_dir(cik) / f"{accession}.none.json.gz"


def partition_path(year: int) -> Path:
    return base.parquet_root() / "sec" / "cover_shares" / f"year={year}" / "part-0.parquet"


def parse_index(text: str) -> list[dict]:
    """The rows of a daily ``master`` index: ``{cik, company, form, filed, accession}``."""
    out, body = [], False
    for line in text.splitlines():
        if line.startswith("---"):
            body = True
            continue
        parts = line.split("|")
        if not body or len(parts) != 5 or not parts[0].strip().isdigit():
            continue
        cik, company, form, filed, filename = (p.strip() for p in parts)
        acc = filename.rsplit("/", 1)[-1].removesuffix(".txt")
        out.append({"cik": str(int(cik)), "company": company, "form": form, "filed": filed, "accession": acc})
    return out


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _dei(tag: str) -> bool:
    return tag.startswith("{") and "xbrl.sec.gov/dei" in tag.split("}", 1)[0]


def cover_facts(xml: bytes) -> dict:
    """The cover facts of an XBRL instance: ``{document_type, period_end, symbols: {member: symbol}, shares: [{
    class_member, symbol, shares, as_of}]}``, the shares in document order. A fact with a dimension other than the
    class axis is left out."""
    root = ET.fromstring(xml)
    contexts: dict[str, tuple[dict, str | None]] = {}
    for c in root:
        if _local(c.tag) != "context":
            continue
        members, day = {}, None
        for el in c.iter():
            name = _local(el.tag)
            if name == "explicitMember":
                members[str(el.get("dimension") or "")] = (el.text or "").strip()
            elif name in ("instant", "endDate"):
                day = (el.text or "").strip()
        contexts[c.get("id")] = (members, day)

    def member_of(ref: str) -> tuple[bool, str | None]:
        members = contexts.get(ref, ({}, None))[0]
        other = [d for d in members if not d.endswith(CLASS_AXIS)]
        cls = [m for d, m in members.items() if d.endswith(CLASS_AXIS)]
        return not other, (cls[0] if cls else None)

    out = {"document_type": None, "period_end": None, "symbols": {}, "shares": []}
    raw_shares = []
    for el in root:
        if not _dei(el.tag) or (el.get("{http://www.w3.org/2001/XMLSchema-instance}nil") == "true"):
            continue
        name, text, ref = _local(el.tag), (el.text or "").strip(), el.get("contextRef")
        own, member = member_of(ref)
        if not own:
            continue
        if name == "DocumentType":
            out["document_type"] = text
        elif name == "DocumentPeriodEndDate":
            out["period_end"] = text
        elif name == "TradingSymbol" and text:
            out["symbols"].setdefault(member or "", text.upper())
        elif name == "EntityCommonStockSharesOutstanding" and text:
            try:
                raw_shares.append((member, float(text), contexts.get(ref, ({}, None))[1]))
            except ValueError:
                continue
    single = next(iter(out["symbols"].values())) if len(set(out["symbols"].values())) == 1 else None
    for member, shares, day in raw_shares:
        symbol = out["symbols"].get(member or "") or (single if member is None else None)
        out["shares"].append({"class_member": member, "symbol": symbol, "shares": shares, "as_of": day})
    return out


def _recent(body: dict) -> dict[str, dict]:
    """``{accession: {form, filed, report_date, primary_document, inline}}`` of a submissions file's recent filings."""
    r = ((body or {}).get("filings") or {}).get("recent") or {}
    accs = r.get("accessionNumber") or []

    def col(name, i):
        values = r.get(name) or []
        return values[i] if i < len(values) else None
    return {a: {"accession": a, "form": col("form", i), "filed": col("filingDate", i),
                "report_date": col("reportDate", i), "primary_document": col("primaryDocument", i),
                "inline": bool(col("isInlineXBRL", i))} for i, a in enumerate(accs)}


class MissingObject(base.SourceError):
    """The host answered that it holds no object for the URL: a 404, or the ``/Archives/`` bucket's 403
    ``AccessDenied`` (``base.is_missing``). A plain 403 is the SEC refusing and stays a ``SourceError``, because
    writing a day or a filing off under a refusal would never ask for it again."""


class SecCoverShares:
    name = NAME
    kind = KIND
    providers = (PROVIDER,)
    tables = ("shares_class",)
    cadence = "daily"
    starts_by_hand = True

    def default_as_of(self) -> date:
        return calendar.today_ny()

    def _getter(self, ctx: base.RunContext):
        from ....core import sec_throttle
        spacer = Spacer(SEC_INTERVAL_S, ctx.clock, ctx.sleep)

        def get(url: str):
            sec_throttle.pace()
            spacer.wait()
            r = base.http_get(ctx, PROVIDER, url, accept=(403, 404))
            if base.is_missing(r):
                raise MissingObject(f"{PROVIDER} {url}: HTTP {r.status_code} (the host holds no such object)")
            if r.status_code >= 400:
                # ``accept`` means ``http_get`` returned instead of raising, so it counted no failure for this one.
                ctx.count(PROVIDER, failures=1)
                raise base.SourceError(f"{PROVIDER} {url}: HTTP {r.status_code}{base.refusal_note()}")
            return r
        return get

    def _index(self, ctx: base.RunContext, day: date, as_of: date, get,
               streak: base.MissingStreak | None = None) -> str | None:
        path = index_path(day)
        missing = path.with_name(path.name.removesuffix(".gz") + ".missing")
        if path.exists():
            return base.read_raw(path).decode("latin-1")
        if missing.exists():
            return None
        try:
            r = get(index_url(day))
        except base.CapReached:
            raise
        except MissingObject:
            # The archive answers the same for an index it does not hold and for a path that refuses everything, so
            # a day is written off only when EDGAR's closure explains it; a stretch of open weekdays raises instead.
            if as_of - day >= MISSING_AFTER and (streak or base.MissingStreak("daily index")).missing(ctx, day):
                base.write_raw(missing, b"")
            return None
        base.write_raw(path, r.content)
        return r.content.decode("latin-1")

    def fetch(self, ctx: base.RunContext, as_of: date) -> list[Path]:
        tier_a = sorted({str(r["cik"]) for r in spine_rows() if r.get("tier") == "A" and r.get("cik")}, key=int)
        if not tier_a:
            raise base.SourceError("the spine holds no Tier A CIK (spine.duckdb not built?): nothing to read")
        get = self._getter(ctx)
        wanted: dict[str, set[str]] = {}
        known = set(tier_a)
        day = as_of - LOOKBACK
        # The whole day walk comes before any filing is read, so a path refusing everything stops this run here
        # (``base.MissingStreak``) and no filing is written off under it either.
        streak = base.MissingStreak("daily index")
        while day < as_of:
            if day.weekday() < 5:
                for row in parse_index(self._index(ctx, day, as_of, get, streak) or ""):
                    if row["cik"] in known and row["form"] in FORMS:
                        wanted.setdefault(row["cik"], set()).add(row["accession"])
            day += timedelta(days=1)
        fill = [c for c in tier_a if c not in wanted and not cik_dir(c).is_dir()][:FILL_PER_RUN]
        out: list[Path] = []
        for cik in sorted(set(wanted) | set(fill), key=int):
            todo = sorted(a for a in wanted.get(cik, ()) if not filing_path(cik, a).exists()
                          and not none_path(cik, a).exists())
            if cik in wanted and not todo:
                continue
            try:
                filings = _recent(get(submissions_url(cik)).json())
            except base.CapReached:
                raise
            except (base.SourceError, ValueError) as e:
                if isinstance(e, base.SourceError) and not isinstance(e, MissingObject):
                    raise
                ctx.issue("submissions_missing", cik, str(e)[-160:])
                base.write_raw_json(cik_dir(cik) / "_asked.json.gz", {"as_of": as_of.isoformat(), "error": str(e)})
                continue
            if cik not in wanted:
                newest = [f for f in filings.values() if f["form"] in FORMS]
                if not newest:
                    base.write_raw_json(cik_dir(cik) / "_asked.json.gz", {"as_of": as_of.isoformat(), "forms": []})
                    continue
                todo = [max(newest, key=lambda f: (f["filed"] or "", f["accession"]))["accession"]]
            for acc in todo:
                f = filings.get(acc)
                if f is None:
                    ctx.issue("filing_not_in_submissions", f"{cik}:{acc}", "the daily index names it; the "
                              "submissions file's recent filings do not")
                    continue
                path = self._filing(ctx, cik, f, as_of, get)
                if path is not None:
                    out.append(path)
        return out + [p for p in self._untyped() if p not in out]

    def _filing(self, ctx: base.RunContext, cik: str, f: dict, as_of: date, get) -> Path | None:
        acc, doc = f["accession"], f.get("primary_document") or ""
        meta = {"cik": cik, **f, "as_of": as_of.isoformat(), "run_id": ctx.run_id}
        if not f.get("inline") or not doc.lower().endswith((".htm", ".html")):
            ctx.issue("no_inline_xbrl", f"{cik}:{acc}", f"{f.get('form')} {doc}: not inline XBRL, no instance")
            base.write_raw_json(none_path(cik, acc), {**meta, "why": "not inline XBRL"})
            return None
        url = instance_url(cik, acc, doc)
        try:
            facts = cover_facts(get(url).content)
        except base.CapReached:
            raise
        except (base.SourceError, ET.ParseError) as e:
            if isinstance(e, base.SourceError) and not isinstance(e, MissingObject):
                raise
            ctx.issue("instance_unreadable", f"{cik}:{acc}", f"{type(e).__name__}: {str(e)[-160:]}")
            base.write_raw_json(none_path(cik, acc), {**meta, "why": f"{type(e).__name__}"})
            return None
        return base.write_raw_json(filing_path(cik, acc), {**meta, "url": url, "facts": facts,
                                                           "fetched_at": calendar._now().timestamp()})

    def _untyped(self) -> list[Path]:
        """Raw filings no Parquet row holds yet (a run cut off between its fetch and its typing)."""
        root = base.raw_root() / "sec" / "cover_shares"
        files = [p for p in root.glob("cik=*/*.json.gz") if not p.name.startswith("_")
                 and not p.name.endswith(".none.json.gz")] if root.is_dir() else []
        parts = sorted((base.parquet_root() / "sec" / "cover_shares").glob("year=*/part-0.parquet"))
        if not files or not parts:
            return sorted(files)
        con = duckdb.connect()
        try:
            have = {r[0] for r in con.execute("SELECT DISTINCT accession FROM read_parquet(["
                                              + ", ".join(base.sql_path(p) for p in parts) + "])").fetchall()}
        finally:
            con.close()
        return sorted(p for p in files if p.name.removesuffix(".json.gz") not in have)

    def backfill(self, ctx: base.RunContext, start, end: date) -> None:
        self.to_parquet_all(ctx, self.fetch(ctx, end))

    def to_parquet(self, ctx: base.RunContext, raw: Path) -> list[Path]:
        return self.to_parquet_all(ctx, [raw])

    def to_parquet_all(self, ctx: base.RunContext, raws: list[Path]) -> list[Path]:
        years: dict[int, list[dict]] = {}
        for raw in raws:
            p = base.read_raw_json(raw)
            facts = p.get("facts") or {}
            filed = date.fromisoformat(p["filed"]) if p.get("filed") else None
            if filed is None:
                continue
            for f in facts.get("shares") or []:
                years.setdefault(filed.year, []).append({
                    "cik": p["cik"], "accession": p["accession"], "form": p.get("form"), "filed": filed,
                    "period_end": facts.get("period_end") or p.get("report_date") or None,
                    "class_member": f.get("class_member") or "", "symbol": f.get("symbol"), "shares": f["shares"],
                    "as_of": f.get("as_of"), "source_as_of": p["as_of"], "fetched_at": p["fetched_at"],
                    "run_id": p["run_id"]})
        return [base.upsert_partition(rows, SCHEMA, KEY, partition_path(y)) for y, rows in sorted(years.items())]

    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        stage.create(con, "stg_sec_cover", SCHEMA,
                     [f for _, f in stage.partitions(base.parquet_root() / "sec" / "cover_shares", "year")])
        return {}

    def check(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict:
        cols = ["security_id", "measure", "as_of", "value", "source"]
        added, removed = stage.compare_live(con, "shares_class", cols, f"source = '{NAME}'")
        tier_a = {str(r["cik"]) for r in spine_rows() if r.get("tier") == "A" and r.get("cik")}
        have = {r[0] for r in con.execute("SELECT DISTINCT cik FROM stg_sec_cover").fetchall()}
        as_of = ctx.as_of or self.default_as_of()
        return {"dataset": NAME, "result": "changed" if added or removed else "unchanged",
                "source_as_of": as_of.isoformat(), "rows": stage.count(con, "stg_sec_cover"), "added": added,
                "removed": removed, "coverage": round(len(tier_a & have) / len(tier_a), 4) if tier_a else None}


DATASET = SecCoverShares()
