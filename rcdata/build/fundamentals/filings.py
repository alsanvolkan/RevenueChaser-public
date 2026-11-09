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
from .. import dirty as dirty_set
from . import KIND, common, ixbrl, sec_index

NAME = "sec/filings"
NEW_PER_RUN = 9000
ANNUAL_FORMS = ("10-K", "10-KT", "20-F", "40-F")             # newest by filing date; a 10-K first on one day
PROXY_FORMS = ("DEF 14A",)
CAPPED = "capped"
#: How many AIF exhibits of one 40-F are kept. shortcut: one filing has never been seen with more than one, so the
#: largest is taken; upgrade the number if a filer splits its Annual Information Form across exhibits.
_AIF_PER_FILING = 1
#: The one sliced item: ``business`` is Value Chain's own field and its slicer caps it
#: (``module_value_chain/splitter.py``), which is that board's rule and not changed here. Everything else this
#: layer stores is kept entire (avo, 5 Nov 2026: "belgeyi hic kesmeyelim, hepsi kalsin"): ``whole``, ``proxy``
#: and an ``aif`` exhibit carry the filer's own length.
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
#: An ``ex99`` alone is not enough. Brookfield's 40-F (read 5 Nov 2026) files 99.1 as the Annual Information
#: Form, 99.2 as the financial statements and 99.3 as the MD&A, and the statements are the largest of the three --
#: so "the biggest ex99" picks the wrong one. A name that says ``aif`` is taken first; only when none does is
#: ``99.1`` taken, by its number.
#: shortcut: three named limits. (1) The name is the only signal, because ``index.json`` carries no reliable
#: exhibit *type*; upgrade by reading the filing's ``<CIK>-index.htm`` description column. (2) The 99.1/99.2/99.3
#: order is **one filer's** layout, not a rule of the form. (3) Only ``.htm`` and ``.html`` are considered, so a
#: filer who files the AIF as a PDF is missed (``read_exhibits`` raises ``aif_not_named`` when it can tell).
_AIF_NAME = re.compile(r"aif", re.I)
_EX_99_1 = re.compile(r"ex(?:hibit)?[-_]?99[._-]?1(?![0-9])", re.I)
_EX_99_ANY = re.compile(r"ex(?:hibit)?[-_]?99", re.I)


def stored_exhibits(cik: str, accession: str, primary: str | None = None) -> list[Path]:
    """The AIF exhibit files fetched for one filing, in the order :func:`aif_exhibits` chose them.

    Read from the stored ``index.json`` rather than guessed from file names: the fallback branch picks a name with
    no ``aif`` in it (``ex99-1.htm``), which a name filter cannot find again -- so the exhibit was fetched, stored,
    and never typed.
    """
    index = _exhibit_path(cik, accession, "index.json")
    if not index.exists():
        return []
    names = aif_exhibits(base.read_raw_json(index), primary)[:_AIF_PER_FILING]
    return [pth for pth in (_exhibit_path(cik, accession, name) for name in names) if pth.exists()]


def aif_exhibits(listing: dict, primary: str | None = None) -> list[str]:
    """The names of a 40-F's Annual Information Form exhibits in its ``index.json``, largest first; [] when none.

    A name that says ``aif`` wins outright. Only when no name does is ``99.1`` taken, so the statements exhibit
    (99.2) and the MD&A (99.3) are never mistaken for the business description. ``primary`` is left out: the
    index lists the 40-F itself, which is already read as the document, and a primary named like an AIF would
    otherwise be fetched and typed twice.
    """
    items = ((listing or {}).get("directory") or {}).get("item") or []
    pages = [(int(i.get("size") or 0), str(i.get("name") or "")) for i in items
             if str(i.get("name") or "").lower().endswith((".htm", ".html")) and str(i.get("name")) != primary]
    return [name for _, name in sorted(_chosen(pages), reverse=True)]


def _chosen(pages: list[tuple[int, str]]) -> list[tuple[int, str]]:
    named = [(size, name) for size, name in pages if _AIF_NAME.search(name)]
    return named or [(size, name) for size, name in pages if _EX_99_1.search(name)]


