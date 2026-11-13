"""``sec/forms``: the sections and note blocks of the stored filings, the one dataset of the ``sec_forms`` kind.

No request leaves the box. ``sec/filings`` has already fetched every document this reads -- the newest annual
report of each company, its Annual Information Form exhibits, and the newest proxy -- so this dataset's ``fetch``
is the **cut**, and what it returns is the stale companies' documents for ``to_parquet_all`` to cut.

**Why one dataset and not two.** ``dirty.dataset_dir`` splits a name on the first ``/`` only and the build manifest
scans exactly those folders (``rcdata/build/dirty.py``), so two datasets writing into one ``parquet/sec/forms/``
would leave both manifests empty, every later build would read "nothing changed", and the served tables would
freeze after the first build with every test green. One dataset, one folder, two Parquet files -- as ``sec/filings``
holds five.

**When a company is re-cut.** ``dirty.py`` reloads Parquet into DuckDB; it does not cut. So the staleness rule is
``fetch``'s own (:func:`_stale`): a company is stale when the ``(accession, document)`` pairs on disk differ from
the ones its ``sections.parquet`` holds, or when any of its rows was written by another cutter version. That covers
the three cases nothing else triggers -- our own rule version changing, a new proxy or 40-F exhibit appearing, and
a document re-fetched under a new accession -- and a filing that legitimately yields **no** note rows (no inline
XBRL: 0.6% of the stored annual reports) does not read as stale on every run, which comparing expected rows
against stored rows would have done.

**Which cutter version.** :data:`SECTIONS_VERSION` is what this layer stores with today, and every row carries the
version that wrote it (``rules_version``). ``ours-2`` (#755, #756) lifts the 200,000-character cap and fixes the
five item defects; when it lands, :data:`SECTIONS_VERSION` moves to it and the rule above re-cuts every company,
one build, no re-fetch. Until then the sections are what ``ours-1`` gives, cap included -- said plainly here
because the plan's §3 describes the uncut text ``ours-2`` will store.

**Which cutter.** :func:`sections_of` is the one seam, and in this change it calls ``rcdata/sec_sections.py``
only. ``cutter.py`` beside this module (#754) is the edgartools item cutter in its own venv, the owner's decision
A of 12 Nov 2026, and it is **not** wired in here: the items are being reworked in #755 and #756 at the same time,
so putting its ``detection_method`` on the row would have this change and those editing one rule from two sides.
shortcut: items cut by our own rule, so ``method`` is ``header``/``aif``/``uncut`` and never edgartools' own;
upgrade by routing :func:`sections_of` through ``cutter.Cutter`` (its fallback row is the ``header`` cut we
already write) once the item work has landed. Nothing else in this layer changes when that happens, because the
version on every row is what decides who is re-cut.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb

from ... import sec_notes, sec_sections
from .. import base
from .. import dirty as dirty_set
from ..fundamentals import common, filings
from . import KIND

NAME = "sec/forms"
#: The dataset whose stored documents this cuts. Its path rules are its own: the private helpers below are
#: imported from it rather than re-derived, so a change to where it stores a document cannot silently split them.
SOURCE = filings.NAME

SECTION_TABLE = "filing_section"
NOTE_TABLE = "filing_note"
SECTIONS_PARQUET = "sections.parquet"
NOTES_PARQUET = "notes.parquet"

WHOLE, PROXY, BUSINESS = "whole", "proxy", "business"
#: How a section was found, so a weak method can be withdrawn on its own: the document uncut, the line-start item
#: header, or a 40-F's Annual Information Form.
UNCUT, HEADER, AIF = "uncut", "header", "aif"
SECTIONS = (WHOLE, PROXY, BUSINESS)

#: The cutter version this layer stores sections with, and the note reader's own version. Both ride on every row.
SECTIONS_VERSION = sec_sections.OURS_1
NOTES_VERSION = sec_notes.NOTES_VERSION

_STAMPS = [("source_as_of", "DATE"), ("fetched_at", "DOUBLE"), ("run_id", "TEXT")]
#: One row per (filing, document, section). ``document`` is in the key because one accession can hold more than
#: one document this cuts: a 40-F's AIF exhibit is a second one, and its ``business`` row is not the primary
#: document's.
SECTION = [("cik", "TEXT"), ("accession", "TEXT"), ("document", "TEXT"), ("form", "TEXT"), ("filing_date", "DATE"),
           ("report_date", "DATE"), ("section", "TEXT"), ("text", "TEXT"), ("chars", "INTEGER"),
           ("quality", "TEXT"), ("start_char", "INTEGER"), ("end_char", "INTEGER"), ("method", "TEXT"),
           ("rules_version", "TEXT")] + _STAMPS
#: One row per (filing, document, text block). The columns after ``filing_date`` are ``sec_notes.Note.row``'s own.
NOTE = [("cik", "TEXT"), ("accession", "TEXT"), ("document", "TEXT"), ("form", "TEXT"), ("filing_date", "DATE"),
        ("concept", "TEXT"), ("taxonomy", "TEXT"), ("local_name", "TEXT"), ("kind", "TEXT"), ("seq", "INTEGER"),
        ("top_level", "BOOLEAN"), ("parent_seq", "INTEGER"), ("context_ref", "TEXT"), ("period_end", "DATE"),
        ("text", "TEXT"), ("chars", "INTEGER"), ("rules_version", "TEXT")] + _STAMPS
SCHEMAS = {SECTION_TABLE: SECTION, NOTE_TABLE: NOTE}


# ── the documents on disk ───────────────────────────────────────────────────────────────────────────────────────

def targets(cik: str) -> list[tuple[dict, Path, bool]]:
    """``(filing, document, is_exhibit)`` for every stored document of ``cik`` this layer cuts.

    The same choice ``sec/filings`` fetched them with (its ``pick``: the newest annual report and the newest proxy,
    an amendment never counting), read from the newest stored submissions list. The dates come from that list and
    not from a document's ``.meta.json``, which carries only ``cik``, ``accession``, ``form``, ``as_of``, ``url``
    and the run stamps -- no ``filing_date`` and no ``report_date``.
    """
    raws = filings._subs_raws(cik)
    if not raws:
        return []
    rows = filings.parse(cik, base.read_raw_json(raws[-1]))
    out: list[tuple[dict, Path, bool]] = []
    for forms in (filings.ANNUAL_FORMS, filings.PROXY_FORMS):
        f = filings.pick(rows, forms)
        if f is None:
            continue
        primary = filings._doc_path(cik, f["accession"])
        if primary.exists():
            out.append((f, primary, False))
        if f["form"] == "40-F":
            out += [(f, p, True) for p in filings.stored_exhibits(cik, f["accession"], f.get("primary_document"))]
    return out


def _quality(text: str) -> str:
    """A whole document's or an exhibit's quality: its length is the filer's, so only "is there text" is judged."""
    if not text:
        return "missing"
    return "ok" if len(text) >= sec_sections.MIN_ITEM_CHARS else "short"


