"""``fundamentals/tables``: no fetch; the served tables and views of ``fundamentals.duckdb`` from the sources' staging
tables, and the build-issue rules (``ISSUE_KINDS``), whose rows go to ``ops.sqlite`` once the new file is live.

``build_kind`` builds every dataset of the kind in ``DATASETS`` order, so the sources' staging tables exist when this
one builds; it is registered last. A source not in the registry (a test's) gets empty staging tables here.
"""
from __future__ import annotations

from datetime import date

import duckdb

from ....core import calendar
from ... import ops
from .. import base
from .. import dirty as dirty_set
from . import (KIND, common, companyfacts, earnings, estimates, facts, filings, fsds, transcripts,
               uw_earnings, wrds_fund)

NAME = "fundamentals/tables"

COMPANY = [
    ("ticker", "TEXT PRIMARY KEY"), ("cik", "TEXT NOT NULL"), ("gvkey", "TEXT"), ("name", "TEXT"),
    ("exchange", "TEXT"), ("gsector", "TEXT"), ("ggroup", "TEXT"), ("gind", "TEXT"), ("gsubind", "TEXT"),
    ("sector", "TEXT"), ("industry", "TEXT"), ("spine_as_of", "DATE"),
    # From the SEC facts: the filer's name and kind, the newest re-read, and its newest statement (filed, period).
    ("entity_name", "TEXT"), ("is_us", "BOOLEAN"), ("non_usd_units", "TEXT"), ("us_gaap", "BOOLEAN"),
    ("facts_as_of", "DATE"), ("facts_fetched_at", "DOUBLE"), ("newest_filed", "DATE"), ("newest_period_end", "DATE"),
    # The day the company's SEC filing list was last read: "no 8-K event" means something only after it (F2).
    ("submissions_as_of", "DATE"),
    # The filing list's company header (``filings.HEADER``, #645 G2; fundamentals 3): SEC SIC, state of
    # incorporation (EDGAR's code), and the business address's state and ISO country.
    *[(c, t) for c, t in filings.HEADER if c not in ("cik", "source_as_of", "fetched_at", "run_id")],
    ("source", "TEXT"), ("run_id", "TEXT"),
]
HEADER_COLS = [c for c, _ in filings.HEADER if c not in ("cik", "source_as_of", "fetched_at", "run_id")]
FACT = [(c, "TEXT NOT NULL" if c in ("cik", "field", "period_type", "source") else
         "DATE NOT NULL" if c == "period_end" else "DOUBLE NOT NULL" if c == "value" else
         "BOOLEAN NOT NULL" if c in ("derived", "preliminary") else t) for c, t in facts.FACT]

# One row per filing and 8-K item (``kind`` ``item``; a filing without items has one row with ``item`` NULL), and
# one per document text (``kind`` ``document``: ``item`` ``business`` or ``proxy``, with ``text`` and ``quality``).
FILING_ITEM = [("cik", "TEXT NOT NULL"), ("accession", "TEXT NOT NULL"), ("form", "TEXT"), ("filing_date", "DATE"),
               ("report_date", "DATE"), ("primary_document", "TEXT"), ("kind", "TEXT NOT NULL"), ("item", "TEXT"),
               ("text", "TEXT"), ("chars", "INTEGER"), ("quality", "TEXT"), ("source", "TEXT NOT NULL"),
               ("source_as_of", "DATE"), ("fetched_at", "DOUBLE"), ("run_id", "TEXT")]

# One row per report: a coming one from a calendar (``confirmed`` false), or one that happened (an 8-K with item 2.02,
# ``confirmed`` true, its accession kept).
EARNINGS_EVENT = [("cik", "TEXT NOT NULL"), ("ticker", "TEXT"), ("report_date", "DATE NOT NULL"),
                  ("report_time", "TEXT"), ("fiscal_period_end", "DATE"), ("eps_estimate", "DOUBLE"),
                  ("currency", "TEXT"), ("confirmed", "BOOLEAN NOT NULL"), ("accession", "TEXT"),
                  ("source", "TEXT NOT NULL"), ("source_as_of", "DATE"), ("fetched_at", "DOUBLE"), ("run_id", "TEXT")]

# One row per company and reported quarter: the EPS the company itself reported, as Unusual Whales serves it
# (``uw/earnings``), with UW's own consensus and surprise of that quarter. **Not** a ``fundamental_fact`` row: it has
# no filing behind it, and ``fundamental_fact`` is the SEC's values with the filing each came from. Its basis is
# usually the company's adjusted (non-GAAP) figure, so it is never comparable with ``eps_diluted`` without saying so
# -- which is why ``source`` is on the row (avo, 5 Nov 2026).
EARNINGS_REPORTED = [("cik", "TEXT NOT NULL"), ("ticker", "TEXT"), ("period_end", "DATE NOT NULL"),
                     ("report_date", "DATE"), ("eps_reported", "DOUBLE NOT NULL"), ("eps_estimate", "DOUBLE"),
                     ("surprise_pct", "DOUBLE"), ("source", "TEXT NOT NULL"), ("source_as_of", "DATE"),
                     ("fetched_at", "DOUBLE"), ("run_id", "TEXT")]

# One row per company and fiscal quarter: the day the quarter's results were first made public. Compustat's ``rdq``
# (``wrds/fundq``) wherever it has one (F3, and avo's "WRDS'e gecelim tamamen" of 20 Nov 2026); UW's report day
# (``uw/earnings``) for a quarter Compustat has no date for within ``wrds_fund.PERIOD_DAYS`` of the same period end.
# ``source`` says which on every row, and ``gvkey`` is the Compustat company the date came from (NULL on a UW row).
# Measured on homesrv, 20 Nov 2026 (#1181): the two agree to the day on 92.5% of 128,710 matched quarters, and
# ``rdq`` is empty on 9.12% of the linked companies' quarters.
# ``fiscal_year`` and ``fiscal_quarter`` are Compustat's ``fyearq`` and ``fqtr``: the company's own label for the
# quarter, NULL on a UW row (UW serves period ends, not fiscal labels). They are here because Alpha Vantage asks
# for an earnings call **by fiscal quarter** (``YYYYQn``, measured 21 Nov 2026, #1214) and no date arithmetic can
# produce that label for a company whose fiscal year does not end in December.
EARNINGS_REPORT_HISTORY = [("cik", "TEXT NOT NULL"), ("period_end", "DATE NOT NULL"),
                           ("report_date", "DATE NOT NULL"), ("gvkey", "TEXT"), ("source", "TEXT NOT NULL"),
                           ("source_as_of", "DATE"), ("fetched_at", "DOUBLE"), ("run_id", "TEXT"),
                           ("fiscal_year", "INTEGER"), ("fiscal_quarter", "INTEGER")]

