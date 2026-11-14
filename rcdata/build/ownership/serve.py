"""``ownership/serve``: what the ownership store derives from its sources and the spine, built last of the kind.

**13F totals**, over the filings ``sec/13f`` typed (``status`` ok) and their holdings in Parquet:

- *The amendment rule* (the SEC's Form 13F instructions; the ownership review of 2026-10-02, Q1): per filer and
  report period, in filed order, a holdings report (13F-HR) starts the period's set, a report of amendment type
  ``NEW HOLDINGS`` adds to it (whether or not its form carries the ``/A``: one 13F-HR of 9,501 in a real set does
  not, and read as a new report it replaced the quarter with its few names; 13F review B1, 2026-10-30), and any
  other amendment (``RESTATEMENT``, or none stated) replaces it. An amendment belongs to the period it reports on,
  not to the quarter it was filed in.
- *Long equity only*: ``sh_type`` ``SH`` and no put or call. Principal amounts and options stay in Parquet.
- *The period is the calendar quarter*: a report period that is not a quarter end (a filer writes 09-29 or 10-01
  for the September quarter) counts in the nearest quarter end (``common.quarter_end``); the filing keeps the
  period as written. Read as written it was a quarter of its own: one filer, breadth 100 %, never paired with the
  filer's other quarters (13F review B2, 2026-10-30).
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
  divided by 1,000 (0.001). Shares never change. The test runs on every filing, before the rule change too
  (#780): a filer that wrote dollars while the rule still said thousands was read 1,000 times too high, and a few
  of them were enough to put the pre-2023 quarters' total value at $200-360T and AAPL's implied price at 6-41
  times its close. Three more guards (#780, after that fix ran live): a filer off by 1,000,000 (median ratio
  inside the band around it, ``SCALE_STEPS``) is rescaled the same way; a CUSIP's consensus price 300-3,000 times
  off **both** quarters beside it is moved back by 1,000 first (2022 Q3, when most holders already wrote dollars
  that were read as thousands); and one holding 300 times or more (``ROW_BAND``) from its consensus price, after its
  filer's factor, is read at that price (``value_row_replaced`` on the issues board).
- *Each CUSIP named*, per report period, by the first spine source that answers: (1) Compustat's dated identifier
  history (``security_history``, field ``cusip``, the period inside ``valid_from``..``valid_to``) with exactly one
  ticker: ``wrds/sec_idhist``; (2) the spine's current CUSIP of exactly one ticker: ``spine/security``; (2b) when
  (2) is refused or absent, Compustat's owner of the CUSIP in the period (``comp.sec_idhist`` ``efffrom``..``effthru``,
  or a security's own CUSIP with no dates), when exactly one security owns it then and that security is exactly one
  spine ticker: ``compustat/owner`` (#278: a CUSIP Compustat moved to another security after the quarter, or a
  spine row whose own CUSIP differs or is empty); (3) the spine's OpenFIGI map, when its ticker is a spine ticker:
  ``openfigi/map`` (the map asks the newest two quarters' 13F CUSIPs too, a CINS as ``ID_CINS``). A CUSIP is never
  named from an issuer name.
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
- *Continuing filers only* (#732, owner 12 Nov 2026): new and exited holders and the buy and sell flow count only the
  filers with a holdings report in **both** quarters. A filer with no report in the quarter before (a first report,
  or one back after a gap) is not buying, and one with no report in the quarter is not selling: they are counted
  apart, ``started_filers`` and ``started_shares``, and ``exited_no_filing`` with ``stopped_shares``. Before, a filer
  that crossed the reporting threshold read as a new holder and a buy of its whole position. Each such filer, per
  ticker and settled quarter, is a row of ``sec13f_filer_breaks`` (kind ``started`` or ``stopped``, its shares), so
  the board can list where the series broke.
- *Completeness*: ``filing_deadline`` is the period end plus 45 days, rolled past weekends and Washington's Birthday
  (``filing_deadline``). ``filed_through`` is its newest filing. A quarter is ``closed`` once the SEC has published
  the 13F data set whose filing window holds its deadline (``sec/13f_sets_listed``; ``sec13f_periods.data_set`` and
  ``data_set_seen`` name it and the day it was first listed; owner, 12 Nov 2026, #732). Before the store has read the
  SEC's data-set page even once, ``closed`` is NULL and a reader falls back to the deadline.

**Tables**: ``sec13f_cusip_map`` (CUSIP and period: its ticker and how, its long-equity value and holders),
``sec13f_filer_period`` (filer and period: its long-equity names and value, ``value_factor``), ``sec13f_ticker_period``
(the metrics), ``sec13f_periods`` (per period: filers, value, value mapped, coverage), ``sec13f_filer_breaks``
(ticker, settled period and filer: a filer that started or stopped reporting, with its shares) and
``sec13f_type_holdings`` (investor type and period: its ``TYPE_TOP`` largest names and the rest, #760).

- *Investor types* (#760, owner 12 Nov 2026): a filer's types are the ``tags`` Unusual Whales gives its CIK in the
  newest ``uw_institution`` snapshot (``value_investor``, ``energy``, ``tiger_club``, ...; a filer can carry several),
  plus ``all`` for every filer. Per type and quarter the type's combined portfolio is the long-equity value of its
  filers' holdings that map to a ticker; each of its ``TYPE_TOP`` largest tickers is a row with its ``share`` (%) of
  that portfolio, and the rest is one row with ``ticker`` NULL. A ticker outside the top in a quarter is in that
  quarter's rest, so every quarter's shares add up to 100. One snapshot types every quarter: a filer's type is what
  UW says it is now, not what it was then.

**Views** under the old ``raw.duckdb`` names, with the old columns (D4): ``sec13f_totals`` (ticker, ``entry`` JSON
``{period: {shares, value, holders, ...}}``) and ``short_interest``: one row per ticker and session (an IBKR snapshot
session or the day a FINRA settlement date becomes visible, ``FINRA_VISIBLE_DAY``) with the newest FINRA short
interest published before it and that session's IBKR borrow fee, rebate and shares available. ``si_float`` and
``total_float`` have no source here and are NULL.

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
from .. import base, dirty
from ..spine import openfigi_map, wrds
from . import KIND, common, sec13f, sec13f_listing, uw_institutions

NAME = "ownership/serve"
SPINE = "spine"
HIST, CURRENT, FIGI, REFUSED = "wrds/sec_idhist", "spine/security", openfigi_map.NAME, "refused"
OWNER = "compustat/owner"
CONSENSUS_FILERS = 3
MIN_NAMES = 5
SCALE_BAND = (1.0 / 3000.0, 1.0 / 300.0)
SCALE_STEPS = (1000.0, 1_000_000.0)      # a filer off by one or two steps of 1,000 (#780: 2014 Q2, 2018 Q2 x1,000,000)
ROW_BAND = 300.0                         # one holding this far from its consensus price is read at that price (#780)
NEIGHBOUR_DAYS = 100                     # a consensus price is checked against the quarters on either side (#780)
ROWS_TOP = 50
DEADLINE_DAYS = 45
UNMAPPED_TOP = 50
RESCALED_TOP = 50
SP500, FUNDS = "sp500", "funds/index_holdings"
QE = common.quarter_end("period")
PREV = "CAST(date_trunc('quarter', p.period) - INTERVAL 1 DAY AS DATE)"     # the calendar quarter before p's

MAP = [("cusip", "TEXT"), ("period", "DATE"), ("ticker", "TEXT"), ("map_source", "TEXT"),
       ("candidate_ticker", "TEXT"), ("issuer_name", "TEXT"), ("value", "DOUBLE"), ("holders", "INTEGER")]
FILER = [("cik", "TEXT"), ("period", "DATE"), ("filer_name", "TEXT"), ("filings", "INTEGER"), ("cusips", "INTEGER"),
         ("value", "DOUBLE"), ("names", "INTEGER"), ("median_ratio", "DOUBLE"), ("value_factor", "DOUBLE"),
         ("confidential_omitted", "BOOLEAN")]
TICKER = [("ticker", "TEXT"), ("period", "DATE"), ("shares", "DOUBLE"), ("value", "DOUBLE"), ("holders", "INTEGER"),
          ("top10_pct", "DOUBLE"), ("hhi", "DOUBLE"), ("new_holders", "INTEGER"), ("exited_holders", "INTEGER"),
          ("exited_confidential", "INTEGER"), ("exited_no_filing", "INTEGER"), ("buy_flow", "DOUBLE"),
          ("sell_flow", "DOUBLE"), ("started_filers", "INTEGER"), ("started_shares", "DOUBLE"),
          ("stopped_shares", "DOUBLE"), ("breadth", "DOUBLE"), ("breadth_chg", "DOUBLE"),
          ("prev_period", "DATE"), ("filers", "INTEGER"), ("cusips", "INTEGER"), ("filing_deadline", "DATE"),
          ("filed_through", "DATE"), ("closed", "BOOLEAN"), ("source", "TEXT")]
PERIODS = [("period", "DATE"), ("filers", "INTEGER"), ("filings_active", "INTEGER"), ("value_total", "DOUBLE"),
           ("value_mapped", "DOUBLE"), ("coverage", "DOUBLE"), ("unmapped_cusips", "INTEGER"),
           ("refused_value", "DOUBLE"), ("rescaled_filers", "INTEGER"), ("filing_deadline", "DATE"),
           ("filed_through", "DATE"), ("data_set", "TEXT"), ("data_set_seen", "DATE"), ("closed", "BOOLEAN")]
BREAKS = [("ticker", "TEXT"), ("period", "DATE"), ("cik", "TEXT"), ("filer_name", "TEXT"), ("kind", "TEXT"),
          ("shares", "DOUBLE")]
TYPES = [("tag", "TEXT"), ("period", "DATE"), ("ticker", "TEXT"), ("rank", "INTEGER"), ("value", "DOUBLE"),
         ("holders", "INTEGER"), ("share", "DOUBLE"), ("type_value", "DOUBLE"), ("type_filers", "INTEGER"),
         ("snapshot_date", "DATE")]
TYPE_TOP = 25                            # names per investor type and quarter; the rest is one row (#760). The
                                         # board draws six: 25 keeps a name it draws from reading 0 in a quarter
                                         # where it ranked 11th
ALL_TYPE = "all"                         # every filer, whatever UW tags it
STARTED, STOPPED = "started", "stopped"
SCHEMAS = {"sec13f_cusip_map": MAP, "sec13f_filer_period": FILER, "sec13f_ticker_period": TICKER,
           "sec13f_periods": PERIODS, "sec13f_filer_breaks": BREAKS, "sec13f_type_holdings": TYPES}
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
    "value_rescaled": "one of the largest filer-quarters whose VALUE was off by a factor of 1,000 (or 1,000,000) and "
                      "was rescaled",
    "value_row_replaced": "one of the largest holdings whose value lay 300 times or more from the 13F's own price "
                          "for it and was read at that price",
}

# FINRA publishes a settlement date's short interest on the seventh business day after it (FINRA's equity short
# interest page), after the close, so a session sees it from the eighth (#252: the bridge view read it from the
# settlement date itself, a week early). A business day skips the NYSE's and the federal closures, the later of the
# two readings.
FINRA_VISIBLE_DAY = 8
_CLOSED = ", ".join(f"DATE '{d}'" for d in sorted(set().union(
    *(calendar.market_holidays(y) | calendar.federal_holidays(y) for y in calendar.MARKET_HOLIDAYS))))

VIEWS = {
    "sec13f_totals": (
        "SELECT ticker, CAST(json_group_object(strftime(period, '%Y-%m-%d'), json_object("
        + ", ".join(f"'{m}', {m}" for m in METRICS) + ")) AS JSON) AS entry "
        "FROM sec13f_ticker_period GROUP BY ticker"),
    "short_interest": f"""
        WITH f0 AS (
            SELECT ticker, settlement_date, current_short, days_to_cover, fetched_at, run_id
            FROM finra_short_interest WHERE ticker IS NOT NULL AND settlement_date IS NOT NULL
            QUALIFY row_number() OVER (PARTITION BY ticker, settlement_date
                                       ORDER BY (symbol = ticker) DESC, symbol) = 1),
        p AS (
            SELECT settlement_date, CAST(day AS DATE) AS visible_from FROM (
                SELECT settlement_date,
                       unnest(generate_series(CAST(settlement_date AS TIMESTAMP) + INTERVAL 1 DAY,
                                              CAST(settlement_date AS TIMESTAMP) + INTERVAL 31 DAY,
                                              INTERVAL 1 DAY)) AS day
                FROM (SELECT DISTINCT settlement_date FROM f0))
            WHERE isodow(day) <= 5 AND CAST(day AS DATE) NOT IN ({_CLOSED})
            QUALIFY row_number() OVER (PARTITION BY settlement_date ORDER BY day) = {FINRA_VISIBLE_DAY}),
        f AS (SELECT f0.*, p.visible_from FROM f0 JOIN p USING (settlement_date)),
        i AS (
            SELECT ticker, session_date, fee_rate, rebate_rate, available, fetched_at, run_id
            FROM ibkr_shortable WHERE ticker IS NOT NULL AND session_date IS NOT NULL
            QUALIFY row_number() OVER (PARTITION BY ticker, session_date
                                       ORDER BY snapshot_at DESC, fetched_at DESC, (symbol = ticker) DESC,
                                                symbol) = 1),
        k AS (SELECT ticker, session_date FROM i UNION SELECT ticker, visible_from FROM f)
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
        ASOF LEFT JOIN f ON f.ticker = k.ticker AND k.session_date >= f.visible_from
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


