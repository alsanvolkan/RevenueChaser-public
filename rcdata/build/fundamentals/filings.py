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

Requests go through the shared SEC bucket and are counted under ``sec``. Backfill: every Tier A company with no
filing list yet, and its two documents, resumable.

Raw: ``raw/sec/filings/submissions/<cik>/<as-of>.json.gz`` (with a ``.meta.json``: the accessions it holds) and
``raw/sec/filings/documents/<cik>/<accession>.html.gz``. Parquet: ``parquet/sec/filings/cik=<cik>/filings.parquet``
(one row per filing and 8-K item), ``.../documents.parquet`` (the texts), and ``.../segments.parquet`` /
``.../customers.parquet`` (the annual report's inline XBRL, ``ixbrl.py``: revenue by segment, product and country,
and major customers' shares), each replaced whole from the newest list.

Staging: ``stg_sec_filings``, ``stg_sec_documents``, ``stg_sec_segments``, ``stg_sec_customers``.
"""
from __future__ import annotations

import re
import time
from datetime import date, timedelta
from pathlib import Path

import duckdb

from ....core import calendar
from .. import base
from . import KIND, common, ixbrl, sec_index

NAME = "sec/filings"
NEW_PER_RUN = 9000
ANNUAL_FORMS = ("10-K", "20-F", "40-F")             # newest by filing date; a 10-K first on one day
PROXY_FORMS = ("DEF 14A",)
CAPPED = "capped"
#: How many AIF exhibits of one 40-F are kept. shortcut: one filing has never been seen with more than one, so the
#: largest is taken; upgrade the number if a filer splits its Annual Information Form across exhibits.
_AIF_PER_FILING = 1
#: The one item still cut at the cap: ``business`` is Value Chain's own field and its slicer caps it
#: (``module_value_chain/splitter.py``), which is that board's rule and not changed here. Everything else this
#: layer stores is kept entire (avo, 5 Nov 2026: "belgeyi hic kesmeyelim, hepsi kalsin") -- a slice with no stop
#: header is labelled ``overrun`` rather than truncated, so the reader is told instead of the text being cut.
SLICED_ITEMS = ("business",)
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


def _exhibit_path(cik: str, accession: str, name: str) -> Path:
    return common.raw_dir(NAME) / "documents" / str(cik) / f"{accession}.{name}.gz"


def index_url(cik: str, accession: str) -> str:
    """The filing's own file listing. Only a 40-F needs it: every other form this layer reads keeps what it wants
    in the primary document."""
    return f"{common.sec_www()}/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/index.json"


#: An exhibit of a 40-F whose name says it is the Annual Information Form -- the Canadian filing that carries the
#: business description when the 40-F itself does not. Measured on two real filings (5 Nov 2026): Brookfield files
#: it as ``a2025-40xfex991aif.htm`` while Canadian Natural writes everything into the primary document, so both
#: shapes exist and the primary document is fetched either way.
#: shortcut: the name is the only signal used; the index gives no reliable exhibit *type* for these. Upgrade by
#: reading the filing's ``<CIK>-index.htm`` description column if a filer is found whose name does not say ``aif``.
_AIF_NAME = re.compile(r"(?:aif|ex[-_]?99)", re.I)


def stored_exhibits(cik: str, accession: str) -> list[Path]:
    """The AIF exhibit files already fetched for one filing, largest name first; the index sidecar is not one."""
    folder = _doc_path(cik, accession).parent
    if not folder.is_dir():
        return []
    return sorted(pth for pth in folder.glob(f"{accession}.*.gz")
                  if _AIF_NAME.search(pth.name) and not pth.name.endswith(".meta.json.gz"))


def aif_exhibits(listing: dict) -> list[str]:
    """The names of a 40-F's AIF exhibits in its ``index.json``, largest first; [] when it has none."""
    items = ((listing or {}).get("directory") or {}).get("item") or []
    found = [(int(i.get("size") or 0), str(i.get("name") or "")) for i in items
             if str(i.get("name") or "").lower().endswith((".htm", ".html")) and _AIF_NAME.search(str(i.get("name")))]
    return [name for _, name in sorted(found, reverse=True)]


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


_DASH = r"\-\u2010-\u2015"

#: The sections of an annual report served beside the business description, each its own ``filing_item`` row
#: (avo, 5 Nov 2026). Item 5 carries the dividend history and the monthly share-repurchase table, and Item 8 the
#: statements **and their notes** -- where segment and major-customer concentration are written out, next to the
#: inline XBRL figures ``ixbrl.py`` already reads.
#: shortcut: 10-K only. A 20-F numbers its items differently (Item 5 is the operating and financial review and the
#: statements are Item 18), so patterns for it are a guess until they are checked against real 20-F documents;
#: upgrade by adding a ``20-F`` key here once that check is done.
SECTIONS: dict[str, dict[str, tuple[str, str]]] = {
    "market": {"10-K": (rf"\bitem\s*5\s*[.\:{_DASH}\s]\s*market\s+for\s+(?:the\s+)?registrant",
                        rf"\bitem\s*6\s*[.\:{_DASH}\s]|\bitem\s*7\s*[.\:{_DASH}\s]\s*management")},
    "financials": {"10-K": (rf"\bitem\s*8\s*[.\:{_DASH}\s]\s*(?:consolidated\s+)?financial\s+statements",
                            rf"\bitem\s*9\s*[.\:{_DASH}\s]")},
}


def _longest_section(text: str, start: str, stop: str) -> tuple[str, str]:
    """``(text, quality)`` of the longest match of ``start``: the body, not the table of contents.

    The same rule the business slicer uses (``module_value_chain/splitter.slice_item``): a report's contents page
    lists every item header, so the first match is a few dozen characters and only the longest is the section.
    ``overrun`` when no stop header follows -- the slice ran on, and is kept but never called ``ok``.

    shortcut: the longest match is the body only while the contents page lists this section's **stop** header too,
    which stops the contents match early. Every 10-K sampled on 5 Nov 2026 had a full contents page; a filer whose
    contents skips the stop item would have its slice start at the contents. Upgrade by preferring a match whose
    slice holds no other section's start header, if such a filer is found.
    """
    start_re, stop_re = re.compile(start, re.I), re.compile(stop, re.I)
    best, stopped = "", False
    for m in start_re.finditer(text):
        end_m = stop_re.search(text, m.end())
        chunk = text[m.start():end_m.start() if end_m else len(text)]
        if len(chunk) > len(best):
            best, stopped = chunk, end_m is not None
    if not best:
        return "", "missing"
    if not stopped:
        # No stop header: the slice runs to the end of the document. Kept whole and never called ``ok``, so a
        # reader knows it is more than the section -- the text is not cut to say so.
        return best, "overrun"
    return best, "ok" if len(best) >= min_text_chars() else "short"


def document_sections(html: str, form: str) -> list[tuple[str, str, str]]:
    """``(item, text, quality)`` per section this layer serves from one document.

    Nothing here is cut. ``whole`` is the document's text **uncut** (avo, 5 Nov 2026: "belgeyi hic kesmeyelim, hepsi kalsin"): a 10-K
    runs from 220,000 to 1.4 million characters and the business section is a tenth of it, so everything a later
    reader might want -- the risk factors, the legal proceedings, the notes -- was being thrown away at the one
    place it could be kept for free. Its quality is the length's only; the cap belongs to a slice, which can run
    away when a stop header is missed, and not to a whole document, whose length is the filer's.

    Then the slices: ``business`` as before (``splitter``, Value Chain's own rule), and ``market`` and
    ``financials`` from :data:`SECTIONS`. A proxy is one ``proxy`` row, whole.
    """
    from ....module_value_chain import splitter
    cap = max_text_chars()
    text = splitter.strip_html(html)
    if form in PROXY_FORMS:
        return [("proxy", text, "ok" if len(text) >= min_text_chars() else ("short" if text else "missing"))]
    out = [("whole", text, "ok" if len(text) >= min_text_chars() else ("short" if text else "missing"))]
    item, quality = splitter.slice_item(text, form)
    item = item or ""
    out.append(("business", item, CAPPED if len(item) >= cap else quality))
    for name, per_form in SECTIONS.items():
        pair = per_form.get(str(form).upper())
        if pair is None:
            continue
        sliced, sliced_quality = _longest_section(text, pair[0], pair[1])
        if sliced_quality == "missing":
            # No row rather than a row that says nothing: the ``whole`` row beside it already proves the document
            # was read, so a section with no row is a section this filer does not have.
            continue
        out.append((name, sliced, sliced_quality))
    return out


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
        if f is None:
            continue
        path = _doc_path(cik, f["accession"])
        if not path.exists():
            url = document_url(cik, f["accession"], f["primary_document"])
            r = common.sec_get(ctx, url, missing_object=True)  # /Archives/: a missing document answers 403
            if r is None:
                ctx.issue("document_missing", f"{cik}:{f['accession']}",
                          f"the archive holds no {f['primary_document']}")
            else:
                base.write_raw_json(common.meta_path(path), {"cik": cik, "accession": f["accession"],
                                                             "form": f["form"], "as_of": as_of.isoformat(),
                                                             "url": url, "fetched_at": time.time(),
                                                             "run_id": ctx.run_id})
                base.write_raw(path, r.content)
                fetched += 1
        # A 40-F may keep its business description in an Annual Information Form exhibit instead of the primary
        # document (``aif_exhibits``), and the primary document is now read either way.
        if f["form"] == "40-F":
            fetched += read_exhibits(ctx, cik, f, as_of)
    return fetched


def read_exhibits(ctx: base.RunContext, cik: str, f: dict, as_of: date) -> int:
    """Fetch a 40-F's AIF exhibits that are not stored yet; how many were fetched. One index request per filing."""
    first = _exhibit_path(cik, f["accession"], "index.json")
    if first.exists():
        listing = base.read_raw_json(first)
    else:
        r = common.sec_get(ctx, index_url(cik, f["accession"]), missing_object=True)
        if r is None:
            ctx.issue("index_missing", f"{cik}:{f['accession']}", "the archive holds no index.json")
            return 0
        listing = r.json()
        base.write_raw_json(first, listing)
    fetched = 0
    for name in aif_exhibits(listing)[:_AIF_PER_FILING]:
        path = _exhibit_path(cik, f["accession"], name)
        if path.exists():
            continue
        url = document_url(cik, f["accession"], name)
        r = common.sec_get(ctx, url, missing_object=True)
        if r is None:
            ctx.issue("document_missing", f"{cik}:{f['accession']}:{name}", f"the archive holds no {name}")
            continue
        base.write_raw_json(common.meta_path(path), {"cik": cik, "accession": f["accession"], "form": f["form"],
                                                     "exhibit": name, "as_of": as_of.isoformat(), "url": url,
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
        for forms in (ANNUAL_FORMS, PROXY_FORMS):
            f = pick(rows, forms)
            if f is None:
                continue
            annual = forms is ANNUAL_FORMS
            base_row = {k: f.get(k) for k in ("cik", "accession", "form", "filing_date", "report_date",
                                               "primary_document")}
            paths = [_doc_path(cik, f["accession"])]
            if annual and f["form"] == "40-F":
                paths += stored_exhibits(cik, f["accession"])
            for path in paths:
                if not path.exists():
                    continue
                meta = base.read_raw_json(common.meta_path(path))
                html = base.read_raw(path).decode("utf-8", "replace")
                stamps = {"source_as_of": date.fromisoformat(meta["as_of"]),
                          "fetched_at": float(meta["fetched_at"]), "run_id": meta["run_id"]}
                exhibit = meta.get("exhibit")
                for item, text, quality in document_sections(html, f["form"]):
                    # An exhibit is the business description on its own: its whole text is the section, and the
                    # 10-K item slices do not apply to a Canadian Annual Information Form.
                    if exhibit and item != "whole":
                        continue
                    out.append({**base_row, "item": f"aif:{exhibit}" if exhibit else item, "text": text,
                                "chars": len(text), "quality": quality, **stamps})
                if annual and not exhibit:
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
