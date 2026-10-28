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
from . import KIND, common, companyfacts, earnings, facts, filings, uw_financials, wrds_fund

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
    ("source", "TEXT"), ("run_id", "TEXT"),
]
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

# One row per Compustat value that changed between two monthly snapshots, for a period both held (``wrds_fund``);
# ``cik`` NULL when the gvkey is no longer a Tier A company's.
REVISION = [("cik", "TEXT"), ("gvkey", "TEXT NOT NULL"), ("source", "TEXT NOT NULL"), ("datadate", "DATE NOT NULL"),
            ("item", "TEXT NOT NULL"), ("old_value", "DOUBLE"), ("new_value", "DOUBLE"), ("old_as_of", "DATE NOT NULL"),
            ("new_as_of", "DATE NOT NULL"), ("run_id", "TEXT")]

# Revenue by segment, product or country: the annual report's inline XBRL (``sec/filings``, its newest year) and
# Compustat's segments (``wrds/segments``). ``document_date`` is the date of the document a value came from (the
# SEC filing date; Compustat's ``srcdate``).
SEGMENT_REVENUE = [("cik", "TEXT NOT NULL"), ("period_end", "DATE NOT NULL"), ("period_start", "DATE"),
                   ("axis", "TEXT NOT NULL"), ("segment", "TEXT"), ("member", "TEXT"), ("concept", "TEXT"),
                   ("value", "DOUBLE NOT NULL"), ("unit", "TEXT"), ("accession", "TEXT"), ("form", "TEXT"),
                   ("document_date", "DATE"), ("source", "TEXT NOT NULL"), ("source_as_of", "DATE"),
                   ("fetched_at", "DOUBLE"), ("run_id", "TEXT")]
# A major customer's share: the annual report's ConcentrationRiskPercentage1, or Compustat's customer sales over the
# SEC's revenue of that year (``share`` NULL when there is none to divide by, or the ratio is above 1).
CUSTOMER_SHARE = [("cik", "TEXT NOT NULL"), ("period_end", "DATE NOT NULL"), ("customer", "TEXT"),
                  ("member", "TEXT"), ("benchmark", "TEXT"), ("share", "DOUBLE"), ("sales", "DOUBLE"),
                  ("customer_type", "TEXT"), ("accession", "TEXT"), ("form", "TEXT"), ("document_date", "DATE"),
                  ("source", "TEXT NOT NULL"), ("source_as_of", "DATE"), ("fetched_at", "DOUBLE"), ("run_id", "TEXT")]

SCHEMAS: dict[str, list[tuple[str, str]]] = {
    "fundamental_company": COMPANY,
    "fundamental_fact": FACT,
    "filing_item": FILING_ITEM,
    "earnings_event": EARNINGS_EVENT,
    "fundamental_revision": REVISION,
    "segment_revenue": SEGMENT_REVENUE,
    "customer_share": CUSTOMER_SHARE,
}
TABLE_CHECKS: dict[str, list[str]] = {
    # Every row is annual or quarterly, and a non-preliminary row carries its accession and filed date.
    "fundamental_fact": ["CHECK (period_type IN ('annual', 'quarterly'))",
                         "CHECK (preliminary OR (accn IS NOT NULL AND filed IS NOT NULL))"],
    # A document's text is never labelled ok at the cap (filings.CAPPED).
    "filing_item": ["CHECK (kind IN ('item', 'document'))",
                    f"CHECK (quality IS NULL OR quality <> 'ok' OR chars < {filings.max_text_chars()})"],
    "segment_revenue": ["CHECK (axis IN ('business', 'product', 'geographic'))"],
    "customer_share": ["CHECK (share IS NULL OR (share >= 0 AND share <= 1))"],
}
TABLES = tuple(SCHEMAS)
VIEWS = ("fundamental_quarter", "edgar_financials", "sec_submissions", "earnings_calendar")