def _span(whole: str, part: str) -> tuple[int | None, int | None]:
    """Where ``part`` sits in ``whole``, or ``(None, None)``.

    shortcut: found by searching for the cut text, so a cut the rule altered -- a capped one -- has no span;
    upgrade when ``ours-2`` returns the offsets of its own cut.
    """
    i = whole.find(part)
    return (i, i + len(part)) if i >= 0 else (None, None)


def _flag(quality: str, text: str, item: str | None, form: str, *, aif: bool = False) -> str:
    """``quality`` with :data:`SECTIONS_VERSION`'s own ``;suspect`` flags (#835).

    Every business row carries them, not the 40-F's alone: ``module_relatedness.locator.asks`` and Value Chain
    read this column, and a 10-K whose cut is wrong but labelled ``ok`` is the case the flags exist for -- 24 of
    the 29 wrong cuts of the 10-K reading report were labelled ``ok``. Which signals a version defines is
    :attr:`sec_sections.Rule.item_signals` and :attr:`sec_sections.Rule.aif_signals`, so the row's
    ``rules_version`` says what was computed -- for a row written after #838, which is what split the two.

    ``aif`` is True for the one row this layer cuts with :func:`sec_sections.cut_aif_business`, because which
    cutter made the text is what picks the set and the form string does not say it (#838).
    """
    return sec_sections.cut_flag(quality, text, item, form, version=SECTIONS_VERSION, aif=aif)