# One row per turn of an earnings call, in the order spoken (``alphavantage/earnings_transcripts``, #1214). The
# provider sends no prepared-remarks / Q&A field, so the halves are not stored apart: the first analyst turn is
# the boundary and the reader's caller splits there
# (``module_earnings_radar/rules/transcript_split.py``). ``provider_sentiment`` is the provider's own per-turn
# number, kept as it came and read by nothing: R8 measures Loughran-McDonald rates over our own tokenizer.
EARNINGS_TRANSCRIPT = [("ticker", "TEXT NOT NULL"), ("quarter", "TEXT NOT NULL"), ("seq", "INTEGER NOT NULL"),
                       ("speaker", "TEXT"), ("title", "TEXT"), ("content", "TEXT"),
                       ("provider_sentiment", "DOUBLE"), ("source", "TEXT NOT NULL"), ("source_as_of", "DATE"),
                       ("fetched_at", "DOUBLE"), ("run_id", "TEXT")]

# One row per event Audit Analytics recorded for a company of ours (``wrds/audit``, #1181 step C): a financial
# restatement, a late-filing notice (the NT forms) or a director's or officer's change. ``kind`` says which,
# ``event_key`` is the feed's own key (an officer change names one person per row: ``person_key``, an opaque number,
# never a name). ``flags`` holds the labels of the feed's 0/1 columns that are 1 (``wrds_fund.AUDIT.SERVED``; empty
# for a late filing), ``action`` the officer feed's own word (Appointed, Resigned, ...). ``period_start`` and
# ``period_end`` are the restated span, or the period a late filing is late for. **A history, not a fresh flag:**
# the feeds ended about two months before the day they were read (measured 20 Nov 2026), so the newest events of a
# company are in ``filing_item`` (the 8-K's own item) first.
AUDIT_FLAG = [("cik", "TEXT NOT NULL"), ("kind", "TEXT NOT NULL"), ("event_key", "TEXT NOT NULL"),
              ("person_key", "TEXT"), ("file_date", "DATE NOT NULL"), ("form", "TEXT"), ("effective_date", "DATE"),
              ("period_start", "DATE"), ("period_end", "DATE"), ("action", "TEXT"), ("flags", "VARCHAR[]"),
              ("source", "TEXT NOT NULL"), ("source_as_of", "DATE"), ("fetched_at", "DOUBLE"), ("run_id", "TEXT")]

# One row per Compustat value that changed between two monthly snapshots, for a period both held (``wrds_fund``);
# ``cik`` NULL when the gvkey is no longer a Tier A company's.
REVISION = [("cik", "TEXT"), ("gvkey", "TEXT NOT NULL"), ("source", "TEXT NOT NULL"), ("datadate", "DATE NOT NULL"),
            ("item", "TEXT NOT NULL"), ("old_value", "DOUBLE"), ("new_value", "DOUBLE"), ("old_as_of", "DATE NOT NULL"),
            ("new_as_of", "DATE NOT NULL"), ("run_id", "TEXT")]

# Revenue by segment, product or country: the annual report's inline XBRL (``sec/filings``, its newest year) and
# Compustat's segments (``wrds/segments``). ``document_date`` is the date of the document a value came from (the
# SEC filing date; Compustat's ``srcdate``). ``report_tables`` (fundamentals 4): the tables of the filing, as role ids
# of its definition linkbase, in which the row's member stands on the row's axis (``linkbase.py``); NULL for
# Compustat's rows and for a filing with no linkbase stored.
SEGMENT_REVENUE = [("cik", "TEXT NOT NULL"), ("period_end", "DATE NOT NULL"), ("period_start", "DATE"),
                   ("axis", "TEXT NOT NULL"), ("segment", "TEXT"), ("member", "TEXT"), ("concept", "TEXT"),
                   ("value", "DOUBLE NOT NULL"), ("unit", "TEXT"), ("accession", "TEXT"), ("form", "TEXT"),
                   ("document_date", "DATE"), ("source", "TEXT NOT NULL"), ("source_as_of", "DATE"),
                   ("fetched_at", "DOUBLE"), ("run_id", "TEXT"), ("report_tables", "VARCHAR[]")]
# A major customer's share: the annual report's ConcentrationRiskPercentage1, or Compustat's customer sales over the
# SEC's revenue of that year (``share`` NULL when there is none to divide by, or the ratio is above 1).
CUSTOMER_SHARE = [("cik", "TEXT NOT NULL"), ("period_end", "DATE NOT NULL"), ("customer", "TEXT"),
                  ("member", "TEXT"), ("benchmark", "TEXT"), ("share", "DOUBLE"), ("sales", "DOUBLE"),
                  ("customer_type", "TEXT"), ("accession", "TEXT"), ("form", "TEXT"), ("document_date", "DATE"),
                  ("source", "TEXT NOT NULL"), ("source_as_of", "DATE"), ("fetched_at", "DOUBLE"), ("run_id", "TEXT")]

# The newest analyst estimates per company, ticker and period (``estimates.py``; the snapshots stay in Parquet).
ESTIMATE = [(c, "TEXT NOT NULL" if c in ("cik", "ticker", "period") else t) for c, t in estimates.schema()] + [
    ("source", "TEXT NOT NULL")]

# Each company's ``PEER_K`` nearest companies on the GICS ladder (``PEER_LEVELS``, narrowest first), the closest
# newest annual revenue first within a rung (a company without one last); ``level`` the narrowest level they share.
PEER_GROUP = [("cik", "TEXT NOT NULL"), ("ticker", "TEXT NOT NULL"), ("peer_cik", "TEXT NOT NULL"),
              ("peer_ticker", "TEXT NOT NULL"), ("rank", "INTEGER NOT NULL"), ("level", "TEXT NOT NULL"),
              ("revenue", "DOUBLE"), ("peer_revenue", "DOUBLE"), ("run_id", "TEXT")]
PEER_K = 10
PEER_LEVELS = ("gsubind", "gind", "ggroup", "gsector")

