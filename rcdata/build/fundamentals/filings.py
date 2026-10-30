"""``sec/filings``: each Tier A company's filing list (8-K items) and its newest annual report and proxy text.

**The filing list.** ``GET <RC_SEC_DATA_BASE>/submissions/CIK<10 digits>.json`` (``filings.recent``: accession, form,
filing and report dates, primary document, 8-K items), re-read when the SEC daily index (``sec_index.py``) shows the
company filed anything the stored list does not hold (the index read back to the day after the last whole run when
that is longer ago than a week, ``sec_index.lookback``), and for a Tier A company never read, up to ``NEW_PER_RUN`` a
run. The day each list was read is served (``fundamental_company.submissions_as_of``), so "no 8-K event" can be told
from "not read lately" (the Fundamentals review's F2).

**The documents.** The primary document of the newest annual report (10-K, else 20-F; a 40-F is recorded as
``form_40f``: its business text is in an exhibit this layer does not read) and of the newest proxy (DEF 14A),
fetched once each (the raw file is the resume marker). The annual report's business section is sliced by Value
Chain's splitter (``module_value_chain/splitter.py``: Item 1 of a 10-K, Item 4 of a 20-F); the proxy is kept whole.
Both are capped at ``MAX_TEXT_CHARS`` (the splitter's 200,000). **Quality is never ``ok`` at the cap:** a text that
reaches it is ``capped`` (the splitter itself labels a stopped slice it cut at the cap ``ok``; that is its own
board's concern and not changed here), so a reader knows the text is not whole. Otherwise the splitter's ``ok`` /
``short`` / ``overrun`` / ``missing``, and for a proxy ``ok`` or ``short`` (under the splitter's 2,000 characters).

Requests are spaced under 5 a second and counted against the ``sec`` cap. Backfill: every Tier A company with no
filing list yet, and its two documents, resumable.

Raw: ``raw/sec/filings/submissions/<cik>/<as-of>.json.gz`` (with a ``.meta.json``: the accessions it holds) and
``raw/sec/filings/documents/<cik>/<accession>.html.gz``. Parquet: ``parquet/sec/filings/cik=<cik>/filings.parquet``
(one row per filing and 8-K item), ``.../documents.parquet`` (the texts), and ``.../segments.parquet`` /
``.../customers.parquet`` (the annual report's inline XBRL, ``ixbrl.py``: revenue by segment, product and country,
and major customers' shares), each replaced whole from the newest list.

Staging: ``stg_sec_filings``, ``stg_sec_documents``, ``stg_sec_segments``, ``stg_sec_customers``.
"""
from __future__ import annotations

import time
from datetime import date, timedelta
from pathlib import Path

import duckdb

from ....core import calendar
from .. import base
from . import KIND, common, ixbrl, sec_index

NAME = "sec/filings"
NEW_PER_RUN = 300
ANNUAL_FORMS = ("10-K", "20-F", "40-F")             # newest by filing date; a 10-K first on one day
PROXY_FORMS = ("DEF 14A",)
CAPPED = "capped"
STAGING = {"filings": "stg_sec_filings", "documents": "stg_sec_documents", "segments": "stg_sec_segments",
           "customers": "stg_sec_customers"}

FILING = [("cik", "TEXT"), ("accession", "TEXT"), ("form", "TEXT"), ("filing_date", "DATE"), ("report_date", "DATE"),
          ("primary_document", "TEXT"), ("item", "TEXT"), ("source_as_of", "DATE"), ("fetched_at", "DOUBLE"),
          ("run_id", "TEXT")]
DOCUMENT = [("cik", "TEXT"), ("accession", "TEXT"), ("form", "TEXT"), ("filing_date", "DATE"),
            ("report_date", "DATE"), ("primary_document", "TEXT"), ("item", "TEXT"), ("text", "TEXT"),
            ("chars", "INTEGER"), ("quality", "TEXT"), ("source_as_of", "DATE"), ("fetched_at", "DOUBLE"),
            ("run_id", "TEXT")]
# The annual report's inline XBRL (``ixbrl.py``): revenue by segment, product and country, and major customers.
_FILED = [("cik", "TEXT"), ("accession", "TEXT"), ("form", "TEXT"), ("filing_date", "DATE")]
_STAMPS = [("source_as_of", "DATE"), ("fetched_at", "DOUBLE"), ("run_id", "TEXT")]
SEGMENT = _FILED + [("axis", "TEXT"), ("member", "TEXT"), ("segment", "TEXT"), ("concept", "TEXT"),
                    ("value", "DOUBLE"), ("unit", "TEXT"), ("period_start", "DATE"), ("period_end", "DATE")] + _STAMPS
