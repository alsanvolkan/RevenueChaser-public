"""``ownership/serve``: what the ownership store derives from its sources and the spine, built last of the kind.

**13F totals**, over the filings ``sec/13f`` typed (``status`` ok) and their holdings in Parquet:

- *The amendment rule* (the SEC's Form 13F instructions; the ownership review of 2026-10-02, Q1): per filer and
  report period, in filed order, a holdings report (13F-HR) starts the period's set, a report of amendment type
  ``NEW HOLDINGS`` adds to it (whether or not its form carries the ``/A``: one 13F-HR of 9,501 in a real set does
  not, and read as a new report it replaced the quarter with its few names; 13F review B1, 2026-10-30), and any
  other amendment (``RESTATEMENT``, or none stated) replaces it. An amendment belongs to the period it reports on,
  not to the quarter it was filed in.
- *Long equity only*: ``sh_type`` ``SH`` and no put or call. Principal amounts and options stay in Parquet.
- *The quarters served*: a quarter whose filing deadline passed before the store's first 13F day (the walk's first
  day, which the data-set fill moves back) is left out: only its late filers and amendments are stored (the sets
  from 2025 hold 852 such filings of 135 filers, back to 2001), so its breadth and totals would mislead. The filings
  stay in ``sec13f_filings`` and Parquet.
- *VALUE off by 1,000* (alsanvolkan/RevenueChaser#208): a filing made from 2023-01-03 reports dollars, but some
  filers kept writing thousands (332 of 8,848 in Q2 2026, T. Rowe Price among them) and one wrote dollars times
  1,000. The 13F's own prices are the reference: per CUSIP and period, the median value per share over the filers
  holding it (``CONSENSUS_FILERS`` or more). A filer whose median ratio to those prices, over ``MIN_NAMES`` or more
  names, lies inside ``SCALE_BAND`` (1/3000 to 1/300, the old derive's band, which read closes instead) has its value
  read in thousands (``sec13f_filer_period.value_factor`` 1000), and one inside its inverse (300 to 3000) has it
  divided by 1,000 (0.001). Shares never change.
- *Each CUSIP named*, per report period, by the first spine source that answers: (1) Compustat's dated identifier
  history (``security_history``, field ``cusip``, the period inside ``valid_from``..``valid_to``) with exactly one
  ticker: ``wrds/sec_idhist``; (2) the spine's current CUSIP of exactly one ticker: ``spine/security``; (3) the
  spine's OpenFIGI map, when its ticker is a spine ticker: ``openfigi/map`` (the map asks the newest two quarters'
  13F CUSIPs too, a CINS as ``ID_CINS``). A CUSIP is never named from an issuer name.
- *The reused-ticker guard* (#236, on the 13F side): OpenFIGI's ticker is refused when Compustat gives the CUSIP to
  securities (``comp.security`` and the CUSIP items of ``comp.sec_idhist``, every company, not only the linked) and
  the ticker is linked to none of them: an old company's CUSIP whose ticker was later reused. The spine's current
  CUSIP (2), which carries no period, is refused for a period in which Compustat's dated history gives the CUSIP to
  another security, or gives it to one with no dates (review S1, 2026-10-28). A refused row stays in the map
  (``map_source`` ``refused``, ``candidate_ticker``) and is a ``cusip_owner_differs`` issue; nothing is dropped, and
  OpenFIGI is not asked in its place.
- *The metrics*, per ticker and period (summed over its CUSIPs; one holder per filer CIK), as the old derive defines
  them (``module_sec_13f/engine.py`` ``compute_period_metrics``): shares, value, holders, the top 10 holders' share of
  the shares (%), the HHI of the holders' shares, and breadth, the holders as a share of the period's filers (%; a
  filer counts when it has a long-equity row). Against the **calendar quarter before** (``prev_period``), not the
  name's own last stored quarter (module_sec_13f.md: a gap in a name's filings widened the step): new and exited
  holders, buy and sell flow (shares) and ``breadth_chg`` (percentage points). When the store holds no filer for that
  quarter they are unknown (NULL), never 0. An exit need not be a sale (ownership_review.md), so two counts say how
  many may not be: ``exited_confidential``, the filer's report for the quarter left holdings out under confidential
  treatment (``isConfidentialOmitted``; a later ``NEW HOLDINGS`` amendment adds them back), and ``exited_no_filing``,
  the filer has no holdings report for the quarter (not filed yet, a notice that another manager reports them, or no
  longer filing).
- *Completeness*: ``filing_deadline`` is the period end plus 45 days, rolled past weekends and Washington's Birthday
  (``filing_deadline``); a quarter is complete once its deadline has passed. ``filed_through`` is its newest filing.

**Tables**: ``sec13f_cusip_map`` (CUSIP and period: its ticker and how, its long-equity value and holders),
``sec13f_filer_period`` (filer and period: its long-equity names and value, ``value_factor``), ``sec13f_ticker_period``
(the metrics) and ``sec13f_periods`` (per period: filers, value, value mapped, coverage).

**Views** under the old ``raw.duckdb`` names, with the old columns (D4): ``sec13f_totals`` (ticker, ``entry`` JSON
``{period: {shares, value, holders, ...}}``) and ``short_interest``: one row per ticker and session (an IBKR snapshot
session or a FINRA settlement date) with the newest FINRA short interest settled on or before it and that session's
IBKR borrow fee, rebate and shares available. ``si_float`` and ``total_float`` have no source here and are NULL.

**When it derives.** A run of any ownership dataset builds the whole kind. The 13F tables are derived on a run of
``sec/13f`` or of this dataset (a spine change reaches them through this dataset's own daily run) and carried over
from the live file otherwise, so a FINRA or IBKR run does not read millions of holdings again.

**Build issues** (``ISSUE_KINDS``), written once the file is live: ``spine_missing``, ``cusip_owner_differs``,
``cusip_unmapped_large`` (the ``UNMAPPED_TOP`` largest CUSIPs no source names, of the newest complete quarter, as
the plan's counts are, else the newest),
``filings_unparsed`` (per index day) and ``value_rescaled`` (the ``RESCALED_TOP`` filer-quarters whose rescale moved
the most dollars, keyed ``cik|period``, with the median ratio and the names it rests on).
"""
from __future__ import annotations