SCHEMAS: dict[str, list[tuple[str, str]]] = {
    "fundamental_company": COMPANY,
    "fundamental_fact": FACT,
    "filing_item": FILING_ITEM,
    "earnings_event": EARNINGS_EVENT,
    "earnings_reported": EARNINGS_REPORTED,
    "earnings_report_history": EARNINGS_REPORT_HISTORY,
    "earnings_transcript": EARNINGS_TRANSCRIPT,
    "audit_flag": AUDIT_FLAG,
    "fundamental_revision": REVISION,
    "segment_revenue": SEGMENT_REVENUE,
    "customer_share": CUSTOMER_SHARE,
    "estimate_latest": ESTIMATE,
    "peer_group": PEER_GROUP,
}
TABLE_CHECKS: dict[str, list[str]] = {
    # Every row is annual or quarterly, and a non-preliminary row carries its accession and filed date.
    "fundamental_fact": ["CHECK (period_type IN ('annual', 'quarterly'))",
                         "CHECK (preliminary OR (accn IS NOT NULL AND filed IS NOT NULL))"],
    # Only ``item`` rows since #858: this layer cuts no document any more, so the ``document`` rows and the CHECK
    # that refused ``ok`` at the Value Chain board's cap went with the cut. The ``kind`` column stays, because the
    # readers filter on it and a served table keeps its shape. The text is ``filing_section`` (``sec/forms``).
    "filing_item": ["CHECK (kind = 'item')"],
    "segment_revenue": ["CHECK (axis IN ('business', 'product', 'geographic'))"],
    "audit_flag": [f"CHECK (kind IN {wrds_fund.AUDIT.KINDS})"],
    "customer_share": ["CHECK (share IS NULL OR (share >= 0 AND share <= 1))"],
}
TABLES = tuple(SCHEMAS)
VIEWS = ("fundamental_quarter", "edgar_financials", "sec_submissions", "earnings_calendar", "estimate_snapshots")

# The sources' staging tables, created empty here when a source is not registered (a test's registry).
STAGED: dict[str, list[tuple[str, str]]] = {
    companyfacts.STAGING["facts"]: facts.FACT,
    companyfacts.STAGING["company"]: facts.COMPANY,
    filings.STAGING["filings"]: filings.FILING,
    earnings.STAGING: earnings.SCHEMA,
    transcripts.STAGING: transcripts.SCHEMA,
    **{d.revision_staging: wrds_fund.REVISION for d in wrds_fund.DATASETS if d.revisions},
    wrds_fund.FUNDQ.staging: wrds_fund.FUNDQ.schema + wrds_fund.PROVENANCE,
    uw_earnings.STAGING: uw_earnings.SCHEMA,
    estimates.STAGING: estimates.schema(),
    fsds.STAGING: fsds.SCHEMA,
    filings.STAGING["segments"]: filings.SEGMENT,
    filings.STAGING["customers"]: filings.CUSTOMER,
    filings.STAGING["header"]: filings.HEADER,
    **{wrds_fund.SEGMENTS.STAGING[p]: schema + wrds_fund.PROVENANCE
       for p, (schema, _, _, _) in wrds_fund.SEGMENTS.PARTS.items()},
    **{wrds_fund.AUDIT.STAGING[p]: schema + wrds_fund.PROVENANCE
       for p, (_, schema, _) in wrds_fund.AUDIT.PARTS.items()},
}

# The named problems, one kind each; ``key`` is the CIK unless the kind says otherwise.
ISSUE_KINDS: dict[str, str] = {
    "source_never_run": "a source this build needs has no data yet (key: the dataset)",
    "statement_older_than_120_days": ("a Tier A quarterly filer whose newest statement was filed more than 120 days "
                                      "before the build's as-of"),
    "annual_statement_older_than_400_days": ("a Tier A annual-only filer (20-F, 40-F) whose newest statement was "
                                             "filed more than 400 days before the build's as-of; also a filer that "
                                             "moved to IFRS, whose last us-gaap statement is years old (TM: 2020)"),
    "no_statement": "a Tier A company with no SEC statement fact (not read yet, or no XBRL facts at the SEC)",
    "no_us_gaap_facts": "a Tier A company whose statements are not read from us-gaap (an IFRS filer or mover, read from ifrs-full)",
}


def create(con: duckdb.DuckDBPyConnection, table: str) -> None:
    cols = [f'"{c}" {t}' for c, t in SCHEMAS[table]] + TABLE_CHECKS.get(table, [])
    con.execute(f"CREATE TABLE {table} ({', '.join(cols)})")


def _plain(table: str) -> list[tuple[str, str]]:
    return [(c, t.replace(" PRIMARY KEY", "").replace(" NOT NULL", "")) for c, t in SCHEMAS[table]]


def load(con: duckdb.DuckDBPyConnection, table: str, rows: list[dict]) -> int:
    """Create served ``table`` and insert Python ``rows`` into it (typed through ``base.rows_table``)."""
    create(con, table)
    if rows:
        base.rows_table(con, f"_load_{table}", _plain(table), rows)
        con.execute(f"INSERT INTO {table} SELECT * FROM _load_{table}")
        con.execute(f"DROP TABLE _load_{table}")
    return common.count(con, table)


# ── #446: a build on the live copy ───────────────────────────────────────────────────────────────────────────
# The served tables are then already there, with every company's rows. A company of ``common.DIRTY_TABLE`` has its
# rows deleted and reloaded from its staged partitions (the keyed sources stage only those companies); a part of a
# table that comes from a whole-reloaded source (Alpha Vantage, UW earnings, WRDS, Yahoo) is deleted and reloaded
# whole; a table made from the other served tables (companies, peers) and the views are made again.

DIRTY = f"(SELECT cik FROM {common.DIRTY_TABLE})"


def _clear(con: duckdb.DuckDBPyConnection, table: str, ciks, *parts: str) -> None:
    """Make ``table`` ready to take this build's rows: created empty on a whole build (``ciks`` None); on the live
    copy, each of ``parts`` deleted (a SQL condition; ``"dirty"`` the dirty companies' rows, any source)."""
    if ciks is None:
        create(con, table)
        return
    for part in parts:
        con.execute(f"DELETE FROM {table} WHERE " + (f"cik IN {DIRTY}" if part == "dirty" else part))


def _names(table: str) -> str:
    return ", ".join(f'"{c}"' for c, _ in SCHEMAS[table])


def _ensure_staging(con: duckdb.DuckDBPyConnection) -> None:
    for name, schema in STAGED.items():
        cols = ", ".join(f'"{c}" {t}' for c, t in schema)
        con.execute(f"CREATE TEMP TABLE IF NOT EXISTS {name} ({cols})")


# ── The served tables ─────────────────────────────────────────────────────────────────────────────────────────

def _facts(con: duckdb.DuckDBPyConnection, ciks=None) -> int:
    """``fundamental_fact``: the SEC facts, one row per (company, field, period type, period end), and the periods
    a company's predecessor CIK filed (#749).

    The ``preliminary`` column and its CHECK stay: they are the fact table's shape, and changing it would bump the
    serving version and rebuild every file. Nothing writes a preliminary row since ``uw/financials`` went (#659)."""
    _clear(con, "fundamental_fact", ciks, "dirty")
    names = _names("fundamental_fact")
    con.execute(f"""INSERT INTO fundamental_fact SELECT {names} FROM {companyfacts.STAGING['facts']}
                    QUALIFY row_number() OVER (PARTITION BY cik, field, period_type, period_end
                                               ORDER BY filed DESC, accn DESC) = 1""")
    _predecessor_facts(con, ciks)
    return common.count(con, "fundamental_fact")


