"""The SEC's Insider Transactions Data Sets: how ``sec/form4`` gets its history without one request per filing
(2014 to today is about two million Form 4s; the sets are 51 zips).

``GET <RC_FORM345_INDEX_URL>`` (default ``<www.sec.gov>/data-research/sec-markets-data/insider-transactions-data-sets``)
links one zip per quarter, ``<yyyy>q<n>_form345.zip``, back to 2006q1 (83 on 2026-11-06, in two folders, so each link
is resolved against the page). The window is the filing dates the set holds. The index read, the name parsing, the
download and the TSV load are the 13F sets' own (``sec13f_sets``), which take the suffix, the raw folder and the
members as arguments.

A zip holds tab-separated ``SUBMISSION``, ``REPORTINGOWNER``, ``NONDERIV_TRANS`` and ``DERIV_TRANS`` (the four
holdings, footnote and signature members are not read: a holding without a transaction is not a row on either path).
Forms 3, 4 and 5 are in one set; only ``sec/form4``'s own forms are typed. Each becomes the rows the EDGAR path
writes (``form4.ROWS``), one per reported transaction:

- dates are ``DD-MON-YYYY``; the issuer CIK and the owner CIK lose their leading zeros, as the EDGAR path writes
  them; the ticker is ``ISSUERTRADINGSYMBOL`` through ``common.norm_symbol`` — the same function, called on the
  set's distinct symbols, never a second copy of it in SQL;
- ``RPTOWNER_RELATIONSHIP`` is one text field (``Director,Officer``, and without the comma in
  ``TenPercentOwnerOther``), so the four flags are substring tests, and the EDGAR path's ``is_*`` booleans become
  true/false here rather than NULL;
- the first reporting owner is the file's first row for that accession, and ``owners`` their number, as the EDGAR
  path takes the document's first ``reportingOwner``;
- a price or share count given only as a footnote is blank in the set and NULL here, as on the EDGAR path;
- ``aff10b5One`` has no column in the older sets: it is NULL for them, and the note says so;
- a filing with no transaction row is one ``no_transaction`` row, kept, never dropped.

The rows go to the EDGAR walk's own day partitions (``rows/day=<filed>``) and every weekday of the window inside the
requested range, up to the set's newest filing, is marked done for that walk, so the walk reads the forms only after
it. A day the walk already finished is not typed from a set, and a set published while its window is still open
leaves its later days to the walk. A range of fewer than ``sec13f_sets.MIN_WEEKDAYS`` weekdays asks for nothing: a
small repair reads the forms.

Under ``--no-parquet`` (raw only) the zips are staged and nothing is typed: no day is marked, so a later ordinary
run types them from the kept zips without asking the SEC again.

Raw: ``raw/sec/form345_sets/<zip>`` with a ``.meta.json`` sidecar; each download is one paced SEC request, 10 to
80 MB. A backfill asks again for a set it holds with ``If-Modified-Since`` (the SEC answers 304, no body): a set
published again replaces the kept one and the days it had filled are typed again.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

import duckdb

from ....core import calendar
from .. import base
from . import common, edgar, sec, sec13f_sets

INDEX_ENV = "RC_FORM345_INDEX_URL"
INDEX_PATH = "/data-research/sec-markets-data/insider-transactions-data-sets"
SUFFIX = "_form345"
SUB = "form345_sets"
LABEL = "insider transactions"
MEMBERS = ("SUBMISSION", "REPORTINGOWNER", "NONDERIV_TRANS", "DERIV_TRANS")
# The flag the SEC added in 2023; the older sets have no column for it, so it is added empty (as 13F's FIGI is).
AFF = "AFF10B5ONE"
TX = (("non_derivative", "z_nonderiv_trans", "NONDERIV_TRANS_SK"), ("derivative", "z_deriv_trans", "DERIV_TRANS_SK"))


def index_url() -> str:
    return os.getenv(INDEX_ENV) or sec.www_base() + INDEX_PATH


def _num(col: str, typ: str = "DOUBLE") -> str:
    return f"TRY_CAST(TRY_CAST(replace(trim({col}), ',', '') AS DOUBLE) AS {typ})"


def _flag(col: str) -> str:
    return (f"CASE lower(trim({col})) WHEN '1' THEN true WHEN 'true' THEN true WHEN 'y' THEN true "
            "WHEN 'yes' THEN true WHEN '0' THEN false WHEN 'false' THEN false WHEN 'n' THEN false "
            "WHEN 'no' THEN false END")


def _typed(con: duckdb.DuckDBPyConnection, name: str, first: date, last: date, forms, fetched_at: float,
           run_id: str | None, notes: list[str]) -> None:
    """``rows``: the set's filings of ``forms`` filed from ``first`` to ``last``, one row per reported transaction,
    typed as the EDGAR path writes them."""
    day = sec13f_sets._day
    form_list = ", ".join(f"'{f}'" for f in sorted(forms))
    # One row per accession in each of the two per-filing members: a row written twice would fan the filing's
    # transactions out. Of conflicting rows the first the file holds is kept (``rowid`` is the file order).
    con.execute("""CREATE OR REPLACE TABLE z_submission AS SELECT * FROM z_submission
        QUALIFY row_number() OVER (PARTITION BY trim(ACCESSION_NUMBER) ORDER BY rowid) = 1""")
    has_aff = any(r[0] == AFF for r in con.execute("DESCRIBE z_submission").fetchall())
    if not has_aff:
        notes.append(f"{LABEL} data set {name}: no {AFF} column, aff10b5one is NULL for it")
    con.execute(f"""CREATE TABLE z_sub AS
        SELECT trim(ACCESSION_NUMBER) AS accession, upper(trim(DOCUMENT_TYPE)) AS form,
               {day('FILING_DATE')} AS filed,
               CAST(TRY_CAST(trim(ISSUERCIK) AS BIGINT) AS VARCHAR) AS issuer_cik,
               nullif(trim(ISSUERNAME), '') AS issuer_name,
               nullif(trim(ISSUERTRADINGSYMBOL), '') AS issuer_symbol,
               TRY_CAST(trim(FORM4_TRANS_REPORTED) AS INTEGER) AS reported,
               {_flag(AFF) if has_aff else 'CAST(NULL AS BOOLEAN)'} AS aff10b5one
        FROM z_submission WHERE upper(trim(DOCUMENT_TYPE)) IN ({form_list})
          AND {day('FILING_DATE')} BETWEEN DATE '{first}' AND DATE '{last}'""")
    # The ticker is the app's own function on the set's distinct symbols, not a second copy of it in SQL.
    syms = [r[0] for r in con.execute("SELECT DISTINCT issuer_symbol FROM z_sub "
                                      "WHERE issuer_symbol IS NOT NULL").fetchall()]
    con.execute("CREATE TABLE z_sym (raw VARCHAR, symbol VARCHAR)")
    if syms:
        con.executemany("INSERT INTO z_sym VALUES (?, ?)",
                        [(s, (common.norm_symbol(s) or None) if s.upper() != "NONE" else None) for s in syms])
    # The first reporting owner is resolved once and then projected. Four independent aggregates would each skip
    # their own NULLs -- a blank field reads as NULL here, there is no quote character -- so a first owner with no
    # title (every Director row) would take the next owner's title. The 13F path's per-filing members use this
    # same window shape.
    con.execute("""CREATE TABLE z_own AS
        SELECT trim(ACCESSION_NUMBER) AS accession,
               CAST(count(*) OVER (PARTITION BY trim(ACCESSION_NUMBER)) AS INTEGER) AS owners,
               CAST(TRY_CAST(trim(RPTOWNERCIK) AS BIGINT) AS VARCHAR) AS owner_cik,
               nullif(trim(RPTOWNERNAME), '') AS owner_name,
               upper(trim(RPTOWNER_RELATIONSHIP)) AS relationship,
               nullif(trim(RPTOWNER_TITLE), '') AS officer_title
        FROM z_reportingowner
        QUALIFY row_number() OVER (PARTITION BY trim(ACCESSION_NUMBER) ORDER BY rowid) = 1""")
    # Finding B: the tokens are matched upper-case, and a relationship naming none of them is said out loud rather
    # than quietly leaving all four false.
    odd = con.execute("""SELECT count(*), min(relationship) FROM z_own WHERE relationship IS NOT NULL
        AND NOT regexp_matches(relationship, 'DIRECTOR|OFFICER|TENPERCENTOWNER|OTHER')""").fetchone()
    if odd[0]:
        notes.append(f"{LABEL} data set {name}: {odd[0]} filing(s) whose reporting-owner relationship names none of "
                     f"the four roles (first: {odd[1]!r}), so their four flags are false")
    con.execute(f"""CREATE TABLE z_head AS
        SELECT s.accession, s.form, s.filed, s.filed AS index_day, s.issuer_cik, s.issuer_name,
               s.issuer_symbol, y.symbol, o.owner_cik, o.owner_name, coalesce(o.owners, 0) AS owners,
               s.reported,
               contains(o.relationship, 'DIRECTOR') AS is_director,
               contains(o.relationship, 'OFFICER') AS is_officer, o.officer_title,
               contains(o.relationship, 'TENPERCENTOWNER') AS is_ten_pct,
               contains(o.relationship, 'OTHER') AS is_other, s.aff10b5one,
               {fetched_at!r} AS fetched_at, ? AS run_id
        FROM z_sub s LEFT JOIN z_own o ON o.accession = s.accession
                     LEFT JOIN z_sym y ON y.raw = s.issuer_symbol""", [run_id])
    union = " UNION ALL ".join(f"""
        SELECT trim(t.ACCESSION_NUMBER) AS accession, '{label}' AS tx_table, t.{sk} AS sk,
               nullif(trim(t.SECURITY_TITLE), '') AS security_title, {day('t.TRANS_DATE')} AS transaction_date,
               nullif(trim(t.TRANS_CODE), '') AS code, {_flag('t.EQUITY_SWAP_INVOLVED')} AS equity_swap,
               {_num('t.TRANS_SHARES')} AS shares, {_num('t.TRANS_PRICEPERSHARE')} AS price,
               nullif(trim(t.TRANS_ACQUIRED_DISP_CD), '') AS acquired_disposed,
               {_num('t.SHRS_OWND_FOLWNG_TRANS')} AS shares_after,
               nullif(trim(t.DIRECT_INDIRECT_OWNERSHIP), '') AS direct_indirect,
               {_num('t.CONV_EXERCISE_PRICE') if label == 'derivative' else 'CAST(NULL AS DOUBLE)'}
                   AS exercise_price,
               {"nullif(trim(t.UNDLYNG_SEC_TITLE), '')" if label == 'derivative' else 'CAST(NULL AS VARCHAR)'}
                   AS underlying_title,
               {_num('t.UNDLYNG_SEC_SHARES') if label == 'derivative' else 'CAST(NULL AS DOUBLE)'}
                   AS underlying_shares,
               {day('t.EXPIRATION_DATE') if label == 'derivative' else 'CAST(NULL AS DATE)'} AS expiration_date
        FROM {table} t JOIN z_head h ON h.accession = trim(t.ACCESSION_NUMBER)""" for label, table, sk in TX)
    con.execute(f"""CREATE TABLE z_tx AS SELECT * FROM ({union})""")
    # ``detail`` and ``status`` from the count the submission itself reports (FORM4_TRANS_REPORTED), as the 13F
    # path reads TABLEENTRYTOTAL: a filing whose transaction rows the loader could not read is said to be short
    # rather than looking like a clean holdings-only Form 4. The column is blank in the older sets, so NULL there.
    short = ("CASE WHEN h.reported IS NOT NULL AND h.reported <> {n} THEN {n} || "
             "' transactions read, the submission says ' || h.reported END")
    con.execute(f"""CREATE TABLE rows AS
        SELECT h.*, t.tx_table,
               CAST(row_number() OVER (PARTITION BY t.accession, t.tx_table
                                       ORDER BY TRY_CAST(trim(t.sk) AS BIGINT), t.sk) - 1 AS INTEGER) AS line,
               t.security_title, t.transaction_date, t.code, t.equity_swap, t.shares, t.price, t.acquired_disposed,
               t.shares_after, t.direct_indirect, t.exercise_price, t.underlying_title, t.underlying_shares,
               t.expiration_date, 'ok' AS status,
               {short.format(n='count(*) OVER (PARTITION BY t.accession)')} AS detail
        FROM z_head h JOIN z_tx t ON t.accession = h.accession
        UNION ALL BY NAME
        SELECT h.*, CAST(NULL AS VARCHAR) AS tx_table, CAST(NULL AS INTEGER) AS line,
               CAST(NULL AS VARCHAR) AS security_title, CAST(NULL AS DATE) AS transaction_date,
               CAST(NULL AS VARCHAR) AS code, CAST(NULL AS BOOLEAN) AS equity_swap, CAST(NULL AS DOUBLE) AS shares,
               CAST(NULL AS DOUBLE) AS price, CAST(NULL AS VARCHAR) AS acquired_disposed,
               CAST(NULL AS DOUBLE) AS shares_after, CAST(NULL AS VARCHAR) AS direct_indirect,
               CAST(NULL AS DOUBLE) AS exercise_price, CAST(NULL AS VARCHAR) AS underlying_title,
               CAST(NULL AS DOUBLE) AS underlying_shares, CAST(NULL AS DATE) AS expiration_date,
               CASE WHEN coalesce(h.reported, 0) <> 0 THEN 'unparsed' ELSE 'no_transaction' END AS status,
               {short.format(n='0')} AS detail
        FROM z_head h WHERE NOT EXISTS (SELECT 1 FROM z_tx t WHERE t.accession = h.accession)""")


def _fill_one(ctx: base.RunContext, feed: edgar.Feed, zip_path: Path, name: str, first: date, last: date,
              schema: dict, open_window: bool = False) -> int:
    """Type the set's filings of the weekdays ``first``..``last`` the walk has not finished, write each day's
    partition and mark it done; the days written."""
    if base.raw_only():
        # Raw-only (--no-parquet, #498), as the 13F sets do it: the zip is staged in raw above, and typing it here
        # would write the ``.done`` marker of every day in range while base.write_parquet writes no partition, so
        # those days would never be typed again (the forms walk skips a marked day, and _reopen is the only way back).
        ctx.notes.append(f"{LABEL} data set {name}: raw only, the zip is staged and not typed")
        return 0
    days = [first + timedelta(days=n) for n in range((last - first).days + 1)]
    todo = [d for d in days if d.weekday() < 5 and not feed.is_done(d)]
    if not todo:
        return 0
    root = sec13f_sets.raw_dir(SUB)
    root.mkdir(parents=True, exist_ok=True)
    meta = sec13f_sets._meta(zip_path)
    fetched_at = (base.read_raw_json(meta) if meta.exists() else {}).get("fetched_at")
    with tempfile.TemporaryDirectory(dir=root, prefix=sec13f_sets.SCRATCH) as tmp:
        con = duckdb.connect(str(Path(tmp) / "set.duckdb"))
        try:
            sec13f_sets._load(con, zip_path, Path(tmp), name, ctx.notes, MEMBERS, {}, LABEL)
            newest = con.execute(f"SELECT max({sec13f_sets._day('FILING_DATE')}) FROM z_submission").fetchone()[0]
            if newest is not None and open_window:
                # The set's quarter is still running, so the extract was cut mid-day and its newest day holds only
                # part of that day's filings. The walk takes it.
                newest -= timedelta(days=1)
            later = [d for d in todo if newest is None or d > newest]
            if later:
                ctx.notes.append(f"{LABEL} data set {name}: its newest filing is {newest}; {len(later)} later days "
                                 "left to the forms walk")
                todo = [d for d in todo if d not in later]
            if not todo:
                return 0
            _typed(con, name, todo[0], todo[-1], schema["forms"], fetched_at or zip_path.stat().st_mtime, ctx.run_id,
                   ctx.notes)
            filed = {r[0] for r in con.execute("SELECT DISTINCT filed FROM rows").fetchall()}
            cols = ", ".join(f'CAST("{c}" AS {t}) AS "{c}"' for c, t in schema["rows"])
            written, blank = 0, []
            for day in todo:
                if day not in filed:
                    # Not marked: every business day carries hundreds of Forms 3, 4 and 5, so a day the set holds
                    # none for means the set lost them (a rejected TSV row, a date that does not parse), and a
                    # marker would seal it for good -- the walk skips a marked day. The walk reads its forms.
                    blank.append(day)
                    continue
                base.write_parquet(con, f"SELECT {cols} FROM rows WHERE filed = DATE '{day}' "
                                        "ORDER BY accession, tx_table, line",
                                   feed.parquet_dir() / "rows" / f"day={day}" / "part-0.parquet")
                written += 1
                d = feed.day_dir(day)
                d.mkdir(parents=True, exist_ok=True)
                (d / edgar.DONE).write_text(json.dumps({"run_id": ctx.run_id, "at": time.time(),
                                                        "origin": sec13f_sets.ORIGIN + name}),
                                            encoding="utf-8")
            if blank:
                ctx.notes.append(f"{LABEL} data set {name}: {len(blank)} weekday(s) it holds no filing for, "
                                 f"left to the forms walk (first {blank[0]})")
            return written
        finally:
            con.close()


def fill(ctx: base.RunContext, feed: edgar.Feed, start: date, end: date, schema: dict) -> date | None:
    """Fill ``feed``'s days from ``start`` to ``end`` that a listed set covers; the last day covered (None when no
    set covers any). Stops at the first gap, so a quarter the page does not link is walked rather than skipped."""
    sec13f_sets._clear_scratch(ctx, SUB, LABEL)
    weekdays = sum(1 for k in range((end - start).days + 1) if (start + timedelta(days=k)).weekday() < 5)
    if weekdays < sec13f_sets.MIN_WEEKDAYS:
        ctx.notes.append(f"a backfill of {weekdays} weekdays reads the forms; the data sets fill a range of "
                         f"{sec13f_sets.MIN_WEEKDAYS} weekdays or more")
        return None
    pacer = sec.Pacer(ctx)
    covered = None
    for name, (first, last, url) in sorted(sec13f_sets.listed(ctx, pacer, index_url(), SUFFIX, LABEL).items(),
                                           key=lambda kv: kv[1][0]):
        lo, hi = max(first, start), min(last, end)
        if lo > hi:
            continue
        if covered is not None and lo > covered + timedelta(days=1):
            ctx.notes.append(f"{LABEL} data sets: none covers {covered + timedelta(days=1)} to "
                             f"{lo - timedelta(days=1)}; the forms walk takes it")
            break
        path, replaced = sec13f_sets.download(ctx, pacer, name, url, SUB, LABEL)
        if replaced:
            ctx.notes.append(f"{LABEL} data set {name}: changed since it was read, "
                             f"{sec13f_sets._reopen(feed, name, lo, hi)} days reopened")
        written = _fill_one(ctx, feed, path, name, lo, hi, schema, open_window=last >= calendar.today_ny())
        ctx.notes.append(f"{LABEL} data set {name}: {written} days written, {lo} to {hi}")
        covered = hi if covered is None else max(covered, hi)
    return covered