from datetime import date, timedelta

import duckdb

from ....core import calendar
from ... import ops, serving
from .. import base
from ..spine import openfigi_map, wrds
from . import KIND, common, sec13f

NAME = "ownership/serve"
SPINE = "spine"
HIST, CURRENT, FIGI, REFUSED = "wrds/sec_idhist", "spine/security", openfigi_map.NAME, "refused"
CONSENSUS_FILERS = 3
MIN_NAMES = 5
SCALE_BAND = (1.0 / 3000.0, 1.0 / 300.0)
DEADLINE_DAYS = 45
UNMAPPED_TOP = 50
RESCALED_TOP = 50
SP500, FUNDS = "sp500", "funds/index_holdings"

MAP = [("cusip", "TEXT"), ("period", "DATE"), ("ticker", "TEXT"), ("map_source", "TEXT"),
       ("candidate_ticker", "TEXT"), ("issuer_name", "TEXT"), ("value", "DOUBLE"), ("holders", "INTEGER")]
FILER = [("cik", "TEXT"), ("period", "DATE"), ("filer_name", "TEXT"), ("filings", "INTEGER"), ("cusips", "INTEGER"),
         ("value", "DOUBLE"), ("names", "INTEGER"), ("median_ratio", "DOUBLE"), ("value_factor", "DOUBLE"),
         ("confidential_omitted", "BOOLEAN")]
TICKER = [("ticker", "TEXT"), ("period", "DATE"), ("shares", "DOUBLE"), ("value", "DOUBLE"), ("holders", "INTEGER"),
          ("top10_pct", "DOUBLE"), ("hhi", "DOUBLE"), ("new_holders", "INTEGER"), ("exited_holders", "INTEGER"),
          ("exited_confidential", "INTEGER"), ("exited_no_filing", "INTEGER"), ("buy_flow", "DOUBLE"),
          ("sell_flow", "DOUBLE"), ("breadth", "DOUBLE"), ("breadth_chg", "DOUBLE"),
          ("prev_period", "DATE"), ("filers", "INTEGER"), ("cusips", "INTEGER"), ("filing_deadline", "DATE"),
          ("filed_through", "DATE"), ("source", "TEXT")]
PERIODS = [("period", "DATE"), ("filers", "INTEGER"), ("filings_active", "INTEGER"), ("value_total", "DOUBLE"),
           ("value_mapped", "DOUBLE"), ("coverage", "DOUBLE"), ("unmapped_cusips", "INTEGER"),
           ("refused_value", "DOUBLE"), ("rescaled_filers", "INTEGER"), ("filing_deadline", "DATE"),
           ("filed_through", "DATE")]
SCHEMAS = {"sec13f_cusip_map": MAP, "sec13f_filer_period": FILER, "sec13f_ticker_period": TICKER,
           "sec13f_periods": PERIODS}
TABLES = tuple(SCHEMAS)
# The old sec13f_totals entry's keys, in its order (module_sec_13f/engine.py compute_period_metrics).
METRICS = ("shares", "value", "holders", "top10_pct", "hhi", "new_holders", "exited_holders", "buy_flow",
           "sell_flow", "breadth", "breadth_chg")
ISSUE_KINDS = {
    "spine_missing": "no live spine.duckdb: no CUSIP is named a ticker",
    "cusip_owner_differs": "the spine's current CUSIP or OpenFIGI names a ticker whose security Compustat does not "
                           "give the CUSIP to in that period (a reused ticker or CUSIP)",
    "cusip_unmapped_large": "one of the newest complete quarter's largest long-equity CUSIPs no spine source names",
    "filings_unparsed": "13F filings of an index day whose documents did not parse",
    "value_rescaled": "one of the largest filer-quarters whose VALUE was off by a factor of 1,000 and was rescaled",
}