def _predecessor_facts(con: duckdb.DuckDBPyConnection, ciks=None) -> int:
    """The periods a company filed under an older CIK, served under the company's own CIK (#749); how many rows.

    A reorganisation or a redomicile gives the new registrant a new CIK, and the SEC serves everything filed before
    it only under the old one (Exxon Mobil: 20 annual years under CIK 34088, none under the successor). The
    predecessor's file is fetched as a company of its own (``common.with_predecessors``) and its rows stay
    under its own CIK as well; this adds a copy under the successor.

    **The guard is per field and period type**: only a period that ends *before* the successor's earliest period of
    that field and period type is taken, and every period of a field the successor never filed. So a restatement
    never replaces what the company itself reported, and the two never give one period two rows. The copied row
    keeps the predecessor's own ``accn``, ``form`` and ``filed``, which say which registrant filed it.

    A dirty build reaches here only for a dirty successor, because a changed predecessor partition marks its
    successor dirty (``common.dirty_ciks``), whose rows were just cleared and rebuilt above.
    """
    pairs = common.predecessor_ciks()
    if ciks is not None:
        pairs = {c: p for c, p in pairs.items() if c in ciks}
    rows = [{"predecessor_cik": pre, "cik": cik} for cik, pres in pairs.items() for pre in pres]
    if not rows:
        return 0
    base.rows_table(con, "_predecessor", [("predecessor_cik", "TEXT"), ("cik", "TEXT")], rows)
    cols = ", ".join(f'f."{c}"' for c, _ in SCHEMAS["fundamental_fact"] if c != "cik")
    before = common.count(con, "fundamental_fact")
    con.execute(f"""
        INSERT INTO fundamental_fact
        SELECT m.cik, {cols}
        FROM (SELECT * FROM {companyfacts.STAGING['facts']}
              QUALIFY row_number() OVER (PARTITION BY cik, field, period_type, period_end
                                         ORDER BY filed DESC, accn DESC) = 1) f
        JOIN _predecessor m ON m.predecessor_cik = f.cik
        LEFT JOIN (SELECT cik, field, period_type, min(period_end) AS earliest FROM fundamental_fact
                   GROUP BY cik, field, period_type) s
               ON s.cik = m.cik AND s.field = f.field AND s.period_type = f.period_type
        WHERE s.earliest IS NULL OR f.period_end < s.earliest""")
    con.execute("DROP TABLE _predecessor")
    return common.count(con, "fundamental_fact") - before


def _filing_items(con: duckdb.DuckDBPyConnection, ciks=None) -> int:
    """``filing_item``: every listed filing and 8-K item. The document texts went with the cut (#858): they are
    ``filing_section`` of ``sec/forms``, the one cutter."""
    _clear(con, "filing_item", ciks, "dirty")
    con.execute(f"""
        INSERT INTO filing_item
        SELECT cik, accession, form, filing_date, report_date, primary_document, 'item', item, NULL, NULL, NULL,
               '{filings.NAME}', source_as_of, fetched_at, run_id FROM {filings.STAGING['filings']}""")
    _predecessor_filings(con, ciks)
    return common.count(con, "filing_item")


def _predecessor_filings(con: duckdb.DuckDBPyConnection, ciks=None) -> int:
    """A predecessor's filings, listed under its successor's CIK too (#749); how many rows.

    As ``_predecessor_facts``: only a filing dated before the successor's earliest filing of its own, so the two
    lists never overlap, and the predecessor's rows stay under its own CIK as well. A dirty build reaches here only
    for a dirty successor (``common.dirty_ciks``), whose rows were just cleared.
    """
    pairs = common.predecessor_ciks()
    if ciks is not None:
        pairs = {c: p for c, p in pairs.items() if c in ciks}
    rows = [{"predecessor_cik": pre, "cik": cik} for cik, pres in pairs.items() for pre in pres]
    if not rows:
        return 0
    base.rows_table(con, "_predecessor", [("predecessor_cik", "TEXT"), ("cik", "TEXT")], rows)
    cols = ", ".join(f'f."{c}"' for c, _ in SCHEMAS["filing_item"] if c != "cik")
    before = common.count(con, "filing_item")
    con.execute(f"""
        INSERT INTO filing_item
        SELECT m.cik, {cols}
        FROM (SELECT * FROM filing_item WHERE cik IN (SELECT predecessor_cik FROM _predecessor)) f
        JOIN _predecessor m ON m.predecessor_cik = f.cik
        LEFT JOIN (SELECT cik, min(filing_date) AS earliest FROM filing_item GROUP BY cik) s ON s.cik = m.cik
        WHERE s.earliest IS NULL OR f.filing_date < s.earliest""")
    con.execute("DROP TABLE _predecessor")
    return common.count(con, "filing_item") - before


EARNINGS_8K_ITEM = "2.02"


def _earnings(con: duckdb.DuckDBPyConnection, ciks=None) -> int:
    """``earnings_event``: Alpha Vantage's coming reports of the Tier A tickers, and the 8-K item 2.02 reports."""
    _clear(con, "earnings_event", ciks, f"source = '{earnings.NAME}'",
           f"source = '{filings.NAME}' AND cik IN {DIRTY}")
    con.execute(f"""
        INSERT INTO earnings_event
        SELECT c.cik, a.symbol, a.report_date, a.time_of_day, a.fiscal_date_ending, a.estimate, a.currency, false,
               NULL, '{earnings.NAME}', a.source_as_of, a.fetched_at, a.run_id
        FROM {earnings.STAGING} a JOIN (SELECT DISTINCT ticker, cik FROM fundamental_company) c ON c.ticker = a.symbol
        UNION ALL
        SELECT DISTINCT f.cik, NULL, f.filing_date, NULL, NULL, NULL, NULL, true, f.accession, '{filings.NAME}',
               f.source_as_of, f.fetched_at, f.run_id
        FROM {filings.STAGING['filings']} f
        WHERE f.form = '8-K' AND f.item = '{EARNINGS_8K_ITEM}' AND f.filing_date IS NOT NULL""")
    return common.count(con, "earnings_event")


def _earnings_reported(con: duckdb.DuckDBPyConnection, ciks=None) -> int:
    """``earnings_reported``: UW's reported EPS per company and quarter, its consensus and its surprise as served."""
    _clear(con, "earnings_reported", ciks, "true")
    con.execute(f"""
        INSERT INTO earnings_reported
        SELECT u.cik, u.ticker, u.period_end, u.report_date, u.eps_reported, u.eps_estimate, u.surprise_pct,
               u.source, u.source_as_of, u.fetched_at, u.run_id
        FROM {uw_earnings.STAGING} u
        WHERE u.cik IN (SELECT cik FROM fundamental_company) AND u.eps_reported IS NOT NULL""")
    return common.count(con, "earnings_reported")