def _closed(con: duckdb.DuckDBPyConnection) -> None:
    """``_closed(period, data_set, data_set_seen, closed)`` (TEMP) for every period of ``_deadline``: the SEC data set
    whose filing window holds the period's deadline, the day it was first listed, and whether the period is closed
    (#732). With no listing read yet (``sec13f_data_sets`` absent or empty) ``closed`` is NULL: unknown, not open."""
    have = {r[0] for r in con.execute("SELECT table_name FROM duckdb_tables() "
                                      "WHERE database_name = current_database()").fetchall()}
    _empty(con, "_sets", sec13f_listing.SCHEMA)
    if sec13f_listing.TABLE in have:
        con.execute(f"INSERT INTO _sets SELECT * FROM {sec13f_listing.TABLE}")
    known, oldest = con.execute("SELECT count(*) > 0, min(first_day) FROM _sets").fetchone()
    # A deadline before the oldest set the page lists is closed by age: the SEC publishes no set that far back.
    con.execute(f"""CREATE OR REPLACE TEMP TABLE _closed AS
        SELECT d.period, min(s.name) AS data_set, min(s.first_seen) AS data_set_seen,
               CASE WHEN {bool(known)} THEN count(s.name) > 0 OR d.deadline < ? END AS closed
        FROM _deadline d LEFT JOIN _sets s ON d.deadline BETWEEN s.first_day AND s.last_day
        GROUP BY d.period, d.deadline""", [oldest])


