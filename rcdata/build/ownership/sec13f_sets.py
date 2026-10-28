"""The SEC's Form 13F data sets: how ``sec/13f`` is first filled (avo, 2026-10-28: the zips, as the old board did,
from the start of 2025); the EDGAR forms (``edgar.Feed``) follow from the newest set's last day.

``GET <RC_FORM13F_INDEX_URL>`` (default ``<www.sec.gov>/data-research/sec-markets-data/form-13f-data-sets``) links
one zip per filing window: ``<ddmonyyyy>-<ddmonyyyy>_form13f.zip`` since 2024 (three months, ``01jun2026-31aug2026``),
``<yyyy>q<n>_form13f.zip`` before. Each link is resolved against the page (on 2026-10-28 the newest sat in another
folder than the rest). A page that links no zip is refused (its shape changed). The window is the filing dates the
set holds.

A zip holds tab-separated ``SUBMISSION``, ``COVERPAGE``, ``SUMMARYPAGE`` and ``INFOTABLE`` (others are not read),
matched by name suffix, case-insensitive (one set nests them in an upper-case folder), read with no quote character
(names hold quotes) into a scratch DuckDB file beside the raw zip, so a set's 3.8 million holdings never sit in
memory. Each 13F-HR and 13F-HR/A becomes the row the EDGAR path writes (``sec13f.FILINGS``, ``origin``
``set:<zip>``), its information table its holdings (``sec13f.HOLDINGS``, ``line`` in ``INFOTABLE_SK`` order):

- an amendment by the ``/A`` of ``SUBMISSIONTYPE`` (``ISAMENDMENT`` is blank on half the rows of 01jun2026-31aug2026);
  ``AMENDMENTTYPE`` is ``RESTATEMENT`` or ``NEW HOLDINGS``;
- dates are ``DD-MON-YYYY``; the CIK loses its leading zeros, as the EDGAR path writes it;
- ``ISCONFIDENTIALOMITTED`` ``Y`` / ``N`` is ``confidential_omitted``; a 13F notice (``13F-NT``) is not kept, as the
  EDGAR path does not fetch one;
- the status is ``sec13f.status_of``'s rule, written in SQL: a 13F-HR whose report type is a notice is ``notice``, and
  one with no holdings in ``INFOTABLE`` is ``unparsed`` unless its summary says 0 entries.

The rows go to the EDGAR walk's own day partitions (``filings/day=<filed>``, ``holdings/day=<filed>``), and every
weekday of the window inside the requested range, up to the set's newest filing, is marked done for that walk (a day
with no 13F in the set has none), so the walk reads the forms only after it. A set the SEC publishes while its window
is still open thus leaves its later days to the walk instead of hiding them. A day the walk already finished is not
written again.

Raw: ``raw/sec/13f_sets/<zip>`` as downloaded with a ``.meta.json`` sidecar (URL, ``Last-Modified``, run). Each
download is one paced SEC request (``sec.Pacer``), about 100 MB. A backfill asks again for a set it holds with
``If-Modified-Since`` (the SEC answers 304, no body): a set published again replaces the kept one and the days it had
filled are reopened and typed again; the days the walk finished stay the walk's.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
import zipfile
from datetime import date, timedelta
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin

import duckdb

from .. import base
from . import edgar, sec

INDEX_ENV = "RC_FORM13F_INDEX_URL"
INDEX_PATH = "/data-research/sec-markets-data/form-13f-data-sets"
MEMBERS = ("SUBMISSION", "COVERPAGE", "SUMMARYPAGE", "INFOTABLE")
ORIGIN = "set:"
_MONTHS = {m: n for n, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                       "dec"), 1)}


def index_url() -> str:
    return os.getenv(INDEX_ENV) or sec.www_base() + INDEX_PATH


def window(name: str) -> tuple[date, date] | None:
    """The filing dates a set holds, from its name; None for a name of neither shape."""
    low = name.lower()
    m = re.fullmatch(r"(\d{2})([a-z]{3})(\d{4})-(\d{2})([a-z]{3})(\d{4})_form13f\.zip", low)
    if m and m[2] in _MONTHS and m[5] in _MONTHS:
        return (date(int(m[3]), _MONTHS[m[2]], int(m[1])), date(int(m[6]), _MONTHS[m[5]], int(m[4])))
    m = re.fullmatch(r"(\d{4})q([1-4])_form13f\.zip", low)
    if m:
        year, q = int(m[1]), int(m[2])
        after = date(year + 1, 1, 1) if q == 4 else date(year, 3 * q + 1, 1)
        return date(year, 3 * q - 2, 1), after - timedelta(days=1)
    return None


def parse_index(html: str, page: str) -> dict[str, str]:
    """``{zip name: absolute URL}`` of the page's links, each resolved against the page."""
    out = {}
    for href in re.findall(r"""href\s*=\s*["']([^"']*?_form13f\.zip)["']""", html, flags=re.I):
        url = urljoin(page, href)
        out[url.rsplit("/", 1)[1]] = url
    return out