CUSTOMER = _FILED + [("customer", "TEXT"), ("member", "TEXT"), ("benchmark", "TEXT"), ("share", "DOUBLE"),
                     ("period_start", "DATE"), ("period_end", "DATE")] + _STAMPS


def max_text_chars() -> int:
    from ....module_value_chain import config
    return int(config.MAX_ITEM_CHARS)


def min_text_chars() -> int:
    from ....module_value_chain import config
    return int(config.MIN_ITEM_CHARS)


def submissions_url(cik: str) -> str:
    return f"{common.sec_data()}/submissions/CIK{common.cik10(cik)}.json"


def document_url(cik: str, accession: str, primary: str) -> str:
    return f"{common.sec_www()}/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{primary}"


def _subs_dir(cik: str) -> Path:
    return common.raw_dir(NAME) / "submissions" / str(cik)


def _doc_path(cik: str, accession: str) -> Path:
    return common.raw_dir(NAME) / "documents" / str(cik) / f"{accession}.html.gz"


def _subs_raws(cik: str) -> list[Path]:
    folder = _subs_dir(cik)
    return sorted(folder.glob("*.json.gz")) if folder.is_dir() else []


def _newest_list(cik: str) -> date | None:
    raws = _subs_raws(cik)
    return date.fromisoformat(raws[-1].name.split(".", 1)[0]) if raws else None


def _held(cik: str) -> set[str]:
    raws = _subs_raws(cik)
    return set(base.read_raw_json(common.meta_path(raws[-1])).get("accessions") or []) if raws else set()


def parse(cik: str, body: dict) -> list[dict]:
    """The filings of one submissions answer (``filings.recent``): ``{cik, accession, form, filing_date,
    report_date, primary_document, items}``; ``SourceError`` for a body that is not the documented shape."""
    from ....core.sec_submissions import rows_from_payload
    if not isinstance(body, dict) or not isinstance((body.get("filings") or {}).get("recent"), dict):
        raise base.SourceError(f"sec submissions {cik}: not the documented shape")
    _, rows = rows_from_payload(cik, body)
    recent = body["filings"]["recent"]
    reports = dict(zip(recent.get("accessionNumber") or [], recent.get("reportDate") or []))
    for r in rows:
        r["report_date"] = reports.get(r["accession"]) or None
    return rows


def pick(rows: list[dict], forms: tuple[str, ...]) -> dict | None:
    """The newest filing of ``forms`` by filing date (on one day the earlier form in ``forms``); an amendment never
    counts (as Value Chain's ``pick_filing``)."""
    best, best_rank = None, None
    for r in rows:
        if r["form"] not in forms or not r.get("primary_document"):
            continue
        rank = (str(r["filing_date"]), -forms.index(r["form"]))
        if best is None or rank > best_rank:
            best, best_rank = r, rank
    return best


def slice_text(html: str, form: str) -> tuple[str, str]:
    """``(text, quality)`` of one document: the business section of an annual report, a proxy whole. Never ``ok``
    at the cap (``capped``)."""
    from ....module_value_chain import splitter
    cap = max_text_chars()
    text = splitter.strip_html(html)
    if form in PROXY_FORMS:
        out = text[:cap]
        quality = CAPPED if len(text) >= cap else ("ok" if len(out) >= min_text_chars() else
                                                   ("short" if out else "missing"))
        return out, quality
    item, quality = splitter.slice_item(text, form)
    item = item or ""
    if len(item) >= cap:
        quality = CAPPED
    return item, quality


def read_list(ctx: base.RunContext, cik: str, as_of: date) -> Path | None:
    raw = _subs_dir(cik) / f"{as_of.isoformat()}.json.gz"
    if raw.exists():
        return raw
    r = common.sec_get(ctx, submissions_url(cik), missing=(404,))
    if r is None:
        ctx.issue("no_submissions", cik, "the SEC answered 404 for the filing list")
        return None
    try:
        body = r.json()
    except ValueError as e:
        raise base.SourceError(f"sec submissions {cik}: the answer is not JSON") from e
    rows = parse(cik, body)
    base.write_raw_json(common.meta_path(raw), {"cik": cik, "as_of": as_of.isoformat(), "fetched_at": time.time(),
                                                "run_id": ctx.run_id, "url": submissions_url(cik),
                                                "accessions": sorted(r["accession"] for r in rows)})
    return base.write_raw_json(raw, body)