def _report_history(con: duckdb.DuckDBPyConnection, ciks=None) -> int:
    """``earnings_report_history``: Compustat's ``rdq`` per company and quarter, and UW's report day for a quarter
    Compustat has no date for. Both sources are reloaded whole by every build, so the table is too.

    Two Compustat companies of one CIK that hold the same quarter: the earlier ``rdq`` is kept (the day the
    results were first public), then the lower gvkey. A UW quarter is a fill only when no Compustat row of that
    company ends within ``wrds_fund.PERIOD_DAYS`` of it, because the two write a 52/53-week year's end differently."""
    _clear(con, "earnings_report_history", ciks, "true")
    con.execute(f"""
        INSERT INTO earnings_report_history
        WITH w AS (
            SELECT c.cik, s.datadate AS period_end, s.rdq AS report_date, s.gvkey,
                   '{wrds_fund.FUNDQ.name}' AS source, s.source_as_of, s.fetched_at, s.run_id,
                   s.fyearq AS fiscal_year, s.fqtr AS fiscal_quarter
            FROM {wrds_fund.FUNDQ.staging} s JOIN {wrds_fund._links()} c ON c.gvkey = s.gvkey
            WHERE s.rdq IS NOT NULL AND s.datadate IS NOT NULL
            QUALIFY row_number() OVER (PARTITION BY c.cik, s.datadate ORDER BY s.rdq, s.gvkey) = 1)
        SELECT * FROM w
        UNION ALL
        SELECT u.cik, u.period_end, u.report_date, NULL, u.source, u.source_as_of, u.fetched_at, u.run_id,
               NULL::INTEGER, NULL::INTEGER
        FROM {uw_earnings.STAGING} u
        WHERE u.cik IN (SELECT cik FROM fundamental_company) AND u.report_date IS NOT NULL
          AND u.period_end IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM w WHERE w.cik = u.cik AND abs(date_diff('day', w.period_end, u.period_end))
                          <= {wrds_fund.PERIOD_DAYS})""")
    return common.count(con, "earnings_report_history")


def _transcripts(con: duckdb.DuckDBPyConnection) -> int:
    """``earnings_transcript``: every stored earnings-call turn, in the order spoken.

    Keyed by ticker and quarter rather than by company, so this one is not cleared per dirty CIK: the staging
    holds every call on disk and the table is made again whole from it. One (ticker, quarter) answered twice --
    a re-fetch of a call the provider revised -- keeps the newer fetch, turn by turn.
    """
    con.execute("DELETE FROM earnings_transcript")
    con.execute(f"""
        INSERT INTO earnings_transcript
        SELECT ticker, quarter, seq, speaker, title, content, provider_sentiment,
               '{transcripts.NAME}' AS source, source_as_of, fetched_at, run_id
        FROM {transcripts.STAGING}
        WHERE ticker IS NOT NULL AND quarter IS NOT NULL AND seq IS NOT NULL
        QUALIFY row_number() OVER (PARTITION BY ticker, quarter, seq ORDER BY fetched_at DESC) = 1""")
    return common.count(con, "earnings_transcript")


def _audit_flags(con: duckdb.DuckDBPyConnection, ciks=None) -> int:
    """``audit_flag``: the three Audit Analytics feeds' rows of our companies, one row per event. The newest
    snapshot is reloaded whole by every build, so the table is made again whole too."""
    _clear(con, "audit_flag", ciks, "true")
    con.execute("INSERT INTO audit_flag " + " UNION ALL ".join(
        wrds_fund.AUDIT.served_sql(part) for part in wrds_fund.AUDIT.SERVED))
    return common.count(con, "audit_flag")


def _revisions(con: duckdb.DuckDBPyConnection, ciks=None) -> int:
    """``fundamental_revision``: the Compustat revisions of ``fundq`` and ``funda``, with the company's CIK."""
    _clear(con, "fundamental_revision", ciks, "true")
    union = " UNION ALL ".join(f"SELECT * FROM {d.revision_staging}" for d in wrds_fund.DATASETS if d.revisions)
    con.execute(f"""
        INSERT INTO fundamental_revision
        SELECT c.cik, r.gvkey, r.source, r.datadate, r.item, r.old_value, r.new_value, r.old_as_of, r.new_as_of,
               r.run_id
        FROM ({union}) r LEFT JOIN {_links()} c ON c.gvkey = r.gvkey""")
    return common.count(con, "fundamental_revision")


def _segments(con: duckdb.DuckDBPyConnection, ciks=None) -> int:
    """``segment_revenue``: the annual report's segments and Compustat's (business and geographic)."""
    seg = wrds_fund.SEGMENTS
    _clear(con, "segment_revenue", ciks, f"source = '{filings.NAME}' AND cik IN {DIRTY}", f"source <> '{filings.NAME}'")
    axis = " ".join(f"WHEN '{t}' THEN '{a}'" for t, a in sorted(wrds_fund.SEGMENT_TYPES.items()))
    con.execute(f"""
        INSERT INTO segment_revenue
        SELECT cik, period_end, period_start, axis, segment, member, concept, value, unit, accession, form,
               filing_date, '{filings.NAME}', source_as_of, fetched_at, run_id, report_tables
        FROM {filings.STAGING['segments']} WHERE period_end IS NOT NULL AND value IS NOT NULL
        UNION ALL
        SELECT c.cik, w.datadate, NULL, CASE w.stype {axis} END, w.snms, w.stype || ' ' || w.sid, 'sales',
               w.sales * {wrds_fund.MILLION}, 'USD', NULL, NULL, w.srcdate, '{seg.name}', w.source_as_of,
               w.fetched_at, w.run_id, NULL
        FROM {seg.STAGING['wrds_segmerged']} w JOIN {_links()} c ON c.gvkey = w.gvkey
        WHERE w.datadate IS NOT NULL AND w.sales IS NOT NULL AND w.stype IN
              ({", ".join(f"'{t}'" for t in sorted(wrds_fund.SEGMENT_TYPES))})""")
    return common.count(con, "segment_revenue")