def raw_dir() -> Path:
    return base.raw_root() / "sec" / "13f_sets"


def raw_path(name: str) -> Path:
    return raw_dir() / name


def _meta(path: Path) -> Path:
    return path.with_name(path.name + ".meta.json")


def listed(ctx: base.RunContext, pacer: sec.Pacer) -> dict[str, tuple[date, date, str]]:
    """``{name: (first day, last day, URL)}`` of the sets the page links (one request)."""
    page = index_url()
    sets = parse_index(sec.get(ctx, pacer, page).text, page)
    if not sets:
        raise base.SourceError(f"13F data sets: {page} links no *_form13f.zip (its shape changed?)")
    out = {}
    for name, url in sets.items():
        span = window(name)
        if span is None:
            ctx.notes.append(f"13F data set {name}: no filing window in its name, not read")
            continue
        out[name] = (*span, url)
    return out


def download(ctx: base.RunContext, pacer: sec.Pacer, name: str, url: str) -> tuple[Path, bool]:
    """``(the set's zip under raw/sec/13f_sets/, whether it replaced a kept one)``. A kept set is asked again with
    ``If-Modified-Since`` its ``Last-Modified`` (one without one is kept as it is); an answer that is not a zip is
    refused and not kept."""
    path = raw_path(name)
    kept = path.exists()
    seen = (base.read_raw_json(_meta(path)) if _meta(path).exists() else {}).get("last_modified") if kept else None
    if kept and not seen:
        return path, False
    r = sec.get(ctx, pacer, url, headers={"If-Modified-Since": seen} if seen else None)
    if kept and (r.status_code == 304 or r.headers.get("last-modified") == seen):
        return path, False
    if not zipfile.is_zipfile(BytesIO(r.content)):
        raise base.SourceError(f"13F data set {name}: the answer is not a zip ({len(r.content)} bytes)")
    base.write_raw_json(_meta(path), {"url": url, "last_modified": r.headers.get("last-modified"),
                                      "fetched_at": time.time(), "run_id": ctx.run_id, "bytes": len(r.content)})
    return base.write_raw(path, r.content), kept


def _reopen(feed: edgar.Feed, name: str, first: date, last: date) -> int:
    """Take back the done marks ``name`` wrote from ``first`` to ``last`` (not the walk's): the days typed again."""
    n = 0
    for k in range((last - first).days + 1):
        mark = feed.day_dir(first + timedelta(days=k)) / edgar.DONE
        if mark.exists() and json.loads(mark.read_text(encoding="utf-8")).get("origin") == ORIGIN + name:
            mark.unlink()
            n += 1
    return n


def _member(z: zipfile.ZipFile, table: str) -> str:
    want = f"{table}.TSV"
    for m in z.namelist():
        up = m.upper()
        if up == want or up.endswith("/" + want):
            return m
    raise base.SourceError(f"13F data set {Path(z.filename or '').name}: no {table}.tsv in the zip")