def sections_of(html: str, form: str, *, exhibit: bool = False, by_name: object = None) -> list[tuple]:
    """``(section, text, quality, method, start_char, end_char)`` per section of one document.

    An exhibit is the business description on its own, so its whole text is the section and the 10-K item cuts do
    not apply to it (as ``sec/filings`` reads it today). A proxy is one uncut ``proxy`` row; every other form gives
    the uncut ``whole`` row plus the business section its own rule finds.
    """
    text = sec_sections.strip_html(html)
    if exhibit:
        # Chosen by its number alone, which is one filer's layout: say so on the row rather than letting a guess
        # read like a real Annual Information Form (``sec/filings`` carries the same flag).
        quality = "by_number" if by_name is False else _quality(text)
        return [(BUSINESS, text, quality, AIF, None, None)]
    if str(form) in filings.PROXY_FORMS:
        return [(PROXY, text, _quality(text), UNCUT, None, None)]
    out: list[tuple] = [(WHOLE, text, _quality(text), UNCUT, None, None)]
    if str(form).upper() == "40-F":
        # The item rules know a 10-K's Item 1 and a 20-F's Item 4; a Canadian 40-F has neither, so its business
        # section is the Annual Information Form block when the primary document keeps one.
        aif = sec_sections.cut_aif_business(text, version=SECTIONS_VERSION)
        if aif:
            out.append((BUSINESS, aif, _flag("ok", text, aif, form, aif=True), AIF, *_span(text, aif)))
        return out
    item, quality = sec_sections.cut_item(text, form, version=SECTIONS_VERSION)
    if item:
        out.append((BUSINESS, item, _flag(quality, text, item, form), HEADER, *_span(text, item)))
    return out


# ── what the Parquet tree already holds ─────────────────────────────────────────────────────────────────────────

def _glob(name: str) -> str | None:
    """The ``read_parquet`` glob over every company's ``name`` partition, or None when none exists yet."""
    folder = common.parquet_dir(NAME)
    pattern = f"cik=*/{name}"
    if not any(folder.glob(pattern)):
        return None
    return f"read_parquet({base.sql_path(folder / pattern)}, union_by_name = true)"


def held() -> tuple[dict[str, set[tuple[str, str]]], set[str]]:
    """``({cik: {(accession, document)}}, the CIKs holding a row of another cutter version)``.

    One query per Parquet file over the whole tree, not one read per company.
    """
    docs: dict[str, set[tuple[str, str]]] = {}
    old: set[str] = set()
    con = duckdb.connect()
    try:
        src = _glob(SECTIONS_PARQUET)
        if src:
            for cik, accession, document, version in con.execute(
                    f"SELECT cik, accession, document, rules_version FROM {src}").fetchall():
                docs.setdefault(str(cik), set()).add((str(accession), str(document)))
                if str(version) != SECTIONS_VERSION:
                    old.add(str(cik))
        src = _glob(NOTES_PARQUET)
        if src:
            for cik, version in con.execute(
                    f"SELECT DISTINCT cik, rules_version FROM {src}").fetchall():
                if str(version) != NOTES_VERSION:
                    old.add(str(cik))
    finally:
        con.close()
    return docs, old