def _empty(con: duckdb.DuckDBPyConnection, name: str, schema: list[tuple[str, str]]) -> None:
    common.temp_rows(con, name, schema, [])


def _periods_of(con: duckdb.DuckDBPyConnection, table: str) -> list:
    return [r[0] for r in con.execute(f"SELECT DISTINCT period FROM {table} ORDER BY period").fetchall()]


def _factor_sql() -> str:
    """A filer-quarter's ``value_factor`` from its median ratio to the consensus prices: one or two steps of 1,000 up
    or down when the ratio lies inside ``SCALE_BAND`` around that step and rests on ``MIN_NAMES`` names, else 1."""
    lo, hi = SCALE_BAND
    arms = []
    for step in SCALE_STEPS:
        arms.append(f"WHEN names >= {MIN_NAMES} AND ratio > {lo * 1000.0 / step!r} AND ratio < {hi * 1000.0 / step!r} "
                    f"THEN {step!r}")
        arms.append(f"WHEN names >= {MIN_NAMES} AND ratio > {step / (hi * 1000.0)!r} "
                    f"AND ratio < {step / (lo * 1000.0)!r} THEN {1.0 / step!r}")
    return "CASE " + " ".join(arms) + " ELSE 1.0 END"


def _consensus_guard(con: duckdb.DuckDBPyConnection) -> None:
    """A CUSIP's consensus price for a quarter that lies 300-3,000 times above or below BOTH quarters beside it (each
    within ``NEIGHBOUR_DAYS``) is moved back by 1,000; the moves are kept in ``_pxfix``. Both sides must agree, so a
    clean quarter next to a broken one is never moved, and the first and newest quarters are never moved.

    #780, 2022 Q3: filers could already write dollars while their filings, made before 2023-01-03, were still read
    in thousands; where most holders of a name did, the median price was 1,000 times too high, and a filer who wrote
    thousands as ruled (Sanders Capital) looked 1,000 times too low and was rescaled to $36T."""
    lo, hi = SCALE_BAND
    con.execute(f"""CREATE OR REPLACE TEMP TABLE _pxfix AS
        WITH n AS (
            SELECT cusip, period, px,
                   CASE WHEN period - lag(period) OVER w <= {NEIGHBOUR_DAYS} THEN lag(px) OVER w END AS before,
                   CASE WHEN lead(period) OVER w - period <= {NEIGHBOUR_DAYS} THEN lead(px) OVER w END AS after
            FROM _px WINDOW w AS (PARTITION BY cusip ORDER BY period)),
        r AS (SELECT cusip, period, px, px / before AS rb, px / after AS ra FROM n
              WHERE before > 0 AND after > 0)
        SELECT cusip, period, px AS old_px,
               CASE WHEN rb > {1 / hi!r} THEN px / 1000.0 ELSE px * 1000.0 END AS px
        FROM r
        WHERE (rb > {1 / hi!r} AND rb < {1 / lo!r} AND ra > {1 / hi!r} AND ra < {1 / lo!r})
           OR (rb > {lo!r} AND rb < {hi!r} AND ra > {lo!r} AND ra < {hi!r})""")
    con.execute("UPDATE _px SET px = f.px FROM _pxfix f WHERE _px.cusip = f.cusip AND _px.period = f.period")