def _customers(con: duckdb.DuckDBPyConnection, ciks=None) -> int:
    """``customer_share``: the annual report's major customers, and Compustat's customers over the SEC's revenue."""
    seg = wrds_fund.SEGMENTS
    _clear(con, "customer_share", ciks, f"source = '{filings.NAME}' AND cik IN {DIRTY}", f"source <> '{filings.NAME}'")
    con.execute(f"""
        INSERT INTO customer_share
        SELECT cik, period_end, customer, member, benchmark, share, NULL, NULL, accession, form, filing_date,
               '{filings.NAME}', source_as_of, fetched_at, run_id
        FROM {filings.STAGING['customers']} WHERE period_end IS NOT NULL
        UNION ALL
        SELECT c.cik, w.datadate, w.cnms, NULL, 'revenue',
               CASE WHEN r.value > 0 AND w.salecs * {wrds_fund.MILLION} / r.value BETWEEN 0 AND 1
                    THEN w.salecs * {wrds_fund.MILLION} / r.value END,
               w.salecs * {wrds_fund.MILLION}, w.ctype, NULL, NULL, w.srcdate, '{seg.name}', w.source_as_of,
               w.fetched_at, w.run_id
        FROM {seg.STAGING['seg_customer']} w JOIN {_links()} c ON c.gvkey = w.gvkey
        LEFT JOIN (SELECT cik, period_end, value FROM fundamental_fact
                   WHERE field = 'revenue' AND period_type = '{facts.ANNUAL}' AND NOT preliminary) r
          ON r.cik = c.cik AND abs(date_diff('day', r.period_end, w.datadate)) <= {wrds_fund.PERIOD_DAYS}
        WHERE w.datadate IS NOT NULL""")
    return common.count(con, "customer_share")


def _estimates(con: duckdb.DuckDBPyConnection, ciks=None) -> int:
    """``estimate_latest``: each company, ticker and period's newest estimates."""
    _clear(con, "estimate_latest", ciks, "true")
    names = ", ".join(f'"{c}"' for c, _ in estimates.schema())
    con.execute(f"""
        INSERT INTO estimate_latest SELECT {names}, '{estimates.NAME}' FROM {estimates.STAGING}
        WHERE cik IS NOT NULL AND ticker IS NOT NULL AND period IS NOT NULL
        QUALIFY row_number() OVER (PARTITION BY cik, ticker, period ORDER BY source_as_of DESC, fetched_at DESC) = 1""")
    return common.count(con, "estimate_latest")


def _estimate_view(con: duckdb.DuckDBPyConnection) -> None:
    # The old ``estimate_snapshots`` shape (Earnings Radar's D14), one row per ticker and period: the newest.
    values = ", ".join(f'"{c}"' for c in estimates.value_columns())
    con.execute(f"""
        CREATE OR REPLACE VIEW estimate_snapshots AS
        SELECT ticker, source_as_of AS session_date, period, {values}, CAST(payload AS JSON) AS payload,
               'yfinance' AS source, fetched_at, run_id, NULL::DOUBLE AS run_at
        FROM estimate_latest""")


def _peers(con: duckdb.DuckDBPyConnection, k: int = PEER_K, run_id: str | None = None, ciks=None) -> int:
    """``peer_group`` from ``fundamental_company`` (GICS, one ticker per company: its shortest) and the newest
    annual SEC revenue in ``fundamental_fact``."""
    _clear(con, "peer_group", ciks, "true")
    level = " ".join(f"WHEN a.{c} = b.{c} THEN {i}" for i, c in enumerate(PEER_LEVELS))
    names = "CASE r.lvl " + " ".join(f"WHEN {i} THEN '{c}'" for i, c in enumerate(PEER_LEVELS)) + " END"
    con.execute(f"""
        WITH co AS (SELECT cik, list(ticker ORDER BY length(ticker), ticker)[1] AS ticker,
                           any_value(gsector) AS gsector, any_value(ggroup) AS ggroup, any_value(gind) AS gind,
                           any_value(gsubind) AS gsubind
                    FROM fundamental_company WHERE gsector IS NOT NULL GROUP BY cik),
             rev AS (SELECT cik, value FROM fundamental_fact
                     WHERE field = 'revenue' AND period_type = '{facts.ANNUAL}' AND NOT preliminary
                     QUALIFY row_number() OVER (PARTITION BY cik ORDER BY fiscal_year DESC, period_end DESC) = 1),
             c AS (SELECT co.*, rev.value AS revenue FROM co LEFT JOIN rev ON rev.cik = co.cik),
             pairs AS (SELECT a.cik, a.ticker, b.cik AS peer_cik, b.ticker AS peer_ticker, a.revenue,
                              b.revenue AS peer_revenue, CASE {level} END AS lvl,
                              CASE WHEN a.revenue > 0 AND b.revenue > 0 THEN abs(ln(b.revenue / a.revenue)) END AS dist
                       FROM c a JOIN c b ON a.gsector = b.gsector AND a.cik <> b.cik),
             r AS (SELECT *, row_number() OVER (PARTITION BY cik ORDER BY lvl, dist NULLS LAST, peer_ticker) AS rank
                   FROM pairs)
        INSERT INTO peer_group
        SELECT cik, ticker, peer_cik, peer_ticker, rank, {names}, revenue, peer_revenue, ?
        FROM r WHERE rank <= {int(k)}""", [run_id])
    return common.count(con, "peer_group")


def _links() -> str:
    return "(SELECT DISTINCT gvkey, cik FROM fundamental_company WHERE gvkey IS NOT NULL)"