def _partitions(name: str, ciks: frozenset[str] | None) -> list[Path]:
    folder = common.parquet_dir(NAME)
    if ciks is None:
        return sorted(folder.glob(f"cik=*/{name}"))
    return [p for c in sorted(ciks, key=int) if (p := folder / f"cik={c}" / name).exists()]


def dirty_ciks(ctx: base.RunContext) -> frozenset[str] | None:
    """The companies a build on the live copy reloads; None for a whole build.

    This layer's own, not ``fundamentals/common.dirty_ciks``: that one is hard-wired to ``fundamental_company``
    and to the fundamentals sources' keys.
    """
    plan = getattr(ctx, "incremental", None)
    if plan is None:
        return None
    d = plan.dirty.get(NAME)
    if d is None or d.all:
        return None
    return frozenset(d.keys)


def _by_version(con: duckdb.DuckDBPyConnection, table: str) -> dict[str, int]:
    return {str(v): int(n) for v, n in con.execute(
        f"SELECT rules_version, count(*) FROM {table} GROUP BY 1 ORDER BY 1").fetchall()}


def _counts(by_version: dict[str, int]) -> str:
    return ", ".join(f"{v} {n}" for v, n in by_version.items()) or "none"


class Forms:
    name = NAME
    kind = KIND
    providers = (common.SEC,)
    tables: tuple[str, ...] = (SECTION_TABLE, NOTE_TABLE)
    cadence = "daily"
    backfill_needs_from = False
    starts_by_hand = True
    dirty_keyed = True

    def default_as_of(self) -> date:
        from ....core import calendar
        return calendar.today_ny()

    def fetch(self, ctx: base.RunContext, as_of: date) -> list[Path]:
        """The stale companies' documents. No network: see the module docstring."""
        companies = common.ciks(common.universe())
        if not companies:
            ctx.notes.append("no companies yet (no spine file)")
            return []
        docs, old = held()
        out: list[Path] = []
        stale = 0
        for cik in companies:
            ctx.check_pause()
            found = targets(cik)
            if not found:
                continue
            if {(f["accession"], p.name) for f, p, _ in found} == docs.get(cik, set()) and cik not in old:
                continue
            out += [p for _, p, _ in found]
            stale += 1
        ctx.notes.append(f"{stale} of {len(companies)} companies to cut, {len(out)} documents "
                         f"(sections {SECTIONS_VERSION}, notes {NOTES_VERSION})")
        return out

    def backfill(self, ctx: base.RunContext, start: date | None, end: date) -> None:
        """Cut every company that has a stored document, whatever its Parquet holds."""
        out: list[Path] = []
        for cik in common.ciks(common.universe()):
            ctx.check_pause()
            out += [p for _, p, _ in targets(cik)]
        ctx.notes.append(f"backfill: {len(out)} documents")
        self.to_parquet_all(ctx, out)

    def to_parquet(self, ctx: base.RunContext, raw: Path) -> list[Path]:
        return self.to_parquet_all(ctx, [raw])

    def to_parquet_all(self, ctx: base.RunContext, raws: list[Path]) -> list[Path]:
        """Cut every document of each company ``raws`` names and write its two partitions.

        A company's partition is rewritten whole from its documents on disk, not merged: the cut is a pure function
        of the document and the rule version, so there is nothing of an older run to keep.
        """
        counts: dict = {}
        out: list[Path] = []
        for cik in sorted({Path(r).parent.name for r in raws}, key=int):
            ctx.check_pause()
            sections, notes = [], []
            for f, path, exhibit in targets(cik):
                meta = base.read_raw_json(common.meta_path(path))
                stamps = {"source_as_of": date.fromisoformat(meta["as_of"]),
                          "fetched_at": float(meta["fetched_at"]), "run_id": meta["run_id"]}
                filed = {"cik": cik, "accession": f["accession"], "document": path.name, "form": f["form"],
                         "filing_date": f["filing_date"]}
                html = base.read_raw(path).decode("utf-8", "replace")
                for section, text, quality, method, start, end in sections_of(
                        html, f["form"], exhibit=exhibit, by_name=meta.get("by_name")):
                    sections.append({**filed, "report_date": f.get("report_date"), "section": section,
                                     "text": text, "chars": len(text), "quality": quality, "start_char": start,
                                     "end_char": end, "method": method, "rules_version": SECTIONS_VERSION,
                                     **stamps})
                if not exhibit and f["form"] in filings.ANNUAL_FORMS:
                    for n in sec_notes.parse(html, counts)[0]:
                        # ``Note.row`` keys its row by an int CIK; this layer stores TEXT, as ``filing_item`` does,
                        # so ``filed`` overwrites that one column and adds the form and the filing date.
                        notes.append({**n.row(cik, f["accession"], path.name), **filed, **stamps})
            folder = common.parquet_dir(NAME) / f"cik={cik}"
            con = duckdb.connect()
            try:
                base.rows_table(con, "sections", SECTION, sections)
                out.append(base.write_parquet(con, "SELECT * FROM sections ORDER BY accession, document, section",
                                              folder / SECTIONS_PARQUET))
                base.rows_table(con, "notes", NOTE, notes)
                out.append(base.write_parquet(con, "SELECT * FROM notes ORDER BY accession, document, seq",
                                              folder / NOTES_PARQUET))
            finally:
                con.close()
        if counts:
            ctx.notes.append("note reader: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
        return out

    def dirty(self, ctx: base.RunContext):
        """The companies whose ``cik=<cik>`` partitions changed since the live file was built (#446)."""
        return dirty_set.keyed(ctx, self)

    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        ciks = dirty_ciks(ctx)
        rows: dict[str, int] = {}
        for table, schema, partition in ((SECTION_TABLE, SECTION, SECTIONS_PARQUET),
                                         (NOTE_TABLE, NOTE, NOTES_PARQUET)):
            cols = ", ".join(f'"{c}"' for c, _ in schema)
            if ciks is None:
                con.execute("CREATE TABLE {} ({})".format(
                    table, ", ".join(f'"{c}" {t}' for c, t in schema)))
            elif ciks:
                con.execute(f"DELETE FROM {table} WHERE cik IN "
                            f"({', '.join(repr(c) for c in sorted(ciks, key=int))})")
            files = _partitions(partition, ciks)
            if files:
                src = f"read_parquet([{', '.join(base.sql_path(f) for f in files)}], union_by_name = true)"
                con.execute(f"INSERT INTO {table} SELECT {cols} FROM {base.with_all(con, schema, src)} "
                            "ORDER BY cik, accession, document")
            rows[table] = con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
        return rows

    def check(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict:
        """The build's own report.

        The rows per ``rules_version`` go in the **note**, not in a field of their own: a ``dataset_check`` row
        carries only ``ops.CHECK_FIELDS``, so a dict key here would be dropped on the way to the screen. They are
        what makes a half-migrated table visible rather than inferred -- after ``ours-2`` lands, two versions in
        that line means the re-cut has not finished.
        """
        keys = ["cik", "accession", "document", "section", "rules_version"]
        added, removed = base.compare_with_live(con, KIND, SECTION_TABLE, keys)
        by_version = _by_version(con, SECTION_TABLE)
        notes_by_version = _by_version(con, NOTE_TABLE)
        companies = con.execute(f"SELECT count(DISTINCT cik) FROM {SECTION_TABLE}").fetchone()[0]
        as_of = con.execute(f"SELECT max(source_as_of) FROM {SECTION_TABLE}").fetchone()[0]
        return {"dataset": NAME, "result": "changed" if added or removed else "unchanged",
                "source_as_of": (as_of or ctx.as_of or self.default_as_of()).isoformat(),
                "rows": sum(by_version.values()), "added": added, "removed": removed,
                "note": f"{companies} companies; sections {_counts(by_version)}; "
                        f"notes {_counts(notes_by_version)}"}


DATASET = Forms()