# The sources' staging tables, created empty here when a source is not registered (a test's registry).
STAGED: dict[str, list[tuple[str, str]]] = {
    companyfacts.STAGING["facts"]: facts.FACT,
    companyfacts.STAGING["company"]: facts.COMPANY,
    filings.STAGING["filings"]: filings.FILING,
    filings.STAGING["documents"]: filings.DOCUMENT,
    earnings.STAGING: earnings.SCHEMA,
    uw_financials.STAGING: facts.FACT,
    **{d.revision_staging: wrds_fund.REVISION for d in wrds_fund.DATASETS if d.revisions},
    filings.STAGING["segments"]: filings.SEGMENT,
    filings.STAGING["customers"]: filings.CUSTOMER,
    **{wrds_fund.SEGMENTS.STAGING[p]: schema + wrds_fund.PROVENANCE
       for p, (schema, _, _) in wrds_fund.SEGMENTS.PARTS.items()},
}

# The named problems, one kind each; ``key`` is the CIK unless the kind says otherwise.
ISSUE_KINDS: dict[str, str] = {
    "source_never_run": "a source this build needs has no data yet (key: the dataset)",
    "statement_older_than_120_days": ("a Tier A quarterly filer whose newest statement was filed more than 120 days "
                                      "before the build's as-of"),
    "no_statement": "a Tier A company with no SEC statement fact (not read yet, or no XBRL facts at the SEC)",
    "no_us_gaap_facts": "a Tier A company whose XBRL facts hold no us-gaap taxonomy (an IFRS filer; not read yet)",
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


def _names(table: str) -> str:
    return ", ".join(f'"{c}"' for c, _ in SCHEMAS[table])


def _ensure_staging(con: duckdb.DuckDBPyConnection) -> None:
    for name, schema in STAGED.items():
        cols = ", ".join(f'"{c}" {t}' for c, t in schema)
        con.execute(f"CREATE TEMP TABLE IF NOT EXISTS {name} ({cols})")


# ── The served tables ─────────────────────────────────────────────────────────────────────────────────────────

def _facts(con: duckdb.DuckDBPyConnection) -> int:
    """``fundamental_fact``: the SEC facts, one row per (company, field, period type, period end), and UW's
    preliminary quarters after the company's newest SEC quarter (``uw_financials``; only when that is on)."""
    create(con, "fundamental_fact")
    names = _names("fundamental_fact")
    con.execute(f"""INSERT INTO fundamental_fact SELECT {names} FROM {companyfacts.STAGING['facts']}
                    QUALIFY row_number() OVER (PARTITION BY cik, field, period_type, period_end
                                               ORDER BY filed DESC, accn DESC) = 1""")
    if uw_financials.enabled():
        con.execute(f"""
            INSERT INTO fundamental_fact SELECT {", ".join(f"u.{c}" for c in names.split(", "))}
            FROM {uw_financials.STAGING} u
            JOIN (SELECT cik, max(period_end) AS sec_end FROM fundamental_fact
                  WHERE NOT preliminary AND period_type = '{facts.QUARTERLY}' GROUP BY cik) s ON s.cik = u.cik
            WHERE u.period_type = '{facts.QUARTERLY}' AND u.period_end > s.sec_end + {uw_financials.MARGIN_DAYS}""")
    return common.count(con, "fundamental_fact")


def _filing_items(con: duckdb.DuckDBPyConnection) -> int:
    """``filing_item``: every listed filing and 8-K item, and the document texts."""
    create(con, "filing_item")
    con.execute(f"""
        INSERT INTO filing_item
        SELECT cik, accession, form, filing_date, report_date, primary_document, 'item', item, NULL, NULL, NULL,
               '{filings.NAME}', source_as_of, fetched_at, run_id FROM {filings.STAGING['filings']}
        UNION ALL
        SELECT cik, accession, form, filing_date, report_date, primary_document, 'document', item, text, chars,
               quality, '{filings.NAME}', source_as_of, fetched_at, run_id FROM {filings.STAGING['documents']}""")
    return common.count(con, "filing_item")


EARNINGS_8K_ITEM = "2.02"


def _earnings(con: duckdb.DuckDBPyConnection) -> int:
    """``earnings_event``: Alpha Vantage's coming reports of the Tier A tickers, and the 8-K item 2.02 reports."""
    create(con, "earnings_event")
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


def _revisions(con: duckdb.DuckDBPyConnection) -> int:
    """``fundamental_revision``: the Compustat revisions of ``fundq`` and ``funda``, with the company's CIK."""
    create(con, "fundamental_revision")
    union = " UNION ALL ".join(f"SELECT * FROM {d.revision_staging}" for d in wrds_fund.DATASETS if d.revisions)
    con.execute(f"""
        INSERT INTO fundamental_revision
        SELECT c.cik, r.gvkey, r.source, r.datadate, r.item, r.old_value, r.new_value, r.old_as_of, r.new_as_of,
               r.run_id
        FROM ({union}) r LEFT JOIN {_links()} c ON c.gvkey = r.gvkey""")
    return common.count(con, "fundamental_revision")


def _segments(con: duckdb.DuckDBPyConnection) -> int:
    """``segment_revenue``: the annual report's segments and Compustat's (business and geographic)."""
    create(con, "segment_revenue")
    seg = wrds_fund.SEGMENTS
    axis = " ".join(f"WHEN '{t}' THEN '{a}'" for t, a in sorted(wrds_fund.SEGMENT_TYPES.items()))
    con.execute(f"""
        INSERT INTO segment_revenue
        SELECT cik, period_end, period_start, axis, segment, member, concept, value, unit, accession, form,
               filing_date, '{filings.NAME}', source_as_of, fetched_at, run_id
        FROM {filings.STAGING['segments']} WHERE period_end IS NOT NULL AND value IS NOT NULL
        UNION ALL
        SELECT c.cik, w.datadate, NULL, CASE w.stype {axis} END, w.snms, w.stype || ' ' || w.sid, 'sales',
               w.sales * {wrds_fund.MILLION}, 'USD', NULL, NULL, w.srcdate, '{seg.name}', w.source_as_of,
               w.fetched_at, w.run_id
        FROM {seg.STAGING['wrds_segmerged']} w JOIN {_links()} c ON c.gvkey = w.gvkey
        WHERE w.datadate IS NOT NULL AND w.sales IS NOT NULL AND w.stype IN
              ({", ".join(f"'{t}'" for t in sorted(wrds_fund.SEGMENT_TYPES))})""")
    return common.count(con, "segment_revenue")


def _customers(con: duckdb.DuckDBPyConnection) -> int:
    """``customer_share``: the annual report's major customers, and Compustat's customers over the SEC's revenue."""
    create(con, "customer_share")
    seg = wrds_fund.SEGMENTS
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


def _links() -> str:
    return "(SELECT DISTINCT gvkey, cik FROM fundamental_company WHERE gvkey IS NOT NULL)"


def _companies(ctx: base.RunContext, con: duckdb.DuckDBPyConnection, rows: list[dict]) -> int:
    spine = [{"ticker": r["ticker"], "cik": common.norm_cik(r["cik"]), "gvkey": r.get("gvkey"), "name": r.get("name"),
              "exchange": r.get("exchange"), "gsector": r.get("gsector"), "ggroup": r.get("ggroup"),
              "gind": r.get("gind"), "gsubind": r.get("gsubind"), "sector": r.get("sector"),
              "industry": r.get("industry"), "spine_as_of": r.get("source_as_of")} for r in rows]
    base.rows_table(con, "_spine", [(c, t) for c, t in _plain("fundamental_company")][:12], spine)
    create(con, "fundamental_company")
    con.execute(f"""
        INSERT INTO fundamental_company
        SELECT s.*, c.entity_name, c.is_us, c.non_usd_units, c.us_gaap, c.facts_as_of, c.fetched_at,
               n.newest_filed, n.newest_period_end, l.submissions_as_of, 'spine/security', '{ctx.run_id}'
        FROM _spine s
        LEFT JOIN (SELECT * FROM {companyfacts.STAGING['company']}
                   QUALIFY row_number() OVER (PARTITION BY cik ORDER BY facts_as_of DESC) = 1) c ON c.cik = s.cik
        LEFT JOIN (SELECT cik, max(filed) AS newest_filed, max(period_end) AS newest_period_end
                   FROM fundamental_fact WHERE NOT preliminary GROUP BY cik) n ON n.cik = s.cik
        LEFT JOIN (SELECT cik, max(source_as_of) AS submissions_as_of FROM {filings.STAGING['filings']}
                   GROUP BY cik) l ON l.cik = s.cik""")
    con.execute("DROP TABLE _spine")
    return common.count(con, "fundamental_company")


# ── The views (the old ``raw.duckdb`` shapes, D4) ─────────────────────────────────────────────────────────────

def _views(con: duckdb.DuckDBPyConnection) -> None:
    pivot = ", ".join(f"max(value) FILTER (WHERE field = '{f}') AS \"{f}\"" for f in facts.FIELDS)
    # One row per company and quarter, a column per field; ``filed`` the newest filing a value came from.
    con.execute(f"""
        CREATE VIEW fundamental_quarter AS
        SELECT cik, period_end, min(period_start) AS period_start, {pivot},
               max(filed) AS filed, bool_or(preliminary) AS preliminary,
               string_agg(DISTINCT source, ',' ORDER BY source) AS source
        FROM fundamental_fact WHERE period_type = 'quarterly' GROUP BY cik, period_end""")
    # The old ``edgar_financials`` row per ticker: ``annual`` / ``quarterly`` as JSON lists of records (oldest
    # first) shaped as sec_edgar.extract_* writes them (a key per field, ``period_ends`` / ``filed`` / ``accn``
    # maps; ``fiscal_year`` or ``period``), and ``meta``. A field with no value has no key (the old rows held
    # None); ``total_debt`` is the three-term sum. The SEC's rows only: a preliminary row has no accession.
    def records(period_type: str, key: str, key_json: str) -> str:
        return f"""
            SELECT cik, {key} AS k, json_merge_patch(
                json_object({key_json}), json_group_object(field, value),
                json_object('period_ends', json_group_object(field, CAST(period_end AS VARCHAR)),
                            'filed', json_group_object(field, CAST(filed AS VARCHAR)),
                            'accn', json_group_object(field, accn))) AS rec
            FROM fundamental_fact WHERE period_type = '{period_type}' AND NOT preliminary GROUP BY cik, {key}"""
    con.execute(f"""
        CREATE VIEW edgar_financials AS
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
        CREATE VIEW sec_submissions AS
        SELECT cik, accession, any_value(form) AS form, any_value(filing_date) AS filing_date,
               any_value(primary_document) AS primary_document,
               string_agg(item, ',' ORDER BY item) AS items, '{filings.NAME}' AS source,
               max(fetched_at) AS fetched_at, any_value(run_id) AS run_id, NULL::DOUBLE AS run_at
        FROM filing_item WHERE kind = 'item' GROUP BY cik, accession""")


def _calendar_view(con: duckdb.DuckDBPyConnection) -> None:
    # The old ``earnings_calendar`` shape, one row per ticker and report; what this layer does not have is NULL.
    con.execute("""
        CREATE VIEW earnings_calendar AS
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
    return found


class FundamentalsTables:
    name = NAME
    kind = KIND
    providers: tuple[str, ...] = ()
    tables = TABLES
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

    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        _ensure_staging(con)
        counts = {"fundamental_fact": _facts(con), "filing_item": _filing_items(con)}
        counts["fundamental_company"] = _companies(ctx, con, common.universe())
        counts["earnings_event"] = _earnings(con)
        counts["fundamental_revision"] = _revisions(con)
        counts["segment_revenue"] = _segments(con)
        counts["customer_share"] = _customers(con)
        _views(con)
        _submission_view(con)
        _calendar_view(con)
        found = _issues(con, ctx.as_of or self.default_as_of())
        found += [("source_never_run", name, "no Parquet yet") for name, table in
                  ((companyfacts.NAME, companyfacts.STAGING["facts"]),
                   (filings.NAME, filings.STAGING["filings"])) if not common.count(con, table)]
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