def picked_by_name(listing: dict, name: str, primary: str | None = None) -> bool:
    """True when ``name`` was chosen because it says ``aif``, False when only its number chose it.

    The number is one filer's layout and not a rule of the form, so a by-number pick is a guess: a filer who
    keeps the Annual Information Form in the primary document and files its statements as 99.1 would get an
    ``aif`` row that is the statements. Carried onto the row's ``quality`` so the guesses can be counted.
    """
    return bool(_AIF_NAME.search(name))


def missed_exhibits(listing: dict, primary: str | None = None) -> int:
    """How many ``ex99`` pages the filing has when :func:`aif_exhibits` chose none -- the one case this can tell
    apart from "the AIF is in the primary document", and worth an issue rather than silence."""
    if aif_exhibits(listing, primary):
        return 0
    items = ((listing or {}).get("directory") or {}).get("item") or []
    return sum(1 for i in items if _EX_99_ANY.search(str(i.get("name") or "")))


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


def document_sections(html: str, form: str) -> list[tuple[str, str, str]]:
    """``(item, text, quality)`` per section this layer serves from one document.

    Nothing here is cut. ``whole`` is the document's text **uncut** (avo, 5 Nov 2026: "belgeyi hic kesmeyelim, hepsi kalsin"): a 10-K
    runs from 220,000 to 1.4 million characters and the business section is a tenth of it, so everything a later
    reader might want -- the risk factors, the legal proceedings, the notes -- was being thrown away at the one
    place it could be kept for free. Its quality is the length's only; the cap belongs to a slice, which can run
    away when a stop header is missed, and not to a whole document, whose length is the filer's.

    Then ``business`` as before (``splitter``, Value Chain's own rule). A proxy is one ``proxy`` row, whole.

    Item 5 and Item 8, which avo also asked for, are not here: a review of the first attempt found that the
    longest-match rule picks a **cross-reference** in the item before ("included in Item 8 -- Financial
    Statements") over the real header, because both resolve to the same stop, and that a 10-K often writes one
    sentence under Item 8 and puts the statements after Item 15. Both need the 8,891 documents on disk measured
    before a rule is chosen, so they come in their own change.
    """
    from ....module_value_chain import splitter
    cap = max_text_chars()
    text = splitter.strip_html(html)
    if form in PROXY_FORMS:
        return [("proxy", text, "ok" if len(text) >= min_text_chars() else ("short" if text else "missing"))]
    out = [("whole", text, "ok" if len(text) >= min_text_chars() else ("short" if text else "missing"))]
    if str(form).upper() == "40-F":
        # Value Chain's splitter knows a 10-K's Item 1 and a 20-F's Item 4; a Canadian 40-F has neither, so a
        # slice of it would be an arbitrary cut labelled by rules that do not apply. The whole text is the row.
        return out
    item, quality = splitter.slice_item(text, form)
    item = item or ""
    out.append(("business", item, CAPPED if len(item) >= cap else quality))
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
    missed = missed_exhibits(listing, f.get("primary_document"))
    if missed:
        ctx.issue("aif_not_named", f"{cik}:{f['accession']}",
                  f"{missed} ex99 page(s) and none named an Annual Information Form")
    fetched = 0
    for name in aif_exhibits(listing, f.get("primary_document"))[:_AIF_PER_FILING]:
        path = _exhibit_path(cik, f["accession"], name)
        if path.exists():
            continue
        url = document_url(cik, f["accession"], name)
        r = common.sec_get(ctx, url, missing_object=True)
        if r is None:
            ctx.issue("document_missing", f"{cik}:{f['accession']}:{name}", f"the archive holds no {name}")
            continue
        base.write_raw_json(common.meta_path(path), {"cik": cik, "accession": f["accession"], "form": f["form"],
                                                     "exhibit": name, "by_name": picked_by_name(listing, name),
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
            docs += read_documents(ctx, cik, raw, end)
            # Every company this holds a list for, not only the ones fetched from this run: the rows are typed
            # from raw, so a company whose documents were already on disk still has to be rewritten to gain a
            # row shape it did not have before (the ``whole`` row, 5 Nov 2026).
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
                paths += stored_exhibits(cik, f["accession"], f.get("primary_document"))
            for path in paths:
                if not path.exists():
                    continue
                meta = base.read_raw_json(common.meta_path(path))
                html = base.read_raw(path).decode("utf-8", "replace")
                stamps = {"source_as_of": date.fromisoformat(meta["as_of"]),
                          "fetched_at": float(meta["fetched_at"]), "run_id": meta["run_id"]}
                exhibit = meta.get("exhibit")
                for item, text, quality in document_sections(html, f["form"]):
                    if exhibit and meta.get("by_name") is False:
                        # Chosen by its number alone, which is one filer's layout: say so on the row rather than
                        # letting a guess read like Brookfield's real Annual Information Form.
                        quality = "by_number"
                    # An exhibit is the business description on its own: its whole text is the section, and the
                    # 10-K item slices do not apply to a Canadian Annual Information Form.
                    if exhibit and item != "whole":
                        continue
                    out.append({**base_row, "item": "aif" if exhibit else item, "text": text,
                                "chars": len(text), "quality": quality, **stamps})
                if annual and not exhibit:
                    filed = {k: base_row[k] for k in ("cik", "accession", "form", "filing_date")}
                    found_segments, found_customers = ixbrl.parse(html)
                    segments += [{**filed, **r, "segment": r["label"], **stamps} for r in found_segments]
                    customers += [{**filed, **r, **stamps} for r in found_customers]
        return out, segments, customers

    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        folder = common.parquet_dir(NAME)
        ciks = common.dirty_ciks(ctx, con)
        common.stage_glob(con, STAGING["filings"], FILING, folder, "cik=*/filings.parquet", ciks=ciks)
        common.stage_glob(con, STAGING["documents"], DOCUMENT, folder, "cik=*/documents.parquet", ciks=ciks)
        common.stage_glob(con, STAGING["segments"], SEGMENT, folder, "cik=*/segments.parquet", ciks=ciks)
        common.stage_glob(con, STAGING["customers"], CUSTOMER, folder, "cik=*/customers.parquet", ciks=ciks)
        return {}

    dirty_keyed = True

    def dirty(self, ctx: base.RunContext):
        """The companies whose ``cik=<cik>`` partitions changed since the live file was built (#446)."""
        return dirty_set.keyed(ctx, self)

    def check(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict:
        cols = ["cik", "accession", "item", "quality"]
        added, removed = common.compare_live(con, f"SELECT {', '.join(cols)} FROM filing_item", "filing_item", cols,
                                             dirty_only=common.dirty_ciks(ctx, con) is not None)
        # From the served table, not the staging: on a build over the live copy the staging holds only the dirty
        # companies, and every filing-list row is a ``kind = 'item'`` row of ``filing_item`` either way.
        as_of = con.execute("SELECT max(source_as_of) FROM filing_item WHERE kind = 'item'").fetchone()[0]
        listed = con.execute("SELECT count(DISTINCT cik) FROM filing_item WHERE kind = 'item'").fetchone()[0]
        tier_a = con.execute("SELECT count(DISTINCT cik) FROM fundamental_company").fetchone()[0]
        recent = con.execute(f"""SELECT count(DISTINCT c.cik) FROM fundamental_company c
                                 WHERE c.submissions_as_of >= DATE '{(ctx.as_of or self.default_as_of())
                                 - timedelta(days=sec_index.LOOKBACK_DAYS)}'""").fetchone()[0]
        qualities = dict(con.execute("SELECT quality, count(*) FROM filing_item WHERE kind = 'document' "
                                     "GROUP BY 1").fetchall())
        ctx.notes.append(f"{listed} of {tier_a} Tier A companies have a filing list ({recent} read in the last "
                         f"{sec_index.LOOKBACK_DAYS} days); documents by quality: "
                         + (", ".join(f"{k} {v}" for k, v in sorted(qualities.items())) or "none"))
        return {"dataset": NAME, "result": "changed" if added or removed else "unchanged",
                "source_as_of": (as_of or ctx.as_of or self.default_as_of()).isoformat(),
                "rows": common.count(con, "filing_item", "kind = 'item'"), "added": added, "removed": removed,
                "coverage": round(listed / tier_a, 4) if tier_a else None}


DATASET = Filings()