def _load(con: duckdb.DuckDBPyConnection, zip_path: Path, tmp: Path) -> None:
    """The set's four tables as ``z_submission``, ``z_coverpage``, ``z_summarypage``, ``z_infotable`` (text)."""
    with zipfile.ZipFile(zip_path) as z:
        for table in MEMBERS:
            out = tmp / f"{table}.tsv"
            with z.open(_member(z, table)) as src, open(out, "wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
            con.execute(f"CREATE TABLE z_{table.lower()} AS SELECT * FROM read_csv({base.sql_path(out)}, "
                        "delim = '\t', header = true, all_varchar = true, quote = '', escape = '')")
            out.unlink()


def _day(col: str) -> str:
    return f"CAST(try_strptime(trim({col}), '%d-%b-%Y') AS DATE)"


def _typed(con: duckdb.DuckDBPyConnection, name: str, first: date, last: date, forms, dollars_from: date,
           fetched_at: float, run_id: str | None) -> None:
    """``filings`` and ``holdings`` of the set's 13F-HR and 13F-HR/A filed from ``first`` to ``last``, typed as the
    EDGAR path writes them."""
    form_list = ", ".join(f"'{f}'" for f in sorted(forms))
    con.execute(f"""CREATE TABLE z_sub AS
        SELECT trim(ACCESSION_NUMBER) AS accession, CAST(TRY_CAST(trim(CIK) AS BIGINT) AS VARCHAR) AS cik,
               upper(trim(SUBMISSIONTYPE)) AS form, {_day('PERIODOFREPORT')} AS period,
               {_day('FILING_DATE')} AS filed
        FROM z_submission WHERE upper(trim(SUBMISSIONTYPE)) IN ({form_list})
          AND {_day('FILING_DATE')} BETWEEN DATE '{first}' AND DATE '{last}'""")
    con.execute("""CREATE TABLE z_count AS
        SELECT trim(ACCESSION_NUMBER) AS accession, count(*) AS n FROM z_infotable
        WHERE trim(ACCESSION_NUMBER) IN (SELECT accession FROM z_sub) GROUP BY ALL""")
    con.execute(f"""CREATE TABLE filings AS
        SELECT s.accession, s.cik, nullif(trim(c.FILINGMANAGER_NAME), '') AS filer_name, s.form,
               coalesce(s.period, {_day('c.REPORTCALENDARORQUARTER')}) AS period, s.filed, s.filed AS index_day,
               ends_with(s.form, '/A') AS is_amendment, TRY_CAST(trim(c.AMENDMENTNO) AS INTEGER) AS amendment_no,
               nullif(upper(regexp_replace(trim(c.AMENDMENTTYPE), '\\s+', ' ', 'g')), '') AS amendment_type,
               nullif(upper(regexp_replace(trim(c.REPORTTYPE), '\\s+', ' ', 'g')), '') AS report_type,
               TRY_CAST(trim(m.OTHERINCLUDEDMANAGERSCOUNT) AS INTEGER) AS other_managers,
               TRY_CAST(trim(m.TABLEENTRYTOTAL) AS INTEGER) AS entry_total,
               TRY_CAST(trim(m.TABLEVALUETOTAL) AS DOUBLE) AS value_total,
               CAST(coalesce(k.n, 0) AS INTEGER) AS holdings,
               CASE WHEN s.filed < DATE '{dollars_from}' THEN 1000 ELSE 1 END AS value_scale,
               CASE upper(trim(m.ISCONFIDENTIALOMITTED)) WHEN 'Y' THEN true WHEN 'N' THEN false END
                   AS confidential_omitted,
               CASE WHEN coalesce(s.period, {_day('c.REPORTCALENDARORQUARTER')}) IS NULL THEN 'unparsed'
                    WHEN upper(c.REPORTTYPE) LIKE '%NOTICE%' THEN 'notice'
                    WHEN coalesce(k.n, 0) = 0 AND TRY_CAST(trim(m.TABLEENTRYTOTAL) AS INTEGER) IS DISTINCT FROM 0
                    THEN 'unparsed' ELSE 'ok' END AS status,
               CASE WHEN coalesce(s.period, {_day('c.REPORTCALENDARORQUARTER')}) IS NULL THEN 'no report period'
                    WHEN upper(c.REPORTTYPE) LIKE '%NOTICE%' THEN NULL
                    WHEN coalesce(k.n, 0) = 0 AND TRY_CAST(trim(m.TABLEENTRYTOTAL) AS INTEGER) IS DISTINCT FROM 0
                    THEN 'no information table'
                    WHEN TRY_CAST(trim(m.TABLEENTRYTOTAL) AS INTEGER) <> coalesce(k.n, 0)
                    THEN coalesce(k.n, 0) || ' holdings read, the summary says ' || trim(m.TABLEENTRYTOTAL)
               END AS detail,
               '{ORIGIN}{name}' AS origin, {fetched_at!r} AS fetched_at, ? AS run_id
        FROM z_sub s
        LEFT JOIN z_coverpage c ON trim(c.ACCESSION_NUMBER) = s.accession
        LEFT JOIN z_summarypage m ON trim(m.ACCESSION_NUMBER) = s.accession
        LEFT JOIN z_count k ON k.accession = s.accession""", [run_id])

    def num(col: str, typ: str = "DOUBLE") -> str:
        return f"TRY_CAST(TRY_CAST(replace(trim({col}), ',', '') AS DOUBLE) AS {typ})"

    con.execute(f"""CREATE TABLE holdings AS
        SELECT f.accession, f.cik, f.period, f.filed,
               CAST(row_number() OVER (PARTITION BY f.accession
                                       ORDER BY TRY_CAST(trim(i.INFOTABLE_SK) AS BIGINT), i.INFOTABLE_SK) - 1
                    AS INTEGER) AS line,
               nullif(trim(i.NAMEOFISSUER), '') AS name_of_issuer, nullif(trim(i.TITLEOFCLASS), '') AS title_of_class,
               nullif(upper(trim(i.CUSIP)), '') AS cusip, nullif(trim(i.FIGI), '') AS figi,
               {num('i.VALUE')} AS value, {num('i.VALUE')} * f.value_scale AS value_usd,
               {num('i.SSHPRNAMT')} AS shares, nullif(upper(trim(i.SSHPRNAMTTYPE)), '') AS sh_type,
               nullif(trim(i.PUTCALL), '') AS put_call, nullif(trim(i.INVESTMENTDISCRETION), '') AS discretion,
               nullif(trim(i.OTHERMANAGER), '') AS other_manager, {num('i.VOTING_AUTH_SOLE', 'BIGINT')} AS vote_sole,
               {num('i.VOTING_AUTH_SHARED', 'BIGINT')} AS vote_shared,
               {num('i.VOTING_AUTH_NONE', 'BIGINT')} AS vote_none
        FROM z_infotable i JOIN filings f ON f.accession = trim(i.ACCESSION_NUMBER)""")


def fill(ctx: base.RunContext, feed: edgar.Feed, start: date, end: date, schema: dict) -> date | None:
    """Fill ``feed``'s days from ``start`` to ``end`` that a listed set covers; the last day covered (None when no
    set covers any). ``schema``: ``{"forms", "filings", "holdings", "dollars_from"}`` of ``sec/13f``."""
    pacer = sec.Pacer(ctx)
    covered = None
    for name, (first, last, url) in sorted(listed(ctx, pacer).items(), key=lambda kv: kv[1][0]):
        lo, hi = max(first, start), min(last, end)
        if lo > hi:
            continue
        path, replaced = download(ctx, pacer, name, url)
        if replaced:
            ctx.notes.append(f"13F data set {name}: changed since it was read, "
                             f"{_reopen(feed, name, first, last)} days reopened")
        written = _fill_one(ctx, feed, path, name, lo, hi, schema)
        ctx.notes.append(f"13F data set {name}: {written} days written, {lo} to {hi}")
        covered = hi if covered is None else max(covered, hi)
    return covered


def _fill_one(ctx: base.RunContext, feed: edgar.Feed, zip_path: Path, name: str, first: date, last: date,
              schema: dict) -> int:
    days = [first + timedelta(days=n) for n in range((last - first).days + 1)]
    todo = [d for d in days if d.weekday() < 5 and not feed.is_done(d)]
    if not todo:
        return 0
    raw_dir().mkdir(parents=True, exist_ok=True)
    fetched_at = (base.read_raw_json(_meta(zip_path)) if _meta(zip_path).exists() else {}).get("fetched_at")
    with tempfile.TemporaryDirectory(dir=raw_dir(), prefix=".typing-") as tmp:
        con = duckdb.connect(str(Path(tmp) / "set.duckdb"))
        try:
            _load(con, zip_path, Path(tmp))
            newest = con.execute(f"SELECT max({_day('FILING_DATE')}) FROM z_submission").fetchone()[0]
            later = [d for d in todo if newest is None or d > newest]
            if later:
                ctx.notes.append(f"13F data set {name}: its newest filing is {newest}; {len(later)} later days left "
                                 "to the forms walk")
                todo = [d for d in todo if d not in later]
            if not todo:
                return 0
            _typed(con, name, todo[0], todo[-1], schema["forms"], schema["dollars_from"],
                   fetched_at or zip_path.stat().st_mtime, ctx.run_id)
            filed = {r[0] for r in con.execute("SELECT DISTINCT filed FROM filings").fetchall()}
            cols_f = ", ".join(f'"{c}"' for c, _ in schema["filings"])
            cols_h = ", ".join(f'"{c}"' for c, _ in schema["holdings"])
            written = 0
            for day in todo:
                if day in filed:
                    base.write_parquet(con, f"SELECT {cols_f} FROM filings WHERE filed = DATE '{day}' "
                                            "ORDER BY accession",
                                       feed.parquet_dir() / "filings" / f"day={day}" / "part-0.parquet")
                    base.write_parquet(con, f"SELECT {cols_h} FROM holdings WHERE filed = DATE '{day}' "
                                            "ORDER BY accession, line",
                                       feed.parquet_dir() / "holdings" / f"day={day}" / "part-0.parquet")
                    written += 1
                d = feed.day_dir(day)
                d.mkdir(parents=True, exist_ok=True)
                (d / edgar.DONE).write_text(json.dumps({"run_id": ctx.run_id, "at": time.time(),
                                                        "origin": ORIGIN + name}), encoding="utf-8")
            return written
        finally:
            con.close()