VIEWS = {
    "sec13f_totals": (
        "SELECT ticker, CAST(json_group_object(strftime(period, '%Y-%m-%d'), json_object("
        + ", ".join(f"'{m}', {m}" for m in METRICS) + ")) AS JSON) AS entry "
        "FROM sec13f_ticker_period GROUP BY ticker"),
    "short_interest": """
        WITH f AS (
            SELECT ticker, settlement_date, current_short, days_to_cover, fetched_at, run_id
            FROM finra_short_interest WHERE ticker IS NOT NULL AND settlement_date IS NOT NULL
            QUALIFY row_number() OVER (PARTITION BY ticker, settlement_date
                                       ORDER BY (symbol = ticker) DESC, symbol) = 1),
        i AS (
            SELECT ticker, session_date, fee_rate, rebate_rate, available, fetched_at, run_id
            FROM ibkr_shortable WHERE ticker IS NOT NULL AND session_date IS NOT NULL
            QUALIFY row_number() OVER (PARTITION BY ticker, session_date
                                       ORDER BY snapshot_at DESC, fetched_at DESC, (symbol = ticker) DESC,
                                                symbol) = 1),
        k AS (SELECT ticker, session_date FROM i UNION SELECT ticker, settlement_date FROM f)
        SELECT k.ticker, k.session_date, f.settlement_date AS market_date,
               CAST(f.current_short AS DOUBLE) AS short_interest, CAST(NULL AS DOUBLE) AS si_float,
               CAST(NULL AS DOUBLE) AS total_float, CAST(f.days_to_cover AS DOUBLE) AS days_to_cover,
               CAST(i.fee_rate AS DOUBLE) AS fee_rate, CAST(i.rebate_rate AS DOUBLE) AS rebate_rate,
               CAST(i.available AS DOUBLE) AS short_shares_available, CAST(NULL AS JSON) AS payload,
               concat_ws('+', CASE WHEN f.ticker IS NOT NULL THEN 'finra/short_interest' END,
                         CASE WHEN i.ticker IS NOT NULL THEN 'ibkr/shortable' END) AS source,
               CAST(list_max([f.fetched_at, i.fetched_at]) AS DOUBLE) AS fetched_at,
               coalesce(i.run_id, f.run_id) AS run_id, CAST(NULL AS DOUBLE) AS run_at
        FROM k
        ASOF LEFT JOIN f ON f.ticker = k.ticker AND k.session_date >= f.settlement_date
        LEFT JOIN i ON i.ticker = k.ticker AND i.session_date = k.session_date""",
}


def _washingtons_birthday(year: int) -> date:
    first = date(year, 2, 1)
    return first + timedelta(days=(7 - first.weekday()) % 7 + 14)       # the third Monday of February


def filing_deadline(period: date) -> date:
    """45 days after the period end, moved to the next business day: past a weekend, and past Washington's Birthday,
    the one federal holiday such a move can reach (the deadlines fall on 14-15 February, 15 May, 14 August and
    14 November)."""
    d = period + timedelta(days=DEADLINE_DAYS)
    while d.weekday() >= 5 or d == _washingtons_birthday(d.year):
        d += timedelta(days=1)
    return d


def active_accessions(rows) -> list[str]:
    """The amendment rule over ``rows`` (accession, cik, period, ..., is_amendment, amendment_type), sorted by filer,
    period and filed order: the accessions whose holdings count. A ``NEW HOLDINGS`` report adds whatever its form
    says (adding to an empty set is starting it)."""
    out: list[str] = []
    key, active = None, []
    for acc, cik, period, *_order, is_amendment, kind in rows:
        if (cik, period) != key:
            out += active
            key, active = (cik, period), []
        if kind == "NEW HOLDINGS":
            active.append(acc)
        else:
            active = [acc]
    return out + active


def _empty(con: duckdb.DuckDBPyConnection, name: str, schema: list[tuple[str, str]]) -> None:
    common.temp_rows(con, name, schema, [])


def _spine(con: duckdb.DuckDBPyConnection, found: list) -> bool:
    """``_sp_security``, ``_sp_hist`` and ``_sp_sp500`` (TEMP) from the live spine, attached read-only and detached
    again; empty, and a ``spine_missing`` issue, without one."""
    _empty(con, "_sp_security", [("ticker", "TEXT"), ("gvkey", "TEXT"), ("iid", "TEXT"), ("cusip", "TEXT")])
    _empty(con, "_sp_hist", [("ticker", "TEXT"), ("cusip", "TEXT"), ("valid_from", "DATE"), ("valid_to", "DATE")])
    _empty(con, "_sp_sp500", [("ticker", "TEXT")])
    path = serving.live_path(SPINE)
    if not path.exists():
        found.append(("spine_missing", SPINE, ISSUE_KINDS["spine_missing"]))
        return False
    con.execute(f"ATTACH {base.sql_path(path)} AS spine_src (READ_ONLY)")
    try:
        have = {r[0] for r in con.execute(
            "SELECT table_name FROM duckdb_tables() WHERE database_name = 'spine_src'").fetchall()}
        if "security" not in have:
            found.append(("spine_missing", SPINE, "the live spine.duckdb has no security table"))
            return False
        con.execute("INSERT INTO _sp_security SELECT ticker, gvkey, iid, upper(cusip) FROM spine_src.security")
        if "security_history" in have:
            con.execute("INSERT INTO _sp_hist SELECT ticker, upper(value), valid_from, valid_to "
                        "FROM spine_src.security_history "
                        "WHERE field = 'cusip' AND ticker IS NOT NULL AND value IS NOT NULL")
        if "index_membership" in have:
            con.execute(f"INSERT INTO _sp_sp500 SELECT DISTINCT ticker FROM spine_src.index_membership "
                        f"WHERE index_slug = '{SP500}' AND source = '{FUNDS}' AND valid_to IS NULL "
                        f"AND ticker IS NOT NULL")
        return True
    finally:
        con.execute("DETACH spine_src")


OWNERS = [("cusip", "TEXT"), ("gvkey", "TEXT"), ("iid", "TEXT"), ("efffrom", "DATE"), ("effthru", "DATE")]


def _owners(con: duckdb.DuckDBPyConnection) -> None:
    """``_owners(cusip, gvkey, iid, efffrom, effthru)`` (TEMP): every security Compustat gives each CUSIP, now or
    before, with the dates of its identifier history; a security's own CUSIP with no dated row has none (NULL: any
    period)."""
    security = wrds.parquet_files(wrds.SEC_IDHIST.name, "security")
    history = wrds.parquet_files(wrds.SEC_IDHIST.name, "sec_idhist")
    _empty(con, "_owners", OWNERS)
    if history:
        con.execute(f"INSERT INTO _owners SELECT DISTINCT upper(itemvalue), gvkey, iid, efffrom, effthru "
                    f"FROM {common.files_sql(history)} WHERE upper(item) = 'CUSIP' AND itemvalue IS NOT NULL")
    if security:
        con.execute(f"""INSERT INTO _owners SELECT DISTINCT upper(s.cusip), s.gvkey, s.iid, NULL, NULL
            FROM {common.files_sql(security)} s WHERE s.cusip IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM _owners o WHERE o.cusip = upper(s.cusip) AND o.gvkey = s.gvkey
                                         AND o.iid IS NOT DISTINCT FROM s.iid)""")