def _successions(con: duckdb.DuckDBPyConnection) -> None:
    """``_succ`` (TEMP): ``predecessor`` and ``successor`` (13F CIKs, as filed) and the ``period`` the successor
    took over its reporting, from UW's newest succession snapshot (``uw/institutions``, #775).

    A pair counts only when the filings say the same: the predecessor has a holdings report in the quarter before
    and none in ``period``, and the successor has one in ``period``. A succession whose date does not line up with
    the stop stays a stop, so a real exit and a mis-dated hand-over are never merged silently (#776). Without the
    table (UW not read yet) the set is empty and every stop stays a stop."""
    _empty(con, "_succ", [("predecessor", "TEXT"), ("successor", "TEXT"), ("period", "DATE")])
    table = uw_institutions.SUCCESSION_TABLE
    if not common.has_table(con, table):
        return
    con.execute(f"""INSERT INTO _succ
        WITH u AS (
            SELECT DISTINCT ltrim(predecessor_cik, '0') AS pre, ltrim(successor_cik, '0') AS suc,
                   {common.quarter_end('effective_report_date')} AS period
            FROM {table} WHERE snapshot_date = (SELECT max(snapshot_date) FROM {table})
              AND predecessor_cik IS NOT NULL AND successor_cik IS NOT NULL AND effective_report_date IS NOT NULL),
        k AS (SELECT DISTINCT cik, ltrim(cik, '0') AS bare, period FROM _conf)
        SELECT DISTINCT kp.cik, ks.cik, u.period
        FROM u
        JOIN k kp ON kp.bare = u.pre AND kp.period = CAST(date_trunc('quarter', u.period) - INTERVAL 1 DAY AS DATE)
        JOIN k ks ON ks.bare = u.suc AND ks.period = u.period
        WHERE NOT EXISTS (SELECT 1 FROM k WHERE k.bare = u.pre AND k.period = u.period)""")


def _by_period(con: duckdb.DuckDBPyConnection, periods: list, table: str, select: str,
               schema: list[tuple[str, str]] | None = None) -> None:
    """``table`` filled by ``select`` (its one ``?`` the period) one quarter at a time, as a TEMP table of ``schema``
    when one is given (else it exists already). Every caller groups or joins by period, so the rows are the same as
    one statement over all quarters; what is not, is the memory: a median's or a DISTINCT's state and a join's build
    side sit partly outside DuckDB's memory_limit, and over every quarter at once the whole build of queue #242 was
    OOM-killed at 7.2 GiB under a 5 GB limit (10 Nov 2026, in the CUSIP map after ``sec13f_filer_period``)."""
    if schema is not None:
        _empty(con, table, schema)
    for p in periods:
        con.execute(f"INSERT INTO {table} {select}", [p] * select.count("?"))


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


TEMPS = ("_closed", "_sets", "_sp_security", "_sp_hist", "_sp_sp500", "_owners", "_figi", "_active", "_conf", "_long", "_px", "_ratio",
         "_pxfix", "_fac", "_rowfix", "_held", "_succ",
         "_cp", "_m_hist", "_m_cur", "_m_owner", "_m_figi", "_filers", "_filed", "_deadline", "_tot", "_pos", "_shape",
         "_pairs", "_flow", "_tag")