def _companies(ctx: base.RunContext, con: duckdb.DuckDBPyConnection, rows: list[dict], ciks=None) -> int:
    """``fundamental_company``: the universe's tickers, each with its company's SEC columns.

    Made again whole every build (the universe and the spine's columns can change for any company). On the live
    copy, a clean company's SEC columns (read from the staging on a whole build) are its live row's own: its
    partitions did not change, so they are what a whole build would read."""
    sec_co = f"SELECT * FROM {companyfacts.STAGING['company']}"
    listed = f"SELECT cik, source_as_of FROM {filings.STAGING['filings']}"
    head = ", ".join(HEADER_COLS)
    headers = f"SELECT cik, source_as_of, {head} FROM {filings.STAGING['header']}"
    if ciks is not None:
        con.execute(f"""CREATE OR REPLACE TEMP TABLE _prev_company AS
                        SELECT cik, any_value(entity_name) AS entity_name, any_value(is_us) AS is_us,
                               any_value(non_usd_units) AS non_usd_units, any_value(us_gaap) AS us_gaap,
                               any_value(facts_as_of) AS facts_as_of, any_value(facts_fetched_at) AS fetched_at,
                               any_value(submissions_as_of) AS submissions_as_of,
                               {", ".join(f"any_value({c}) AS {c}" for c in HEADER_COLS)}
                        FROM fundamental_company WHERE cik NOT IN {DIRTY} GROUP BY cik""")
        cols = ", ".join(c for c, _ in facts.COMPANY)
        # A staged company row always carries ``facts_as_of`` (``companyfacts.to_parquet_all`` sets it from the raw
        # file's meta), so a clean company's live columns join exactly when its staged row would have.
        sec_co = (f"SELECT {cols} FROM {companyfacts.STAGING['company']} UNION ALL "
                  f"SELECT {cols.replace('run_id', 'NULL AS run_id')} FROM _prev_company WHERE facts_as_of IS NOT NULL "
                  "OR entity_name IS NOT NULL")
        listed += " UNION ALL SELECT cik, submissions_as_of FROM _prev_company WHERE submissions_as_of IS NOT NULL"
        headers += f" UNION ALL SELECT cik, submissions_as_of, {head} FROM _prev_company"
        con.execute("DROP TABLE fundamental_company")
    spine = [{"ticker": r["ticker"], "cik": common.norm_cik(r["cik"]), "gvkey": r.get("gvkey"), "name": r.get("name"),
              "exchange": r.get("exchange"), "gsector": r.get("gsector"), "ggroup": r.get("ggroup"),
              "gind": r.get("gind"), "gsubind": r.get("gsubind"), "sector": r.get("sector"),
              "industry": r.get("industry"), "spine_as_of": r.get("source_as_of")} for r in rows]
    base.rows_table(con, "_spine", [(c, t) for c, t in _plain("fundamental_company")][:12], spine)
    create(con, "fundamental_company")
    con.execute(f"""
        INSERT INTO fundamental_company
        SELECT s.*, c.entity_name, c.is_us, c.non_usd_units, c.us_gaap, c.facts_as_of, c.fetched_at,
               n.newest_filed, n.newest_period_end, l.submissions_as_of, {", ".join(f"h.{c}" for c in HEADER_COLS)},
               'spine/security', '{ctx.run_id}'
        FROM _spine s
        LEFT JOIN (SELECT * FROM ({sec_co})
                   QUALIFY row_number() OVER (PARTITION BY cik ORDER BY facts_as_of DESC) = 1) c ON c.cik = s.cik
        LEFT JOIN (SELECT cik, max(filed) AS newest_filed, max(period_end) AS newest_period_end
                   FROM fundamental_fact WHERE NOT preliminary GROUP BY cik) n ON n.cik = s.cik
        LEFT JOIN (SELECT cik, max(source_as_of) AS submissions_as_of FROM ({listed})
                   GROUP BY cik) l ON l.cik = s.cik
        LEFT JOIN (SELECT * FROM ({headers})
                   QUALIFY row_number() OVER (PARTITION BY cik ORDER BY source_as_of DESC NULLS LAST) = 1) h
               ON h.cik = s.cik""")
    con.execute("DROP TABLE _spine")
    con.execute("DROP TABLE IF EXISTS _prev_company")
    return common.count(con, "fundamental_company")


# ── The views (the old ``raw.duckdb`` shapes, D4) ─────────────────────────────────────────────────────────────

def _views(con: duckdb.DuckDBPyConnection) -> None:
    pivot = ", ".join(f"max(value) FILTER (WHERE field = '{f}') AS \"{f}\"" for f in facts.FIELDS)
    # One row per company and quarter, a column per field; ``filed`` the newest filing a value came from.
    con.execute(f"""
        CREATE OR REPLACE VIEW fundamental_quarter AS
        SELECT cik, period_end, min(period_start) AS period_start, {pivot},
               max(filed) AS filed, bool_or(preliminary) AS preliminary,
               string_agg(DISTINCT source, ',' ORDER BY source) AS source
        FROM fundamental_fact WHERE period_type = 'quarterly' GROUP BY cik, period_end""")
    # The old ``edgar_financials`` row per ticker: ``annual`` / ``quarterly`` as JSON lists of records (oldest
    # first) shaped as sec_edgar.extract_* writes them (a key per field, ``period_ends`` / ``filed`` / ``accn``
    # maps; ``fiscal_year`` or ``period``), and ``meta``. A field with no value has no key (the old rows held
    # None); ``total_debt`` is ``facts``'s sum of the filer's debt lines. The SEC's rows only: a preliminary row has no accession.
    def records(period_type: str, key: str, key_json: str) -> str:
        return f"""
            SELECT cik, {key} AS k, json_merge_patch(
                json_object({key_json}), json_group_object(field, value),
                json_object('period_ends', json_group_object(field, CAST(period_end AS VARCHAR)),
                            'filed', json_group_object(field, CAST(filed AS VARCHAR)),
                            'accn', json_group_object(field, accn))) AS rec
            FROM fundamental_fact WHERE period_type = '{period_type}' AND NOT preliminary GROUP BY cik, {key}"""
    con.execute(f"""
        CREATE OR REPLACE VIEW edgar_financials AS
        WITH a AS ({records('annual', 'fiscal_year', "'fiscal_year', CAST(fiscal_year AS VARCHAR)")}),
             q AS ({records('quarterly', 'period_end', "'period', CAST(period_end AS VARCHAR)")}),
             al AS (SELECT cik, to_json(list(rec ORDER BY k)) AS annual FROM a GROUP BY cik),
             ql AS (SELECT cik, to_json(list(rec ORDER BY k)) AS quarterly FROM q GROUP BY cik)
        SELECT c.ticker, coalesce(al.annual, '[]'::JSON) AS annual, coalesce(ql.quarterly, '[]'::JSON) AS quarterly,
               json_object('source', 'sec_edgar', 'entity_name', coalesce(c.entity_name, c.name, ''),
                           'is_us', coalesce(c.is_us, true), 'available', al.cik IS NOT NULL OR ql.cik IS NOT NULL,
                           'extract_version', {_extract_version()},
                           'non_usd_units', coalesce(string_split(c.non_usd_units, ','), [])) AS meta,
               c.facts_fetched_at AS fetched_at
        FROM fundamental_company c LEFT JOIN al ON al.cik = c.cik LEFT JOIN ql ON ql.cik = c.cik""")


def _submission_view(con: duckdb.DuckDBPyConnection) -> None:
    # The old ``sec_submissions`` row per filing: its 8-K items as one comma-separated text, as the SEC lists them.
    con.execute(f"""
        CREATE OR REPLACE VIEW sec_submissions AS
        SELECT cik, accession, any_value(form) AS form, any_value(filing_date) AS filing_date,
               any_value(primary_document) AS primary_document,
               string_agg(item, ',' ORDER BY item) AS items, '{filings.NAME}' AS source,
               max(fetched_at) AS fetched_at, any_value(run_id) AS run_id, NULL::DOUBLE AS run_at
        FROM filing_item WHERE kind = 'item' GROUP BY cik, accession""")