def read_documents(ctx: base.RunContext, cik: str, raw: Path, as_of: date) -> int:
    """Fetch the newest annual report and proxy of the list in ``raw`` that are not stored yet; how many fetched."""
    rows = parse(cik, base.read_raw_json(raw))
    fetched = 0
    for forms in (ANNUAL_FORMS, PROXY_FORMS):
        f = pick(rows, forms)
        if f is None or f["form"] == "40-F":
            continue
        path = _doc_path(cik, f["accession"])
        if path.exists():
            continue
        url = document_url(cik, f["accession"], f["primary_document"])
        r = common.sec_get(ctx, url, missing_object=True)      # /Archives/: a missing document answers 403
        if r is None:
            ctx.issue("document_missing", f"{cik}:{f['accession']}",
                      f"the archive holds no {f['primary_document']}")
            continue
        base.write_raw_json(common.meta_path(path), {"cik": cik, "accession": f["accession"], "form": f["form"],
                                                     "as_of": as_of.isoformat(), "url": url,
                                                     "fetched_at": time.time(), "run_id": ctx.run_id})
        base.write_raw(path, r.content)
        fetched += 1
    return fetched


class Filings:
    name = NAME
    kind = KIND
    providers = (common.SEC,)
    tables: tuple[str, ...] = ()
    cadence = "daily"
    backfill_needs_from = False
    starts_by_hand = True

    def default_as_of(self) -> date:
        return calendar.today_ny()

    def fetch(self, ctx: base.RunContext, as_of: date) -> list[Path]:
        companies = common.ciks(common.universe())
        if not companies:
            ctx.notes.append("no Tier A companies yet (no spine file)")
            return []
        filed: dict[str, set[str]] = {}
        days = sec_index.lookback(ctx, NAME, as_of, lambda: min(filter(None, map(_newest_list, companies)),
                                                                default=None))
        for f in sec_index.filings(ctx, as_of, ciks=set(companies), lookback=days):
            filed.setdefault(f["cik"], set()).add(f["accession"])
        due = sorted((c for c, accs in filed.items() if accs - _held(c)), key=int)
        new = [c for c in companies if c not in due and not _subs_raws(c)]
        out, docs = [], 0
        for cik in due + new[:NEW_PER_RUN]:
            raw = read_list(ctx, cik, as_of)
            if raw is not None:
                out.append(raw)
                docs += read_documents(ctx, cik, raw, as_of)
        ctx.notes.append(f"{len(due)} filing lists re-read, {min(len(new), NEW_PER_RUN)} new, {docs} documents"
                         + (f"; {len(new) - NEW_PER_RUN} new left for the next run" if len(new) > NEW_PER_RUN else ""))
        sec_index.mark_scanned(ctx, NAME, as_of)
        return out

    def backfill(self, ctx: base.RunContext, start: date | None, end: date) -> None:
        out, docs = [], 0
        for cik in common.ciks(common.universe()):
            raws = _subs_raws(cik)
            raw = raws[-1] if raws else read_list(ctx, cik, end)
            if raw is None:
                continue
            n = read_documents(ctx, cik, raw, end)
            docs += n
            if not raws or n:
                out.append(raw)
        ctx.notes.append(f"backfill: {len(out)} filing lists, {docs} documents")
        self.to_parquet_all(ctx, out)

    def to_parquet(self, ctx: base.RunContext, raw: Path) -> list[Path]:
        return self.to_parquet_all(ctx, [raw])

    def to_parquet_all(self, ctx: base.RunContext, raws: list[Path]) -> list[Path]:
        newest: dict[str, Path] = {}
        for raw in raws:
            cik = raw.parent.name
            if cik not in newest or raw.name > newest[cik].name:
                newest[cik] = raw
        out = []
        for cik, raw in sorted(newest.items()):
            meta = base.read_raw_json(common.meta_path(raw))
            stamps = {"source_as_of": date.fromisoformat(meta["as_of"]), "fetched_at": float(meta["fetched_at"]),
                      "run_id": meta["run_id"]}
            rows = parse(cik, base.read_raw_json(raw))
            filing_rows = []
            for r in rows:
                items = [i.strip() for i in str(r.get("items") or "").split(",") if i.strip()] or [None]
                filing_rows += [{**r, "item": i, **stamps} for i in items]
            doc_rows, segment_rows, customer_rows = self._documents(cik, rows)
            folder = common.parquet_dir(NAME) / f"cik={cik}"
            con = duckdb.connect()
            try:
                base.rows_table(con, "filings", FILING, filing_rows)
                out.append(base.write_parquet(con, "SELECT * FROM filings ORDER BY filing_date, accession, item",
                                              folder / "filings.parquet"))
                base.rows_table(con, "documents", DOCUMENT, doc_rows)
                out.append(base.write_parquet(con, "SELECT * FROM documents ORDER BY accession",
                                              folder / "documents.parquet"))
                base.rows_table(con, "segments", SEGMENT, segment_rows)
                out.append(base.write_parquet(con, "SELECT * FROM segments ORDER BY axis, member",
                                              folder / "segments.parquet"))
                base.rows_table(con, "customers", CUSTOMER, customer_rows)
                out.append(base.write_parquet(con, "SELECT * FROM customers ORDER BY customer, benchmark",
                                              folder / "customers.parquet"))
            finally:
                con.close()
        return out

    @staticmethod
    def _documents(cik: str, rows: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
        """The document rows, and the annual report's segment and customer rows (its inline XBRL)."""
        out, segments, customers = [], [], []
        for forms, item in ((ANNUAL_FORMS, "business"), (PROXY_FORMS, "proxy")):
            f = pick(rows, forms)
            if f is None:
                continue
            base_row = {k: f.get(k) for k in ("cik", "accession", "form", "filing_date", "report_date",
                                               "primary_document")}
            if f["form"] == "40-F":
                out.append({**base_row, "item": item, "text": None, "chars": 0, "quality": "form_40f"})
                continue
            path = _doc_path(cik, f["accession"])
            if not path.exists():
                continue
            meta = base.read_raw_json(common.meta_path(path))
            html = base.read_raw(path).decode("utf-8", "replace")
            text, quality = slice_text(html, f["form"])
            stamps = {"source_as_of": date.fromisoformat(meta["as_of"]), "fetched_at": float(meta["fetched_at"]),
                      "run_id": meta["run_id"]}
            out.append({**base_row, "item": item, "text": text, "chars": len(text), "quality": quality, **stamps})
            if item == "business":
                filed = {k: base_row[k] for k in ("cik", "accession", "form", "filing_date")}
                found_segments, found_customers = ixbrl.parse(html)
                segments += [{**filed, **r, "segment": r["label"], **stamps} for r in found_segments]
                customers += [{**filed, **r, **stamps} for r in found_customers]
        return out, segments, customers

    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        folder = common.parquet_dir(NAME)
        common.stage_glob(con, STAGING["filings"], FILING, folder, "cik=*/filings.parquet")
        common.stage_glob(con, STAGING["documents"], DOCUMENT, folder, "cik=*/documents.parquet")
        common.stage_glob(con, STAGING["segments"], SEGMENT, folder, "cik=*/segments.parquet")
        common.stage_glob(con, STAGING["customers"], CUSTOMER, folder, "cik=*/customers.parquet")
        return {}

    def check(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict:
        cols = ["cik", "accession", "item", "quality"]
        added, removed = common.compare_live(con, f"SELECT {', '.join(cols)} FROM filing_item", "filing_item", cols)
        as_of = con.execute(f"SELECT max(source_as_of) FROM {STAGING['filings']}").fetchone()[0]
        listed = con.execute(f"SELECT count(DISTINCT cik) FROM {STAGING['filings']}").fetchone()[0]
        tier_a = con.execute("SELECT count(DISTINCT cik) FROM fundamental_company").fetchone()[0]
        recent = con.execute(f"""SELECT count(DISTINCT c.cik) FROM fundamental_company c
                                 WHERE c.submissions_as_of >= DATE '{(ctx.as_of or self.default_as_of())
                                 - timedelta(days=sec_index.LOOKBACK_DAYS)}'""").fetchone()[0]
        qualities = dict(con.execute(f"SELECT quality, count(*) FROM {STAGING['documents']} GROUP BY 1").fetchall())
        ctx.notes.append(f"{listed} of {tier_a} Tier A companies have a filing list ({recent} read in the last "
                         f"{sec_index.LOOKBACK_DAYS} days); documents by quality: "
                         + (", ".join(f"{k} {v}" for k, v in sorted(qualities.items())) or "none"))
        return {"dataset": NAME, "result": "changed" if added or removed else "unchanged",
                "source_as_of": (as_of or ctx.as_of or self.default_as_of()).isoformat(),
                "rows": common.count(con, STAGING["filings"]), "added": added, "removed": removed,
                "coverage": round(listed / tier_a, 4) if tier_a else None}


DATASET = Filings()