def _figi(con: duckdb.DuckDBPyConnection) -> None:
    _empty(con, "_figi", [("cusip", "TEXT"), ("ticker", "TEXT")])
    if openfigi_map.partition().exists():
        con.execute(f"INSERT INTO _figi SELECT upper(cusip), ticker FROM read_parquet("
                    f"{base.sql_path(openfigi_map.partition())}) WHERE status = '{openfigi_map.PRESENT}' "
                    f"AND ticker IS NOT NULL")


TEMPS = ("_sp_security", "_sp_hist", "_sp_sp500", "_owners", "_figi", "_active", "_conf", "_long", "_px", "_ratio",
         "_held",
         "_cp", "_m_hist", "_m_cur", "_m_figi", "_filers", "_filed", "_deadline", "_tot", "_pos", "_shape", "_pairs",
         "_flow")


class OwnershipServe:
    name = NAME
    kind = KIND
    providers: tuple[str, ...] = ()
    tables = TABLES
    cadence = "daily"
    backfill_needs_from = False
    starts_by_hand = True             # the first run is the server thread's, after a backup

    def default_as_of(self) -> date:
        return calendar.today_ny()

    def fetch(self, ctx: base.RunContext, as_of: date) -> list:
        return []                                   # derived from the kind's other tables and the spine only

    def backfill(self, ctx: base.RunContext, start, end: date) -> None:
        return None

    def to_parquet(self, ctx, raw) -> list:
        return []

    def to_parquet_all(self, ctx, raws) -> list:
        return []

    # ── Build ──────────────────────────────────────────────────────────────────────────────────────────────────

    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        counts = None if ctx.dataset in (NAME, sec13f.NAME) else self._carry_over(con)
        if counts is None:
            counts = self._derive(ctx, con)
        for view, sql in VIEWS.items():
            con.execute(f"CREATE VIEW {view} AS {sql}")
        return counts

    @staticmethod
    def _carry_over(con: duckdb.DuckDBPyConnection) -> dict[str, int] | None:
        """The 13F tables copied from the live file, or None when it has none of this shape."""
        live = serving.live_path(KIND)
        if not live.exists():
            return None
        con.execute(f"ATTACH {base.sql_path(live)} AS own_live (READ_ONLY)")
        try:
            cols: dict[str, list[str]] = {}
            for t, c in con.execute("SELECT table_name, column_name FROM duckdb_columns() "
                                    "WHERE database_name = 'own_live' ORDER BY table_name, column_index").fetchall():
                cols.setdefault(t, []).append(c)
            if any(cols.get(t) != [c for c, _ in s] for t, s in SCHEMAS.items()):
                return None
            return {t: common.create_table(con, t, s, f'SELECT * FROM own_live."{t}"') for t, s in SCHEMAS.items()}
        finally:
            con.execute("DETACH own_live")

    def _derive(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        found: list[tuple[str, str, str]] = []
        summary: dict = {}
        ctx.pending[NAME] = {"found": found, "summary": summary}
        try:
            spine_on = _spine(con, found)
            files = sec13f.DATASET.holding_files()
            left_out = self._held(con, files)
            if files:
                _owners(con)
                _figi(con)
            else:
                _empty(con, "_owners", OWNERS)
                _empty(con, "_figi", [("cusip", "TEXT"), ("ticker", "TEXT")])
            counts = {"sec13f_filer_period": common.count(con, "sec13f_filer_period")}
            counts["sec13f_cusip_map"] = self._map(con)
            counts["sec13f_ticker_period"] = self._metrics(con)
            counts["sec13f_periods"] = self._periods(con)
            found += self._issues(con, ctx.as_of or self.default_as_of())
            summary["rescaled"] = common.count(con, "sec13f_filer_period", "value_factor <> 1")
            if left_out:
                summary["left_out"] = left_out
            if spine_on:
                summary.update(self._summary(con, ctx.as_of or self.default_as_of()))
            return counts
        finally:
            for t in TEMPS:
                con.execute(f"DROP TABLE IF EXISTS {t}")

    @staticmethod
    def _held(con: duckdb.DuckDBPyConnection, files) -> tuple[int, int, date] | None:
        """``_long`` (the active filings' long-equity rows), ``sec13f_filer_period`` with its VALUE factor, and
        ``_held`` (filer, period and CUSIP: shares and value in dollars); ``(filings, quarters, first 13F day)`` left
        out as quarters whose deadline passed before that day, None when none is."""
        rows = con.execute(f"SELECT accession, cik, period, filed, coalesce(amendment_no, 0), is_amendment, "
                           f"amendment_type FROM {sec13f.TABLE} WHERE status = 'ok' AND cik IS NOT NULL "
                           f"AND period IS NOT NULL ORDER BY cik, period, filed, 5, is_amendment, accession").fetchall()
        first = sec13f.DATASET.first_day() or min((r[3] for r in rows), default=None)
        served = [p for p in sorted({r[2] for r in rows}) if first is None or filing_deadline(p) >= first]
        start = served[0] if served else date.max
        left = [r for r in rows if r[2] < start]
        rows = [r for r in rows if r[2] >= start]
        common.temp_rows(con, "_active", [("accession", "TEXT")], [{"accession": a} for a in active_accessions(rows)])
        # A filer's holdings report for the period, and whether it left holdings out under confidential treatment.
        con.execute(f"""CREATE OR REPLACE TEMP TABLE _conf AS
            SELECT f.cik, f.period, bool_or(f.confidential_omitted) AS omitted
            FROM {sec13f.TABLE} f JOIN _active a ON a.accession = f.accession GROUP BY ALL""")
        long_cols = [("accession", "TEXT"), ("cik", "TEXT"), ("period", "DATE"), ("cusip", "TEXT"),
                     ("name_of_issuer", "TEXT"), ("shares", "DOUBLE"), ("value_usd", "DOUBLE"),
                     ("value_scale", "INTEGER"), ("filer_name", "TEXT")]
        _empty(con, "_long", long_cols)
        if files:
            con.execute(f"""INSERT INTO _long
                SELECT h.accession, h.cik, h.period, upper(h.cusip), h.name_of_issuer, h.shares, h.value_usd,
                       f.value_scale, f.filer_name
                FROM {common.files_sql(files)} h
                JOIN _active a ON a.accession = h.accession
                JOIN {sec13f.TABLE} f ON f.accession = h.accession
                WHERE upper(h.sh_type) = 'SH' AND coalesce(trim(h.put_call), '') = '' AND h.cusip IS NOT NULL
                  AND h.cik IS NOT NULL AND h.period IS NOT NULL""")
        con.execute(f"""CREATE OR REPLACE TEMP TABLE _px AS
            SELECT cusip, period, median(value_usd / shares) AS px FROM _long
            WHERE shares > 0 AND value_usd > 0 GROUP BY ALL HAVING count(DISTINCT cik) >= {CONSENSUS_FILERS}""")
        con.execute("""CREATE OR REPLACE TEMP TABLE _ratio AS
            SELECT l.cik, l.period, count(DISTINCT l.cusip) AS names, median(l.value_usd / l.shares / p.px) AS ratio
            FROM _long l JOIN _px p ON p.cusip = l.cusip AND p.period = l.period
            WHERE l.shares > 0 AND l.value_usd > 0 AND l.value_scale = 1 GROUP BY ALL""")
        lo, hi = SCALE_BAND
        common.create_table(con, "sec13f_filer_period", FILER, f"""
            SELECT l.cik, l.period, max(l.filer_name) AS filer_name, count(DISTINCT l.accession) AS filings,
                   count(DISTINCT l.cusip) AS cusips, sum(l.value_usd) * coalesce(x.factor, 1) AS value, x.names,
                   round(x.ratio, 6) AS median_ratio, coalesce(x.factor, 1) AS value_factor,
                   c.omitted AS confidential_omitted
            FROM _long l
            LEFT JOIN (SELECT cik, period, names, ratio,
                              CASE WHEN names >= {MIN_NAMES} AND ratio > {lo!r} AND ratio < {hi!r} THEN 1000.0
                                   WHEN names >= {MIN_NAMES} AND ratio > {1 / hi!r} AND ratio < {1 / lo!r}
                                   THEN 0.001 ELSE 1.0 END AS factor FROM _ratio) x
                   ON x.cik = l.cik AND x.period = l.period
            LEFT JOIN _conf c ON c.cik = l.cik AND c.period = l.period
            GROUP BY l.cik, l.period, x.factor, x.names, x.ratio, c.omitted ORDER BY l.period, l.cik""")
        con.execute("""CREATE OR REPLACE TEMP TABLE _held AS
            SELECT l.cik, l.period, l.cusip, sum(l.shares) AS shares, sum(l.value_usd * p.value_factor) AS value,
                   min(l.name_of_issuer) AS issuer_name
            FROM _long l JOIN sec13f_filer_period p ON p.cik = l.cik AND p.period = l.period GROUP BY ALL""")
        return (len(left), len({r[2] for r in left}), first) if left else None

    @staticmethod
    def _map(con: duckdb.DuckDBPyConnection) -> int:
        con.execute("""CREATE OR REPLACE TEMP TABLE _cp AS
            SELECT cusip, period, sum(value) AS value, count(DISTINCT cik) AS holders, min(issuer_name) AS issuer_name
            FROM _held GROUP BY ALL""")
        con.execute("""CREATE OR REPLACE TEMP TABLE _m_hist AS
            SELECT c.cusip, c.period, min(h.ticker) AS ticker FROM _cp c
            JOIN _sp_hist h ON h.cusip = c.cusip AND (h.valid_from IS NULL OR h.valid_from <= c.period)
                           AND (h.valid_to IS NULL OR c.period < h.valid_to)
            GROUP BY ALL HAVING count(DISTINCT h.ticker) = 1""")
        # The current CUSIP has no period of its own: it is refused for a period in which Compustat's dated history
        # gives the CUSIP to another security (or gives it with no dates), as a reused CUSIP would be (#236).
        con.execute("""CREATE OR REPLACE TEMP TABLE _m_cur AS
            WITH s AS (SELECT cusip, min(ticker) AS ticker, min(gvkey) AS gvkey, min(iid) AS iid FROM _sp_security
                       WHERE cusip IS NOT NULL GROUP BY ALL HAVING count(DISTINCT ticker) = 1)
            SELECT c.cusip, c.period, s.ticker,
                   s.gvkey IS NOT NULL AND EXISTS (
                       SELECT 1 FROM _owners o WHERE o.cusip = c.cusip
                       AND NOT (o.gvkey = s.gvkey AND o.iid IS NOT DISTINCT FROM s.iid)
                       AND (o.efffrom IS NULL OR o.efffrom <= c.period)
                       AND (o.effthru IS NULL OR c.period <= o.effthru)) AS refused
            FROM _cp c JOIN s ON s.cusip = c.cusip""")
        con.execute("""CREATE OR REPLACE TEMP TABLE _m_figi AS
            SELECT f.cusip, f.ticker, s.gvkey, s.iid,
                   s.gvkey IS NOT NULL AND EXISTS (SELECT 1 FROM _owners o WHERE o.cusip = f.cusip)
                   AND NOT EXISTS (SELECT 1 FROM _owners o WHERE o.cusip = f.cusip AND o.gvkey = s.gvkey
                                   AND o.iid IS NOT DISTINCT FROM s.iid) AS refused
            FROM _figi f JOIN _sp_security s ON s.ticker = f.ticker""")
        return common.create_table(con, "sec13f_cusip_map", MAP, f"""
            SELECT c.cusip, c.period,
                   coalesce(a.ticker, CASE WHEN NOT b.refused THEN b.ticker END,
                            CASE WHEN b.ticker IS NULL AND NOT f.refused THEN f.ticker END) AS ticker,
                   CASE WHEN a.ticker IS NOT NULL THEN '{HIST}' WHEN NOT b.refused THEN '{CURRENT}'
                        WHEN b.refused THEN '{REFUSED}' WHEN NOT f.refused THEN '{FIGI}'
                        WHEN f.refused THEN '{REFUSED}' END AS map_source,
                   CASE WHEN a.ticker IS NULL AND b.refused THEN b.ticker
                        WHEN a.ticker IS NULL AND b.ticker IS NULL AND f.refused THEN f.ticker END AS candidate_ticker,
                   c.issuer_name, c.value, c.holders
            FROM _cp c
            LEFT JOIN _m_hist a ON a.cusip = c.cusip AND a.period = c.period
            LEFT JOIN _m_cur b ON b.cusip = c.cusip AND b.period = c.period
            LEFT JOIN _m_figi f ON f.cusip = c.cusip
            ORDER BY c.period, c.cusip""")

    @staticmethod
    def _metrics(con: duckdb.DuckDBPyConnection) -> int:
        con.execute("CREATE OR REPLACE TEMP TABLE _filers AS "
                    "SELECT period, count(DISTINCT cik) AS filers FROM _held GROUP BY ALL")
        con.execute(f"CREATE OR REPLACE TEMP TABLE _filed AS SELECT period, max(filed) AS filed_through "
                    f"FROM {sec13f.TABLE} WHERE status = 'ok' AND period IS NOT NULL GROUP BY ALL")
        periods = sorted({r[0] for r in con.execute("SELECT period FROM _filers UNION SELECT period FROM _filed")
                          .fetchall()})
        common.temp_rows(con, "_deadline", [("period", "DATE"), ("deadline", "DATE")],
                         [{"period": p, "deadline": filing_deadline(p)} for p in periods])
        # One position per ticker, period and filer (a ticker's CUSIPs summed).
        con.execute("""CREATE OR REPLACE TEMP TABLE _pos AS
            SELECT m.ticker, h.period, h.cik, sum(h.shares) AS shares
            FROM _held h JOIN sec13f_cusip_map m ON m.cusip = h.cusip AND m.period = h.period
            WHERE m.ticker IS NOT NULL GROUP BY ALL""")
        con.execute("""CREATE OR REPLACE TEMP TABLE _tot AS
            SELECT m.ticker, h.period, sum(h.shares) AS shares, sum(h.value) AS value,
                   count(DISTINCT h.cik) AS holders, count(DISTINCT h.cusip) AS cusips,
                   CAST(date_trunc('quarter', h.period) - INTERVAL 1 DAY AS DATE) AS prev_period
            FROM _held h JOIN sec13f_cusip_map m ON m.cusip = h.cusip AND m.period = h.period
            WHERE m.ticker IS NOT NULL GROUP BY m.ticker, h.period""")
        con.execute("""CREATE OR REPLACE TEMP TABLE _shape AS
            SELECT p.ticker, p.period, sum(p.shares) FILTER (WHERE p.rn <= 10) AS top10,
                   sum(CASE WHEN t.shares > 0 THEN pow(p.shares / t.shares, 2) END) AS h
            FROM (SELECT *, row_number() OVER (PARTITION BY ticker, period ORDER BY shares DESC, cik) AS rn
                  FROM _pos) p
            JOIN _tot t ON t.ticker = p.ticker AND t.period = p.period GROUP BY ALL""")
        # Each filer's position against its position in the calendar quarter before (FULL JOIN: new and exited).
        con.execute("""CREATE OR REPLACE TEMP TABLE _pairs AS
            SELECT coalesce(a.ticker, b.ticker) AS ticker, coalesce(a.period, b.next_period) AS period,
                   coalesce(a.cik, b.cik) AS cik, a.shares AS now_s, b.shares AS prev_s
            FROM _pos a
            FULL OUTER JOIN (SELECT ticker, cik, shares,
                                    CAST(date_trunc('quarter', period) + INTERVAL 6 MONTH - INTERVAL 1 DAY AS DATE)
                                        AS next_period FROM _pos) b
              ON a.ticker = b.ticker AND a.cik = b.cik AND a.period = b.next_period""")
        # An exit may not be a sale: the filer left holdings out under confidential treatment, or has no holdings
        # report for the quarter (not filed yet, a notice that another manager reports them, or no longer filing).
        con.execute("""CREATE OR REPLACE TEMP TABLE _flow AS
            SELECT p.ticker, p.period, count(*) FILTER (WHERE p.prev_s IS NULL) AS new_holders,
                   count(*) FILTER (WHERE p.now_s IS NULL) AS exited_holders,
                   count(*) FILTER (WHERE p.now_s IS NULL AND c.omitted) AS exited_confidential,
                   count(*) FILTER (WHERE p.now_s IS NULL AND c.cik IS NULL) AS exited_no_filing,
                   sum(greatest(coalesce(p.now_s, 0) - coalesce(p.prev_s, 0), 0)) AS buy,
                   sum(greatest(coalesce(p.prev_s, 0) - coalesce(p.now_s, 0), 0)) AS sell
            FROM _pairs p LEFT JOIN _conf c ON c.cik = p.cik AND c.period = p.period GROUP BY ALL""")
        return common.create_table(con, "sec13f_ticker_period", TICKER, f"""
            SELECT t.ticker, t.period, t.shares, t.value, t.holders,
                   CASE WHEN t.shares > 0 THEN round(s.top10 / t.shares * 100, 2) END AS top10_pct,
                   CASE WHEN t.shares > 0 THEN round(s.h * 10000, 1) END AS hhi,
                   CASE WHEN pf.filers IS NOT NULL THEN fl.new_holders END AS new_holders,
                   CASE WHEN pf.filers IS NOT NULL THEN fl.exited_holders END AS exited_holders,
                   CASE WHEN pf.filers IS NOT NULL THEN fl.exited_confidential END AS exited_confidential,
                   CASE WHEN pf.filers IS NOT NULL THEN fl.exited_no_filing END AS exited_no_filing,
                   CASE WHEN pf.filers IS NOT NULL THEN round(fl.buy, 1) END AS buy_flow,
                   CASE WHEN pf.filers IS NOT NULL THEN round(fl.sell, 1) END AS sell_flow,
                   round(t.holders * 100.0 / f.filers, 4) AS breadth,
                   CASE WHEN pf.filers IS NOT NULL THEN round(round(t.holders * 100.0 / f.filers, 4)
                        - round(coalesce(pt.holders, 0) * 100.0 / pf.filers, 4), 4) END AS breadth_chg,
                   t.prev_period, f.filers, t.cusips, d.deadline AS filing_deadline, fd.filed_through,
                   '{NAME}' AS source
            FROM _tot t
            JOIN _shape s ON s.ticker = t.ticker AND s.period = t.period
            JOIN _filers f ON f.period = t.period
            LEFT JOIN _filers pf ON pf.period = t.prev_period
            LEFT JOIN _tot pt ON pt.ticker = t.ticker AND pt.period = t.prev_period
            LEFT JOIN _flow fl ON fl.ticker = t.ticker AND fl.period = t.period
            LEFT JOIN _deadline d ON d.period = t.period
            LEFT JOIN _filed fd ON fd.period = t.period
            ORDER BY t.ticker, t.period""")

    @staticmethod
    def _periods(con: duckdb.DuckDBPyConnection) -> int:
        return common.create_table(con, "sec13f_periods", PERIODS, f"""
            SELECT f.period, f.filers, a.filings_active, v.value_total, coalesce(v.value_mapped, 0) AS value_mapped,
                   CASE WHEN v.value_total > 0 THEN round(coalesce(v.value_mapped, 0) / v.value_total, 4)
                   END AS coverage,
                   v.unmapped_cusips, v.refused_value, r.rescaled_filers, d.deadline AS filing_deadline,
                   fd.filed_through
            FROM _filers f
            LEFT JOIN (SELECT period, count(DISTINCT accession) AS filings_active FROM _long GROUP BY ALL) a
                   ON a.period = f.period
            LEFT JOIN (SELECT period, sum(value) AS value_total,
                              sum(value) FILTER (WHERE ticker IS NOT NULL) AS value_mapped,
                              count(*) FILTER (WHERE ticker IS NULL) AS unmapped_cusips,
                              coalesce(sum(value) FILTER (WHERE map_source = '{REFUSED}'), 0) AS refused_value
                       FROM sec13f_cusip_map GROUP BY ALL) v ON v.period = f.period
            LEFT JOIN (SELECT period, count(*) FILTER (WHERE value_factor <> 1) AS rescaled_filers
                       FROM sec13f_filer_period GROUP BY ALL) r ON r.period = f.period
            LEFT JOIN _deadline d ON d.period = f.period
            LEFT JOIN _filed fd ON fd.period = f.period
            ORDER BY f.period""")

    @staticmethod
    def _issues(con: duckdb.DuckDBPyConnection, as_of: date) -> list[tuple[str, str, str]]:
        found = []
        # The owners named are the other securities that refused it: for the current CUSIP those holding it in a
        # refused period, for OpenFIGI's ticker every holder Compustat gives (that refusal carries no period).
        for cusip, ticker, periods, value, owners, gvkey, iid, current in con.execute(f"""
                WITH r AS (SELECT m.cusip, m.period, m.value, m.candidate_ticker, s.gvkey, s.iid,
                                  coalesce(b.refused, false) AS cur
                           FROM sec13f_cusip_map m LEFT JOIN _sp_security s ON s.ticker = m.candidate_ticker
                           LEFT JOIN _m_cur b ON b.cusip = m.cusip AND b.period = m.period
                           WHERE m.map_source = '{REFUSED}'),
                     w AS (SELECT r.cusip, string_agg(DISTINCT o.gvkey || '/' || coalesce(o.iid, '?'), ', '
                                                      ORDER BY 1) AS owners
                           FROM r JOIN _owners o ON o.cusip = r.cusip
                            AND NOT (o.gvkey = r.gvkey AND o.iid IS NOT DISTINCT FROM r.iid)
                            AND (NOT r.cur OR ((o.efffrom IS NULL OR o.efffrom <= r.period)
                                               AND (o.effthru IS NULL OR r.period <= o.effthru)))
                           GROUP BY r.cusip)
                SELECT r.cusip, any_value(r.candidate_ticker), count(DISTINCT r.period), sum(r.value),
                       any_value(w.owners), any_value(r.gvkey), any_value(r.iid), bool_or(r.cur)
                FROM r LEFT JOIN w ON w.cusip = r.cusip GROUP BY r.cusip ORDER BY r.cusip""").fetchall():
            names = "the spine's current CUSIP names" if current else "OpenFIGI names"
            when = " in those periods" if current else ""
            found.append(("cusip_owner_differs", cusip,
                          f"{names} {ticker} ({gvkey}/{iid}); Compustat gives the CUSIP to {owners}{when}: "
                          f"{periods} period(s), ${value or 0:,.0f} kept unnamed"))
        for cusip, period, issuer, value, holders in con.execute(f"""
                SELECT cusip, period, issuer_name, value, holders FROM sec13f_cusip_map
                WHERE period = (SELECT coalesce(max(period) FILTER (WHERE filing_deadline < ?), max(period))
                                FROM sec13f_periods) AND map_source IS NULL
                ORDER BY value DESC NULLS LAST, cusip LIMIT {UNMAPPED_TOP}""", [as_of]).fetchall():
            found.append(("cusip_unmapped_large", cusip,
                          f"{period}: {issuer or '?'}, ${value or 0:,.0f} held by {holders} filer(s); "
                          f"no spine source names it"))
        for cik, period, name, factor, ratio, names, value in con.execute(f"""
                SELECT cik, period, filer_name, value_factor, median_ratio, names, value FROM sec13f_filer_period
                WHERE value_factor <> 1 ORDER BY abs(value - value / value_factor) DESC, cik, period
                LIMIT {RESCALED_TOP}""").fetchall():
            how = "read in thousands" if factor > 1 else "read as 1,000 times too high"
            found.append(("value_rescaled", f"{cik}|{period}",
                          f"{name or cik}, {period}: VALUE {how} (median ratio {ratio:g} to the 13F's own prices over "
                          f"{names} names): ${value / factor:,.0f} read as ${value:,.0f}"))
        for day, n, acc, why in con.execute(f"""
                SELECT index_day, count(*), min(accession), any_value(detail) FROM {sec13f.TABLE}
                WHERE status = 'unparsed' GROUP BY index_day ORDER BY index_day""").fetchall():
            found.append(("filings_unparsed", str(day), f"{n} 13F filing(s) unparsed, e.g. {acc}: {why}"))
        return found

    @staticmethod
    def _summary(con: duckdb.DuckDBPyConnection, as_of: date) -> dict:
        """The plan's two counts, for the newest complete quarter (its deadline passed), else the newest one."""
        rows = con.execute("SELECT period, filing_deadline, coverage FROM sec13f_periods "
                           "ORDER BY period DESC").fetchall()
        if not rows:
            return {}
        period, _deadline, coverage = next((r for r in rows if r[1] is not None and r[1] < as_of), rows[0])
        held, members = con.execute(
            "SELECT count(*) FILTER (WHERE EXISTS (SELECT 1 FROM sec13f_ticker_period t "
            "WHERE t.ticker = s.ticker AND t.period = ?)), count(*) FROM _sp_sp500 s", [period]).fetchone()
        return {"period": period, "coverage": coverage, "sp500_held": held, "sp500": members}

    # ── Check and issues ───────────────────────────────────────────────────────────────────────────────────────

    def check(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict:
        table = "sec13f_ticker_period"
        added, removed = base.compare_with_live(con, KIND, table, ["ticker", "period", "shares", "holders"])
        summary = (ctx.pending.get(NAME) or {}).get("summary") or {}
        if summary.get("period"):
            p = summary["period"].isoformat()
            ctx.notes.append(f"S&P 500 names with 13F holders in {p}: {summary['sp500_held']} of {summary['sp500']}")
            if summary["coverage"] is not None:
                ctx.notes.append(f"13F value mapped in {p}: {summary['coverage'] * 100:.2f}%")
        elif not common.count(con, table):
            ctx.notes.append("no 13F totals: no holdings yet, or no spine to name them")
        if summary.get("rescaled"):
            ctx.notes.append(f"{summary['rescaled']} filer-quarters' VALUE off by a factor of 1,000 against the 13F's "
                             "own prices: rescaled")
        if summary.get("left_out"):
            n, quarters, first = summary["left_out"]
            ctx.notes.append(f"{n} filings of {quarters} quarters whose filing deadline passed before the first 13F "
                             f"day ({first}) left out")
        newest = con.execute("SELECT max(filed_through) FROM sec13f_periods").fetchone()[0]
        return {"dataset": NAME, "result": "changed" if added or removed else "unchanged",
                "source_as_of": (newest or ctx.as_of or self.default_as_of()).isoformat(),
                "rows": common.count(con, table), "added": added, "removed": removed,
                "coverage": summary.get("coverage")}

    def after_swap(self, ctx: base.RunContext) -> None:
        pending = ctx.pending.get(NAME)
        if pending is None:
            return                                  # the 13F tables were carried over: their issues stand
        ops.sync_issues(NAME, ISSUE_KINDS, pending["found"], run_id=ctx.run_id)


DATASET = OwnershipServe()