class OwnershipServe:
    #: #812: a whole rebuild took 2,352-2,540 s with a 22.9-23.8 GB DuckDB spill peak (homesrv, 12 Nov 2026).
    heavy = True
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

    def dirty(self, ctx: base.RunContext):
        """Everything: the 13F tables are derived whole whenever they are derived (on a run of ``sec/13f`` or of
        this dataset), on the live copy too; on any other run they are the live file's own, kept as they are.
        shortcut: a 13F run derives every quarter again although a day's filings reach only their own quarter and
        the next (its flow); upgrade to a per-quarter reload when that derive is what a build waits on."""
        return dirty.ALL

    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        on_live = getattr(ctx, "incremental", None) is not None
        derive = ctx.dataset in (NAME, sec13f.NAME)
        counts = None
        if not derive:
            counts = self._kept(con) if on_live else self._carry_over(con)
        if counts is None:
            if on_live:
                for view in VIEWS:
                    con.execute(f"DROP VIEW IF EXISTS {view}")
                for t in SCHEMAS:
                    con.execute(f"DROP TABLE IF EXISTS {t}")
            counts = self._derive(ctx, con)
        for view, sql in VIEWS.items():
            con.execute(f"CREATE OR REPLACE VIEW {view} AS {sql}")
        return counts

    @staticmethod
    def _kept(con: duckdb.DuckDBPyConnection) -> dict[str, int] | None:
        """The 13F tables the live copy already holds, or None when they are not of this shape."""
        cols: dict[str, list[str]] = {}
        for t, c in con.execute("SELECT table_name, column_name FROM duckdb_columns() "
                                "WHERE database_name = current_database() ORDER BY table_name, column_index"
                                ).fetchall():
            cols.setdefault(t, []).append(c)
        if any(cols.get(t) != [c for c, _ in s] for t, s in SCHEMAS.items()):
            return None
        return {t: common.count(con, t) for t in SCHEMAS}

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
            counts["sec13f_filer_breaks"] = self._breaks(con, ctx.as_of or self.default_as_of())
            counts["sec13f_type_holdings"] = self._types(con)
            found += self._issues(con, ctx.as_of or self.default_as_of())
            summary["rescaled"] = common.count(con, "sec13f_filer_period", "value_factor <> 1")
            summary["px_fixed"] = common.count(con, "_pxfix")
            summary["rows_replaced"] = common.count(con, "_rowfix")
            summary["succeeded"] = common.count(con, "_succ")
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
        rows = con.execute(f"SELECT accession, cik, {QE} AS period, filed, coalesce(amendment_no, 0), is_amendment, "
                           f"amendment_type FROM {sec13f.TABLE} WHERE status = 'ok' AND cik IS NOT NULL "
                           f"AND period IS NOT NULL ORDER BY cik, 3, filed, 5, is_amendment, accession").fetchall()
        first = sec13f.DATASET.first_day() or min((r[3] for r in rows), default=None)
        served = [p for p in sorted({r[2] for r in rows}) if first is None or filing_deadline(p) >= first]
        start = served[0] if served else date.max
        left = [r for r in rows if r[2] < start]
        rows = [r for r in rows if r[2] >= start]
        common.temp_rows(con, "_active", [("accession", "TEXT")], [{"accession": a} for a in active_accessions(rows)])
        # A filer's holdings report for the period, and whether it left holdings out under confidential treatment.
        con.execute(f"""CREATE OR REPLACE TEMP TABLE _conf AS
            SELECT f.cik, {common.quarter_end('f.period')} AS period, bool_or(f.confidential_omitted) AS omitted
            FROM {sec13f.TABLE} f JOIN _active a ON a.accession = f.accession GROUP BY ALL""")
        long_cols = [("accession", "TEXT"), ("cik", "TEXT"), ("period", "DATE"), ("cusip", "TEXT"),
                     ("name_of_issuer", "TEXT"), ("shares", "DOUBLE"), ("value_usd", "DOUBLE"),
                     ("value_scale", "INTEGER"), ("filer_name", "TEXT")]
        _empty(con, "_long", long_cols)
        if files:
            con.execute(f"""INSERT INTO _long
                SELECT h.accession, h.cik, {common.quarter_end('h.period')}, upper(h.cusip), h.name_of_issuer,
                       h.shares, h.value_usd, f.value_scale, f.filer_name
                FROM {common.files_sql(files)} h
                JOIN _active a ON a.accession = h.accession
                JOIN {sec13f.TABLE} f ON f.accession = h.accession
                WHERE upper(h.sh_type) = 'SH' AND coalesce(trim(h.put_call), '') = '' AND h.cusip IS NOT NULL
                  AND h.cik IS NOT NULL AND h.period IS NOT NULL""")
        periods = _periods_of(con, "_long")
        _by_period(con, periods, "_px", f"""
            SELECT cusip, period, median(value_usd / shares) FROM _long
            WHERE period = ? AND shares > 0 AND value_usd > 0
            GROUP BY ALL HAVING count(DISTINCT cik) >= {CONSENSUS_FILERS}""",
                   [("cusip", "TEXT"), ("period", "DATE"), ("px", "DOUBLE")])
        _consensus_guard(con)
        _by_period(con, periods, "_ratio", """
            SELECT l.cik, l.period, count(DISTINCT l.cusip), median(l.value_usd / l.shares / p.px)
            FROM _long l JOIN _px p ON p.cusip = l.cusip AND p.period = l.period
            WHERE l.period = ? AND l.shares > 0 AND l.value_usd > 0 GROUP BY ALL""",
                   [("cik", "TEXT"), ("period", "DATE"), ("names", "BIGINT"), ("ratio", "DOUBLE")])
        con.execute(f"""CREATE OR REPLACE TEMP TABLE _fac AS
            SELECT cik, period, names, ratio, {_factor_sql()} AS factor FROM _ratio""")
        # One holding far from its price inside a filer whose median is clean (#780: 2018 Q3, a few lines of one
        # filer worth $258T), or in a filer with too few names for the median test, is read at the consensus price.
        _by_period(con, periods, "_rowfix", f"""
            SELECT l.rowid, l.cik, l.period, l.cusip, min(l.filer_name), l.value_usd * coalesce(x.factor, 1.0),
                   l.shares * p.px, coalesce(x.factor, 1.0)
            FROM _long l JOIN _px p ON p.cusip = l.cusip AND p.period = l.period
            LEFT JOIN _fac x ON x.cik = l.cik AND x.period = l.period
            WHERE l.period = ? AND l.shares > 0 AND l.value_usd > 0
              AND (l.value_usd * coalesce(x.factor, 1.0) > {ROW_BAND!r} * l.shares * p.px
                   OR l.value_usd * coalesce(x.factor, 1.0) * {ROW_BAND!r} < l.shares * p.px)
            GROUP BY ALL""",
                   [("rid", "BIGINT"), ("cik", "TEXT"), ("period", "DATE"), ("cusip", "TEXT"),
                    ("filer_name", "TEXT"), ("old_value", "DOUBLE"), ("new_value", "DOUBLE"), ("factor", "DOUBLE")])
        con.execute("UPDATE _long SET value_usd = r.new_value / r.factor FROM _rowfix r WHERE _long.rowid = r.rid")
        common.create_table(con, "sec13f_filer_period", FILER, f"""
            SELECT l.cik, l.period, max(l.filer_name) AS filer_name, count(DISTINCT l.accession) AS filings,
                   count(DISTINCT l.cusip) AS cusips, sum(l.value_usd) * coalesce(x.factor, 1) AS value, x.names,
                   round(x.ratio, 6) AS median_ratio, coalesce(x.factor, 1) AS value_factor,
                   c.omitted AS confidential_omitted
            FROM _long l
            LEFT JOIN _fac x ON x.cik = l.cik AND x.period = l.period
            LEFT JOIN _conf c ON c.cik = l.cik AND c.period = l.period
            GROUP BY l.cik, l.period, x.factor, x.names, x.ratio, c.omitted ORDER BY l.period, l.cik""")
        _by_period(con, periods, "_held", """
            SELECT l.cik, l.period, l.cusip, sum(l.shares), sum(l.value_usd * p.value_factor), min(l.name_of_issuer)
            FROM _long l JOIN sec13f_filer_period p ON p.cik = l.cik AND p.period = l.period
            WHERE l.period = ? GROUP BY ALL""",
                   [("cik", "TEXT"), ("period", "DATE"), ("cusip", "TEXT"), ("shares", "DOUBLE"),
                    ("value", "DOUBLE"), ("issuer_name", "TEXT")])
        return (len(left), len({r[2] for r in left}), first) if left else None

    @staticmethod
    def _map(con: duckdb.DuckDBPyConnection) -> int:
        periods = _periods_of(con, "_held")
        _by_period(con, periods, "_cp", """
            SELECT cusip, period, sum(value), count(DISTINCT cik), min(issuer_name) FROM _held
            WHERE period = ? GROUP BY ALL""",
                   [("cusip", "TEXT"), ("period", "DATE"), ("value", "DOUBLE"), ("holders", "BIGINT"),
                    ("issuer_name", "TEXT")])
        _by_period(con, periods, "_m_hist", """
            SELECT c.cusip, c.period, min(h.ticker) FROM _cp c
            JOIN _sp_hist h ON h.cusip = c.cusip AND (h.valid_from IS NULL OR h.valid_from <= c.period)
                           AND (h.valid_to IS NULL OR c.period < h.valid_to)
            WHERE c.period = ? GROUP BY ALL HAVING count(DISTINCT h.ticker) = 1""",
                   [("cusip", "TEXT"), ("period", "DATE"), ("ticker", "TEXT")])
        # The current CUSIP has no period of its own: it is refused for a period in which Compustat's dated history
        # gives the CUSIP to another security (or gives it with no dates), as a reused CUSIP would be (#236).
        _by_period(con, periods, "_m_cur", """SELECT * FROM (
            WITH s AS (SELECT cusip, min(ticker) AS ticker, min(gvkey) AS gvkey, min(iid) AS iid FROM _sp_security
                       WHERE cusip IS NOT NULL GROUP BY ALL HAVING count(DISTINCT ticker) = 1)
            SELECT c.cusip, c.period, s.ticker,
                   s.gvkey IS NOT NULL AND EXISTS (
                       SELECT 1 FROM _owners o WHERE o.cusip = c.cusip
                       AND NOT (o.gvkey = s.gvkey AND o.iid IS NOT DISTINCT FROM s.iid)
                       AND (o.efffrom IS NULL OR o.efffrom <= c.period)
                       AND (o.effthru IS NULL OR c.period <= o.effthru)) AS refused
            FROM _cp c JOIN s ON s.cusip = c.cusip WHERE c.period = ?)""",
                   [("cusip", "TEXT"), ("period", "DATE"), ("ticker", "TEXT"), ("refused", "BOOLEAN")])
        # #278: the one security Compustat gives the CUSIP to in the period, when it is exactly one spine ticker.
        _by_period(con, periods, "_m_owner", """
            SELECT c.cusip, c.period, min(s.ticker) FROM _cp c
            JOIN _owners o ON o.cusip = c.cusip AND (o.efffrom IS NULL OR o.efffrom <= c.period)
                          AND (o.effthru IS NULL OR c.period <= o.effthru)
            LEFT JOIN _sp_security s ON s.gvkey = o.gvkey AND s.iid IS NOT DISTINCT FROM o.iid
            WHERE c.period = ? GROUP BY ALL
            HAVING count(DISTINCT o.gvkey || '/' || coalesce(o.iid, '')) = 1 AND count(DISTINCT s.ticker) = 1""",
                   [("cusip", "TEXT"), ("period", "DATE"), ("ticker", "TEXT")])
        con.execute("""CREATE OR REPLACE TEMP TABLE _m_figi AS
            SELECT f.cusip, f.ticker, s.gvkey, s.iid,
                   s.gvkey IS NOT NULL AND EXISTS (SELECT 1 FROM _owners o WHERE o.cusip = f.cusip)
                   AND NOT EXISTS (SELECT 1 FROM _owners o WHERE o.cusip = f.cusip AND o.gvkey = s.gvkey
                                   AND o.iid IS NOT DISTINCT FROM s.iid) AS refused
            FROM _figi f JOIN _sp_security s ON s.ticker = f.ticker""")
        common.create_table(con, "sec13f_cusip_map", MAP)
        _by_period(con, periods, "sec13f_cusip_map", f"""
            SELECT c.cusip, c.period,
                   coalesce(a.ticker, CASE WHEN NOT b.refused THEN b.ticker END, o.ticker,
                            CASE WHEN b.ticker IS NULL AND NOT f.refused THEN f.ticker END) AS ticker,
                   CASE WHEN a.ticker IS NOT NULL THEN '{HIST}' WHEN NOT b.refused THEN '{CURRENT}'
                        WHEN o.ticker IS NOT NULL THEN '{OWNER}'
                        WHEN b.refused THEN '{REFUSED}' WHEN NOT f.refused THEN '{FIGI}'
                        WHEN f.refused THEN '{REFUSED}' END AS map_source,
                   CASE WHEN a.ticker IS NULL AND o.ticker IS NULL AND b.refused THEN b.ticker
                        WHEN a.ticker IS NULL AND o.ticker IS NULL AND b.ticker IS NULL AND f.refused THEN f.ticker
                        END AS candidate_ticker,
                   c.issuer_name, c.value, c.holders
            FROM _cp c
            LEFT JOIN _m_hist a ON a.cusip = c.cusip AND a.period = c.period
            LEFT JOIN _m_cur b ON b.cusip = c.cusip AND b.period = c.period
            LEFT JOIN _m_owner o ON o.cusip = c.cusip AND o.period = c.period
            LEFT JOIN _m_figi f ON f.cusip = c.cusip
            WHERE c.period = ? ORDER BY c.cusip""")
        return common.count(con, "sec13f_cusip_map")

    @staticmethod
    def _metrics(con: duckdb.DuckDBPyConnection) -> int:
        held = _periods_of(con, "_held")
        _by_period(con, held, "_filers", "SELECT period, count(DISTINCT cik) FROM _held WHERE period = ? GROUP BY ALL",
                   [("period", "DATE"), ("filers", "BIGINT")])
        con.execute(f"CREATE OR REPLACE TEMP TABLE _filed AS SELECT {QE} AS period, max(filed) AS filed_through "
                    f"FROM {sec13f.TABLE} WHERE status = 'ok' AND period IS NOT NULL GROUP BY ALL")
        periods = sorted({r[0] for r in con.execute("SELECT period FROM _filers UNION SELECT period FROM _filed")
                          .fetchall()})
        common.temp_rows(con, "_deadline", [("period", "DATE"), ("deadline", "DATE")],
                         [{"period": p, "deadline": filing_deadline(p)} for p in periods])
        _closed(con)
        # One position per ticker, period and filer (a ticker's CUSIPs summed).
        _by_period(con, held, "_pos", """
            SELECT m.ticker, h.period, h.cik, sum(h.shares)
            FROM _held h JOIN sec13f_cusip_map m ON m.cusip = h.cusip AND m.period = h.period
            WHERE h.period = ? AND m.ticker IS NOT NULL GROUP BY ALL""",
                   [("ticker", "TEXT"), ("period", "DATE"), ("cik", "TEXT"), ("shares", "DOUBLE")])
        _by_period(con, held, "_tot", """
            SELECT m.ticker, h.period, sum(h.shares), sum(h.value), count(DISTINCT h.cik), count(DISTINCT h.cusip),
                   CAST(date_trunc('quarter', h.period) - INTERVAL 1 DAY AS DATE)
            FROM _held h JOIN sec13f_cusip_map m ON m.cusip = h.cusip AND m.period = h.period
            WHERE h.period = ? AND m.ticker IS NOT NULL GROUP BY m.ticker, h.period""",
                   [("ticker", "TEXT"), ("period", "DATE"), ("shares", "DOUBLE"), ("value", "DOUBLE"),
                    ("holders", "BIGINT"), ("cusips", "BIGINT"), ("prev_period", "DATE")])
        _by_period(con, held, "_shape", """
            SELECT p.ticker, p.period, sum(p.shares) FILTER (WHERE p.rn <= 10),
                   sum(CASE WHEN t.shares > 0 THEN pow(p.shares / t.shares, 2) END)
            FROM (SELECT *, row_number() OVER (PARTITION BY ticker, period ORDER BY shares DESC, cik) AS rn
                  FROM _pos WHERE period = ?) p
            JOIN _tot t ON t.ticker = p.ticker AND t.period = p.period GROUP BY ALL""",
                   [("ticker", "TEXT"), ("period", "DATE"), ("top10", "DOUBLE"), ("h", "DOUBLE")])
        _successions(con)
        # Each filer's position against its position in the calendar quarter before (FULL JOIN: new and exited),
        # per quarter a pair lands in: a quarter held, or the one after it. A filer succeeded in a quarter (``_succ``)
        # is its successor in the quarter before, so the two are one filer across the hand-over (#776).
        nxt = "CAST(date_trunc('quarter', period) + INTERVAL 6 MONTH - INTERVAL 1 DAY AS DATE)"
        pairs = sorted({r[0] for r in con.execute(f"SELECT period FROM _pos UNION SELECT {nxt} FROM _pos").fetchall()})
        _by_period(con, pairs, "_pairs", f"""
            SELECT coalesce(a.ticker, b.ticker), coalesce(a.period, b.next_period), coalesce(a.cik, b.cik),
                   a.shares, b.shares
            FROM (SELECT * FROM _pos WHERE period = ?) a
            FULL OUTER JOIN (SELECT ticker, coalesce(s.successor, q.cik) AS cik, sum(q.shares) AS shares,
                                    q.next_period
                             FROM (SELECT ticker, cik, shares, {nxt} AS next_period FROM _pos) q
                             LEFT JOIN _succ s ON s.predecessor = q.cik AND s.period = q.next_period
                             WHERE q.next_period = ? GROUP BY ALL) b
              ON a.ticker = b.ticker AND a.cik = b.cik AND a.period = b.next_period""",
                   [("ticker", "TEXT"), ("period", "DATE"), ("cik", "TEXT"), ("now_s", "DOUBLE"), ("prev_s", "DOUBLE")])
        # An exit may not be a sale: the filer left holdings out under confidential treatment, or has no holdings
        # report for the quarter (not filed yet, a notice that another manager reports them, or no longer filing).
        # Only a filer with a holdings report in both quarters trades (#732): ``c`` is its report of the quarter,
        # ``cp`` of the quarter before; a filer missing one of them started or stopped reporting.
        con.execute(f"""CREATE OR REPLACE TEMP TABLE _flow AS
            SELECT p.ticker, p.period,
                   count(*) FILTER (WHERE p.prev_s IS NULL AND cp.cik IS NOT NULL) AS new_holders,
                   count(*) FILTER (WHERE p.now_s IS NULL AND c.cik IS NOT NULL) AS exited_holders,
                   count(*) FILTER (WHERE p.now_s IS NULL AND c.omitted) AS exited_confidential,
                   count(*) FILTER (WHERE p.now_s IS NULL AND c.cik IS NULL) AS exited_no_filing,
                   coalesce(sum(greatest(coalesce(p.now_s, 0) - coalesce(p.prev_s, 0), 0))
                            FILTER (WHERE c.cik IS NOT NULL AND cp.cik IS NOT NULL), 0) AS buy,
                   coalesce(sum(greatest(coalesce(p.prev_s, 0) - coalesce(p.now_s, 0), 0))
                            FILTER (WHERE c.cik IS NOT NULL AND cp.cik IS NOT NULL), 0) AS sell,
                   count(*) FILTER (WHERE p.prev_s IS NULL AND cp.cik IS NULL) AS started_filers,
                   coalesce(sum(p.now_s) FILTER (WHERE p.prev_s IS NULL AND cp.cik IS NULL), 0) AS started_shares,
                   coalesce(sum(p.prev_s) FILTER (WHERE p.now_s IS NULL AND c.cik IS NULL), 0) AS stopped_shares
            FROM _pairs p LEFT JOIN _conf c ON c.cik = p.cik AND c.period = p.period
            LEFT JOIN (SELECT cik, period FROM _conf
                       UNION SELECT DISTINCT successor, {PREV} FROM _succ p) cp
                   ON cp.cik = p.cik AND cp.period = {PREV} GROUP BY ALL""")
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
                   CASE WHEN pf.filers IS NOT NULL THEN fl.started_filers END AS started_filers,
                   CASE WHEN pf.filers IS NOT NULL THEN round(fl.started_shares, 1) END AS started_shares,
                   CASE WHEN pf.filers IS NOT NULL THEN round(fl.stopped_shares, 1) END AS stopped_shares,
                   round(t.holders * 100.0 / f.filers, 4) AS breadth,
                   CASE WHEN pf.filers IS NOT NULL THEN round(round(t.holders * 100.0 / f.filers, 4)
                        - round(coalesce(pt.holders, 0) * 100.0 / pf.filers, 4), 4) END AS breadth_chg,
                   t.prev_period, f.filers, t.cusips, d.deadline AS filing_deadline, fd.filed_through,
                   cl.closed, '{NAME}' AS source
            FROM _tot t
            JOIN _shape s ON s.ticker = t.ticker AND s.period = t.period
            JOIN _filers f ON f.period = t.period
            LEFT JOIN _filers pf ON pf.period = t.prev_period
            LEFT JOIN _tot pt ON pt.ticker = t.ticker AND pt.period = t.prev_period
            LEFT JOIN _flow fl ON fl.ticker = t.ticker AND fl.period = t.period
            LEFT JOIN _deadline d ON d.period = t.period
            LEFT JOIN _filed fd ON fd.period = t.period
            LEFT JOIN _closed cl ON cl.period = t.period
            ORDER BY t.ticker, t.period""")

    @staticmethod
    def _periods(con: duckdb.DuckDBPyConnection) -> int:
        return common.create_table(con, "sec13f_periods", PERIODS, f"""
            SELECT f.period, f.filers, a.filings_active, v.value_total, coalesce(v.value_mapped, 0) AS value_mapped,
                   CASE WHEN v.value_total > 0 THEN round(coalesce(v.value_mapped, 0) / v.value_total, 4)
                   END AS coverage,
                   v.unmapped_cusips, v.refused_value, r.rescaled_filers, d.deadline AS filing_deadline,
                   fd.filed_through, cl.data_set, cl.data_set_seen, cl.closed
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
            LEFT JOIN _closed cl ON cl.period = f.period
            ORDER BY f.period""")

    @staticmethod
    def _types(con: duckdb.DuckDBPyConnection) -> int:
        """``sec13f_type_holdings``: per investor type and quarter, the type's ``TYPE_TOP`` largest tickers by the
        value its filers hold, each with its share of the type's mapped portfolio, and the rest as one row with
        ``ticker`` NULL (#760). The types are the newest ``uw_institution`` snapshot's tags, matched on the CIK with
        its zeros dropped (UW pads to ten digits, a 13F filing does not), plus ``all``; without the table only
        ``all`` is served."""
        _empty(con, "_tag", [("cik", "TEXT"), ("tag", "TEXT")])
        table, snapshot = uw_institutions.TABLE, None
        if common.has_table(con, table):
            snapshot = con.execute(f"SELECT max(snapshot_date) FROM {table}").fetchone()[0]
        if snapshot is not None:
            con.execute(f"""INSERT INTO _tag
                SELECT DISTINCT k.cik, u.tag
                FROM (SELECT ltrim(cik, '0') AS bare, unnest(tags) AS tag FROM {table}
                      WHERE snapshot_date = ? AND tags IS NOT NULL) u
                JOIN (SELECT DISTINCT cik, ltrim(cik, '0') AS bare FROM sec13f_filer_period) k ON k.bare = u.bare
                WHERE u.tag IS NOT NULL AND u.tag <> '{ALL_TYPE}'""", [snapshot])
        snap = f"DATE '{snapshot}'" if snapshot is not None else "CAST(NULL AS DATE)"
        common.create_table(con, "sec13f_type_holdings", TYPES)
        _by_period(con, _periods_of(con, "_held"), "sec13f_type_holdings", f"""
            WITH pos AS MATERIALIZED (SELECT m.ticker, h.period, h.cik, sum(h.value) AS value
                         FROM _held h JOIN sec13f_cusip_map m ON m.cusip = h.cusip AND m.period = h.period
                         WHERE h.period = ? AND m.ticker IS NOT NULL GROUP BY ALL HAVING sum(h.value) > 0),
                 tg AS (SELECT cik, tag FROM _tag UNION ALL SELECT DISTINCT cik, '{ALL_TYPE}' FROM pos),
                 v AS (SELECT tg.tag, pos.period, pos.ticker, sum(pos.value) AS value,
                              count(DISTINCT pos.cik) AS holders
                       FROM pos JOIN tg ON tg.cik = pos.cik GROUP BY ALL),
                 t AS (SELECT tg.tag, sum(pos.value) AS type_value, count(DISTINCT pos.cik) AS type_filers
                       FROM pos JOIN tg ON tg.cik = pos.cik GROUP BY ALL),
                 r AS (SELECT *, row_number() OVER (PARTITION BY tag ORDER BY value DESC, ticker) AS rn FROM v),
                 o AS (SELECT tag, period, ticker, rn, value, holders FROM r WHERE rn <= {TYPE_TOP}
                       UNION ALL
                       SELECT tag, min(period), NULL, NULL, sum(value), NULL FROM r WHERE rn > {TYPE_TOP}
                       GROUP BY tag)
            SELECT o.tag, o.period, o.ticker, o.rn, o.value, o.holders, round(o.value / t.type_value * 100, 4),
                   t.type_value, t.type_filers, {snap}
            FROM o JOIN t ON t.tag = o.tag""")
        return common.count(con, "sec13f_type_holdings")

    @staticmethod
    def _breaks(con: duckdb.DuckDBPyConnection, as_of: date) -> int:
        """``sec13f_filer_breaks``: per ticker and **settled** quarter (deadline before ``as_of``; a quarter still
        arriving has every filer not yet filed in it), each filer holding the ticker that started reporting (no
        holdings report in the quarter before, one now) or stopped (one before, none now), with its shares. Only a
        quarter whose quarter before is in the store: before the store's first quarter nothing is known (#732)."""
        common.create_table(con, "sec13f_filer_breaks", BREAKS)
        con.execute(f"""INSERT INTO sec13f_filer_breaks
            SELECT p.ticker, p.period, p.cik, coalesce(fn.filer_name, fb.filer_name),
                   CASE WHEN p.prev_s IS NULL THEN '{STARTED}' ELSE '{STOPPED}' END, coalesce(p.now_s, p.prev_s)
            FROM _pairs p
            JOIN _filers f ON f.period = p.period
            JOIN _filers pf ON pf.period = {PREV}
            JOIN _deadline d ON d.period = p.period
            LEFT JOIN _conf c ON c.cik = p.cik AND c.period = p.period
            LEFT JOIN (SELECT cik, period FROM _conf
                       UNION SELECT DISTINCT successor, {PREV} FROM _succ p) cp
                   ON cp.cik = p.cik AND cp.period = {PREV}
            LEFT JOIN sec13f_filer_period fn ON fn.cik = p.cik AND fn.period = p.period
            LEFT JOIN sec13f_filer_period fb ON fb.cik = p.cik AND fb.period = {PREV}
            WHERE d.deadline < ? AND ((p.prev_s IS NULL AND cp.cik IS NULL) OR (p.now_s IS NULL AND c.cik IS NULL))
            ORDER BY p.ticker, p.period, 5, 6 DESC, p.cik""", [as_of])
        return common.count(con, "sec13f_filer_breaks")

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
            how = (f"read in {'thousands' if factor < 1e6 else 'millions'}" if factor > 1
                   else f"read as {1 / factor:,.0f} times too high")
            found.append(("value_rescaled", f"{cik}|{period}",
                          f"{name or cik}, {period}: VALUE {how} (median ratio {ratio:g} to the 13F's own prices over "
                          f"{names} names): ${value / factor:,.0f} read as ${value:,.0f}"))
        for cik, period, cusip, name, old, new in con.execute(f"""
                SELECT cik, period, cusip, filer_name, old_value, new_value FROM _rowfix
                ORDER BY abs(old_value - new_value) DESC, cik, period, cusip LIMIT {ROWS_TOP}""").fetchall():
            found.append(("value_row_replaced", f"{cik}|{period}|{cusip}",
                          f"{name or cik}, {period}, {cusip}: ${old:,.0f} read at the 13F's own price, ${new:,.0f}"))
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
        if summary.get("px_fixed"):
            ctx.notes.append(f"{summary['px_fixed']} consensus prices 1,000 times off the quarters beside them: moved back")
        if summary.get("rows_replaced"):
            ctx.notes.append(f"{summary['rows_replaced']} holdings 300 times or more from the 13F's own price: read at "
                             "that price")
        if summary.get("succeeded"):
            ctx.notes.append(f"{summary['succeeded']} filers succeeded by another (UW's successions): counted as their "
                             "successor across the hand-over, not as a stop")
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