def _calendar_view(con: duckdb.DuckDBPyConnection) -> None:
    # The old ``earnings_calendar`` shape, one row per ticker and report; what this layer does not have is NULL.
    con.execute("""
        CREATE OR REPLACE VIEW earnings_calendar AS
        SELECT e.source_as_of AS session_date, e.report_date AS earnings_date, c.ticker,
               CASE lower(e.report_time) WHEN 'pre-market' THEN 'premarket' WHEN 'post-market' THEN 'afterhours'
                    ELSE e.report_time END AS report_session,
               c.sector, c.name AS full_name, NULL::DOUBLE AS marketcap, NULL::DOUBLE AS expected_move_perc,
               e.eps_estimate AS street_mean_est, NULL::DOUBLE AS pre_earnings_close,
               NULL::DOUBLE AS post_earnings_close, e.report_time, NULL::JSON AS item, e.source, e.fetched_at,
               e.run_id, NULL::DOUBLE AS run_at, e.confirmed, e.accession, e.fiscal_period_end
        FROM earnings_event e JOIN fundamental_company c ON c.cik = e.cik
        WHERE e.ticker IS NULL OR e.ticker = c.ticker""")


def _extract_version() -> int:
    from ....core.providers import sec_edgar
    return int(sec_edgar.EXTRACT_VERSION)


# ── Build issues ──────────────────────────────────────────────────────────────────────────────────────────────

def _issues(con: duckdb.DuckDBPyConnection, as_of: date) -> list[tuple[str, str, str]]:
    found = []
    for cik, newest, quarterly, us_gaap, read in con.execute("""
            SELECT c.cik, max(f.filed), bool_or(f.period_type = 'quarterly'), any_value(c.us_gaap),
                   any_value(c.facts_as_of)
            FROM (SELECT DISTINCT cik, us_gaap, facts_as_of FROM fundamental_company) c
            LEFT JOIN fundamental_fact f ON f.cik = c.cik AND NOT f.preliminary
            GROUP BY c.cik ORDER BY c.cik""").fetchall():
        if newest is None:
            if us_gaap is False:
                found.append(("no_us_gaap_facts", cik, f"facts read {read}: no us-gaap taxonomy"))
            else:
                found.append(("no_statement", cik, "no facts read yet" if read is None else f"facts read {read}"))
        elif quarterly and (as_of - newest).days > companyfacts.STALE_DAYS:
            found.append(("statement_older_than_120_days", cik,
                          f"newest statement filed {newest} ({(as_of - newest).days} days before {as_of})"))
        elif not quarterly and (as_of - newest).days > companyfacts.ANNUAL_STALE_DAYS:
            found.append(("annual_statement_older_than_400_days", cik,
                          f"newest statement filed {newest} ({(as_of - newest).days} days before {as_of}); "
                          "an annual-only filer"))
    return found


class FundamentalsTables:
    #: #812: the whole-universe build (5,352 Tier A tickers, 5.9 M facts on 12 Nov 2026) is the other half of
    #: the pass that was OOM-killed beside ``sec/companyfacts``.
    heavy = True
    name = NAME
    kind = KIND
    providers: tuple[str, ...] = ()
    tables = TABLES
    feeds: tuple[str, ...] = ()       # it builds every served table itself
    cadence = "daily"
    backfill_needs_from = False
    starts_by_hand = True

    def default_as_of(self) -> date:
        return calendar.today_ny()

    def fetch(self, ctx: base.RunContext, as_of: date) -> list:
        return []                                   # built from the sources' Parquet only

    def backfill(self, ctx: base.RunContext, start, end: date) -> None:
        return None

    def to_parquet(self, ctx, raw) -> list:
        return []

    def to_parquet_all(self, ctx, raws) -> list:
        return []

    # ``rules_state`` went with the cut (#858). It named the document cap and whether the pinned cut capped at all,
    # because both came from outside this package (``module_value_chain/splitter.py``) and a live copy keeps its
    # old CHECK unless something restates it. No CHECK of this package embeds a value from outside it any more,
    # and the files of this package this dataset's code reaches are hashed by ``dirty.dataset_fingerprint``, so
    # this change restates this dataset by itself - which is what drops the ``document`` rows from a live
    # ``filing_item``.

    def dirty(self, ctx: base.RunContext):
        """No Parquet of its own: what it serves is made from the sources' staging (#446)."""
        return dirty_set.ALL

    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        _ensure_staging(con)
        ciks = common.dirty_ciks(ctx, con)
        if ciks is not None:
            for view in VIEWS:                     # made again below, over the tables as this build leaves them
                con.execute(f"DROP VIEW IF EXISTS {view}")
        counts = {"fundamental_fact": _facts(con, ciks), "filing_item": _filing_items(con, ciks)}
        counts["fundamental_company"] = _companies(ctx, con, common.universe(), ciks)
        counts["earnings_event"] = _earnings(con, ciks)
        counts["earnings_reported"] = _earnings_reported(con, ciks)
        counts["earnings_report_history"] = _report_history(con, ciks)
        counts["earnings_transcript"] = _transcripts(con)
        counts["audit_flag"] = _audit_flags(con, ciks)
        counts["fundamental_revision"] = _revisions(con, ciks)
        counts["segment_revenue"] = _segments(con, ciks)
        counts["customer_share"] = _customers(con, ciks)
        counts["estimate_latest"] = _estimates(con, ciks)
        counts["peer_group"] = _peers(con, run_id=ctx.run_id, ciks=ciks)
        _views(con)
        _submission_view(con)
        _calendar_view(con)
        _estimate_view(con)
        found = _issues(con, ctx.as_of or self.default_as_of())
        # Read from the served tables: on the live copy the staging holds only the dirty companies.
        found += [("source_never_run", name, "no Parquet yet") for name, table, where in
                  ((companyfacts.NAME, "fundamental_fact", f"source = '{facts.SOURCE}'"),
                   (filings.NAME, "filing_item", "kind = 'item'")) if not common.count(con, table, where)]
        ctx.pending[NAME] = found
        return counts

    def check(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict:
        cols = ["ticker", "cik", "gvkey", "gsubind"]
        added, removed = common.compare_live(con, f"SELECT {', '.join(cols)} FROM fundamental_company",
                                             "fundamental_company", cols)
        n_issues = len(ctx.pending.get(NAME, []))
        ctx.notes.append(f"{common.count(con, 'fundamental_company')} Tier A tickers, "
                         f"{common.count(con, 'fundamental_fact')} facts; {n_issues} build issues found")
        return {"dataset": NAME, "result": "changed" if added or removed else "unchanged",
                "source_as_of": (ctx.as_of or self.default_as_of()).isoformat(),
                "rows": common.count(con, "fundamental_company"), "added": added, "removed": removed}

    def after_swap(self, ctx: base.RunContext) -> None:
        found = ctx.pending.get(NAME)
        if found is None:
            return
        ops.sync_issues(NAME, ISSUE_KINDS, found, run_id=ctx.run_id)


DATASET = FundamentalsTables()
