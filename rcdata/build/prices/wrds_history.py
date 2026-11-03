"""``wrds/prices``: the price history from WRDS, Compustat's ``comp.secd`` and CRSP's ``crsp.dsf_v2`` from 1993 (CRSP
to 2025), with Compustat's splits (``comp.sec_split``) and dividends (``comp.sec_divid``).

**Compustat is the history** (master plan of 2 Nov 2026, items 1-3; avo, 3 Nov 2026: the whole history and what
comes after from Compustat): ``comp.secd`` from its earliest priced day, 1983-12-30; CRSP (1925-12-31..2025-12-31 on
WRDS) is a narrow fallback, and Alpaca a tester (``prices/serving``). The CRSP column names were read from WRDS's ``information_schema`` on homesrv (1 Nov
2026); Compustat's are as documented. A column the store needs that comes back empty for a whole answer is a
``wrds_column_empty`` run issue.

**No CCM link.** Homesrv's subscription refuses ``crsp_a_ccm`` (run of 30 Oct 2026), so CRSP's rows reach a
security through ``PERMNO_LINK`` at serving time, from columns every ``dsf_v2`` row carries (``hdrcusip``,
``cusip``, ``ticker``); see there for the rule and its measured coverage.

**Attended**, like the spine's WRDS datasets (``spine/wrds.py``): never due, run only from the CLI on the server
(``python -m RevenueChaser.rcdata.build backfill wrds/prices``), on the one WRDS worker of the process (one login,
one Duo push). Every query counts as one ``wrds`` call against the day cap (``rcdata/caps.py``); a backfill that
reaches the cap stops, keeps and serves the years it has, and the next backfill goes on from the first missing one.

**What is asked.** One query per table and calendar year (``year_sql``): ``comp.secd`` on ``COMPUSTAT_EXCHG``
(11, 12, 14: NYSE, NYSE American, Nasdaq; 17 and 21: the Arca listings) and ``comp.sec_divid`` from ``START_YEAR``
(the year of ``COMPUSTAT_START``, 1983-12-30, Compustat's earliest priced day), ``crsp.dsf_v2`` (every share type,
ETFs included) from ``START_YEAR`` to ``CRSP_LAST_YEAR``; ``comp.sec_split`` whole. A
**backfill** asks the snapshot first (the splits the years need), then the years newest first and,
within a year, the three tables; **a partition that exists is skipped** with no query, and a raw file that exists
is typed again rather than asked again (a run killed between the two). The **monthly run** (``fetch``) asks the
last ``REFRESH_YEARS`` years of each table again, and the snapshots when the newest is ``SNAPSHOT_DAYS`` old.

**The chunk and cap math** (measured 1 Nov 2026): ``dsf_v2`` holds 1.69M-2.54M rows a year from 1993 to 2025 (66M in
all, the most in 2025, 11,397 PERMNOs); a year of ``comp.secd`` (2024: 38 MB gzip) took about five minutes to ask on
30 Oct, and WRDS's ``statement_timeout`` is two days, so a year is the chunk. (The 30 Oct "Query interrupted", 31.5
minutes in, came after the run's last raw file: DuckDB's interrupt in the serving build, which the ASOF factor join
fixed, not WRDS.) A backfill from 1993 to 2025 is 33 years of three tables, 99 calls, plus the columns query once a
run and the split snapshot when due: two to three days of the 50-call ``wrds`` cap, each day going on from the first
missing partition.

**Raw**: ``raw/wrds/prices/<table>/year=<Y>/<as_of>.jsonl.gz`` (a snapshot: ``raw/wrds/prices/<table>/
<as_of>.jsonl.gz``), each with the worker's ``.meta.json`` sidecar. **Parquet**: ``parquet/wrds/prices/<table>/
year=<Y>/part-0.parquet`` (a snapshot: ``.../snapshot=<as_of>/part-0.parquet``); a refresh replaces the year whole,
and first keeps every bar value it replaces or drops (``secd`` and ``dsf``: open, high, low, close, volume) in
``parquet/wrds/prices/revisions/<table>/year=<Y>/part-<run_id>.parquet`` (``price_revision`` in the served file).

**Without CRSP.** A subscription that holds Compustat but not CRSP answers a CRSP query with PostgreSQL's
``InsufficientPrivilege`` (``spine/wrds.PermissionRefused``). The run then skips CRSP: the refusal is a
``wrds_no_access`` issue keyed by the schema and a run note, every later CRSP query of the run is skipped unasked,
and the history is Compustat's (``secd`` bars with ``cshoc`` shares, so the caps; ``sec_divid``; ``sec_split``). No
file is left for the refused query, so the next backfill asks it once again and a subscription that gains CRSP is
picked up. The CRSP rows of ``wrds_price_diff`` then count no session. A refused Compustat table, and every other
error, still fails the run.

**The columns first.** The first query of a run that asks anything is ``information_schema.columns`` for the four
tables (``fundamentals/wrds_fund.catalog``, the columns this login can read; one ``wrds`` call, the same login, so no
second Duo): a Compustat table the login cannot read, or a column a query names that WRDS lacks (``ASKED_COLUMNS``,
``exchg`` among them), fails the run before any data is asked; a CRSP table missing from it, or a CRSP column it
lacks, is skipped as above.

**Served** by ``prices/serving``: the bars (``COMP_BARS``, ``CRSP_BARS``; Compustat first, priority 1), the actions (``ACTIONS_SQL``; an Alpaca action of the same kind and day
winning), the revisions, and the difference report ``wrds_price_diff`` (``COMPARISONS``). Compustat's own adjusted
return is ``COMP_RETURN``: ``prccd / ajexdi * trfd`` over the previous session's; its splits are ``comp.sec_split``'s
and, where that has none, a change of ``ajexdi`` (``AJEXDI_SPLITS``).

**Adjustment.** A served bar is nominal, CRSP's (``abs(dlyprc)``: a negative price is CRSP's bid/ask midpoint) as
Alpaca's raw bar, and the factors are applied on read from ``corporate_action``. Before Alpaca's action window the
only actions are Compustat's, keyed ``gvkey:iid`` or a security's ``GVKEY_ALIAS`` (an ETF the spine names by FIGI
whose CUSIP Compustat holds). A security no Compustat record reaches has no split or dividend applied to its CRSP
history, so ``prices/serving`` keeps those bars out of the adjusted view (``crsp_unadjusted``). ``dlycumfacpr`` and
``dlyorddivamt`` are stored for that.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import duckdb

from ....core import calendar
from ... import caps
from .. import base
from ..fundamentals import wrds_fund
from ..spine import wrds
from . import KIND, stage

NAME = "wrds/prices"
COMPUSTAT, CRSP = "wrds/compustat", "wrds/crsp"          # the bar sources in ``price_bar_daily``
# Compustat is the one primary source (master plan of 2 Nov 2026, item 1): the history starts on the earliest
# priced ``comp.secd`` day, measured on homesrv (priced rows 1983-12-30..2026-10-30), and every yearly table from
# that year, so CRSP's fallback rows (``serving``) reach as far back.
COMPUSTAT_START = date(1983, 12, 30)
START_YEAR = COMPUSTAT_START.year
# ``comp.secd``'s exchanges: NYSE, NYSE American, Nasdaq (11, 12, 14, the spine's ``US_EXCHG``) and 17 and 21, where
# the Arca-listed ETFs the old filter dropped are filed (homesrv measurement, master plan item 1).
COMPUSTAT_EXCHG = (11, 12, 14, 17, 21)
CRSP_LAST_YEAR = 2025                                    # the CRSP subscription's last year
REFRESH_YEARS = 2
SNAPSHOT_DAYS = 30
# CRSP's ``shrout`` is in thousands of shares (CRSP's documentation; not read from WRDS here).
CRSP_SHARES_UNIT = 1000
RAW_SUFFIX = ".jsonl.gz"
PROVENANCE = [("source_as_of", "DATE"), ("fetched_at", "DOUBLE"), ("run_id", "TEXT")]

SECD = [("gvkey", "TEXT"), ("iid", "TEXT"), ("datadate", "DATE"), ("tic", "TEXT"), ("cusip", "TEXT"),
        ("curcdd", "TEXT"), ("prccd", "DOUBLE"), ("prcod", "DOUBLE"), ("prchd", "DOUBLE"), ("prcld", "DOUBLE"),
        ("cshtrd", "DOUBLE"), ("cshoc", "DOUBLE"), ("ajexdi", "DOUBLE"), ("trfd", "DOUBLE"), ("exchg", "INTEGER"),
        # The day's distributions, on the secd row itself (the coordinator's WRDS column list, master plan Part C): one
        # Compustat source for the dividends, ``comp.sec_divid`` only their fallback. divrc, cheqv and capgn are kept,
        # not served: no comment defines how they combine with div/divd/divsp.
        ("curcddv", "TEXT"), ("div", "DOUBLE"), ("divd", "DOUBLE"), ("divsp", "DOUBLE"), ("divrc", "DOUBLE"),
        ("cheqv", "DOUBLE"), ("capgn", "DOUBLE")]
# The secd columns a partition asked before them lacks: read as NULL until the year is asked again (``_current``).
SECD_ADDED = ("curcddv", "div", "divd", "divsp", "divrc", "cheqv", "capgn")
# ``hdrcusip`` (8 characters, the PERMNO's newest CUSIP), ``cusip`` (the date's) and ``ticker`` (the date's) feed
# ``PERMNO_LINK``; ``dlycumfacpr`` / ``dlycumfacshr`` (CRSP's cumulative price and share factors) and
# ``dlyorddivamt`` (the ordinary dividend going ex that day) are kept, not served yet: they let CRSP's own splits
# and dividends be read later without asking 33 years again.
DSF = [("permno", "TEXT"), ("dlycaldt", "DATE"), ("dlyprc", "DOUBLE"), ("dlyopen", "DOUBLE"), ("dlyhigh", "DOUBLE"),
       ("dlylow", "DOUBLE"), ("dlyvol", "DOUBLE"), ("dlyret", "DOUBLE"), ("shrout", "DOUBLE"), ("hdrcusip", "TEXT"),
       ("cusip", "TEXT"), ("ticker", "TEXT"), ("dlycumfacpr", "DOUBLE"), ("dlycumfacshr", "DOUBLE"),
       ("dlyorddivamt", "DOUBLE")]
DIVID = [("gvkey", "TEXT"), ("iid", "TEXT"), ("datadate", "DATE"), ("div", "DOUBLE"), ("divd", "DOUBLE"),
         ("divsp", "DOUBLE"), ("curcddv", "TEXT"), ("paydate", "DATE"), ("recorddate", "DATE")]
SPLIT = [("gvkey", "TEXT"), ("iid", "TEXT"), ("datadate", "DATE"), ("split", "DOUBLE")]
REVISION = [("source", "TEXT"), ("security_key", "TEXT"), ("session_date", "DATE"), ("symbol", "TEXT"),
            ("open", "DOUBLE"), ("high", "DOUBLE"), ("low", "DOUBLE"), ("close", "DOUBLE"), ("volume", "DOUBLE"),
            ("fetched_at", "DOUBLE"), ("run_id", "TEXT"), ("revised_at", "DOUBLE"), ("revised_by", "TEXT")]

_EXCHG = ", ".join(str(e) for e in COMPUSTAT_EXCHG)
# table -> its spec. ``date``: the column a year's query ranges over (None: asked whole, as a snapshot).
# ``needed``: the columns the store's numbers come from (an answer with rows and none of them is a run issue).
# ``bar``: the source label and the (security key, symbol, open, high, low, close, volume) columns of a bar
# table, for the revisions.
TABLES: dict[str, dict] = {
    "secd": {"wrds": "comp.secd", "schema": SECD, "key": ["gvkey", "iid", "datadate"], "date": "datadate",
             "last_year": None, "needed": ("prccd", "ajexdi", "trfd", "cshoc"),
             "sql": ("SELECT " + ", ".join(c for c, _ in SECD) + " FROM comp.secd WHERE {range} "
                     "AND exchg IN (" + _EXCHG + ")"),
             "bar": (COMPUSTAT, "gvkey || ':' || iid", "replace(upper(tic), '.', '-')", "prcod", "prchd", "prcld",
                     "prccd", "cshtrd")},
    "dsf": {"wrds": "crsp.dsf_v2", "schema": DSF, "key": ["permno", "dlycaldt"], "date": "dlycaldt",
            "last_year": CRSP_LAST_YEAR, "needed": ("dlyprc", "dlyret", "shrout", "hdrcusip", "ticker"),
            "sql": ("SELECT " + ", ".join(c for c, _ in DSF) + " FROM crsp.dsf_v2 WHERE {range}"),
            "bar": (CRSP, "permno", "CAST(NULL AS TEXT)", "dlyopen", "dlyhigh", "dlylow", "abs(dlyprc)", "dlyvol")},
    # Every query names its columns (never ``SELECT *``): the schema's, checked against WRDS's catalog first.
    "sec_divid": {"wrds": "comp.sec_divid", "schema": DIVID, "key": ["gvkey", "iid", "datadate"], "date": "datadate",
                  "last_year": None, "needed": ("divd",),
                  "sql": "SELECT " + ", ".join(c for c, _ in DIVID) + " FROM comp.sec_divid WHERE {range}"},
    "sec_split": {"wrds": "comp.sec_split", "schema": SPLIT, "key": ["gvkey", "iid", "datadate"], "date": None,
                  "needed": ("split",), "sql": "SELECT " + ", ".join(c for c, _ in SPLIT) + " FROM comp.sec_split"},
}
YEARLY = ("secd", "dsf", "sec_divid")
SNAPSHOTS = ("sec_split",)
# CRSP's table: a subscription without CRSP answers it with a permission refusal, and the history is then
# Compustat's alone (``_skip_crsp``).
CRSP_TABLES = ("dsf",)
# The WRDS columns each query names (its SELECT list and filters), checked against ``information_schema.columns``
# before any data is asked (``_check_columns``): a name WRDS does not have fails the run on that one query, not on a
# data query after the Duo push.
ASKED_COLUMNS = {"secd": [c for c, _ in SECD], "dsf": [c for c, _ in DSF],
                 "sec_divid": [c for c, _ in DIVID], "sec_split": [c for c, _ in SPLIT]}
STAGING = {"secd": "stg_wrds_secd", "dsf": "stg_wrds_dsf", "sec_divid": "stg_wrds_divid",
           "sec_split": "stg_wrds_split"}

# Compustat's adjusted price and its day return (WRDS: total return = (prccd / ajexdi * trfd) over the previous
# day's, minus 1), over a window ``w`` of one security by date. Measured on homesrv against CRSP's ``dlyret`` on
# dividend days: p95 difference 0.000055 %, where the price-only return misses by 1.5 % (p95). Two rows of different
# answers (``run_id``) are not compared: a year partition keeps the ``ajexdi`` basis of the day it was asked, and the
# monthly refresh re-asks only the newest years, so a split since shows as a step at the partition boundary.
COMP_ADJ = "prccd / ajexdi * trfd"
# An empty ``trfd`` means no distribution that day, not missing data (coordinator's homesrv measurement, 3 Nov 2026,
# 0 WRDS queries: the price-only return matches CRSP's ``dlyret`` within 0.01 % on 97.69 % of 4,069,937 matched
# non-ex-dividend days with ``trfd`` empty, as the formula does on its rows, 97.66 %; 6,437 of 17,058 securities have
# no ``trfd`` on any day, the non-payers). So a day without ``trfd`` on either side is read as ``trfd`` = 1, and the
# day's own cash distribution (``COMP_DIV``, the secd row's) is added to the numerator, in the day's share basis.
COMP_DIV = "(coalesce(divd, div, 0) + coalesce(divsp, 0))"
_TRFD_BOTH = "trfd IS NOT NULL AND lag(trfd) OVER w IS NOT NULL"
COMP_RETURN = (f"CASE WHEN lag(run_id) OVER w = run_id THEN CASE WHEN {_TRFD_BOTH} "
               f"THEN ({COMP_ADJ}) / lag({COMP_ADJ}) OVER w - 1 "
               f"ELSE ((prccd + {COMP_DIV}) / ajexdi) / lag(prccd / ajexdi) OVER w - 1 END END")
# Which rule made the row's return: ``trfd``, ``price_dividend`` (no trfd, a distribution added) or ``price_only``.
# shortcut: a dividend day without trfd is flagged, not checked; avo is deciding whether to fill the trfd gap otherwise
# (the hook is ``COMP_DIV``).
COMP_RETURN_BASIS = (f"CASE WHEN {_TRFD_BOTH} OR lag(run_id) OVER w IS NULL THEN 'trfd' "
                     f"WHEN {COMP_DIV} > 0 THEN 'price_dividend' ELSE 'price_only' END")

# The rows of the difference report (``wrds_price_diff``), one per comparison and build.
COMPARISONS = ("crsp_vs_compustat", "compustat_vs_alpaca", "crsp_vs_alpaca", "compustat_returns", "crsp_returns")


def years(table: str, as_of: date, first: int | None = None) -> list[int]:
    """The years a backfill of ``table`` as of ``as_of`` asks, oldest first."""
    spec = TABLES[table]
    last = min(as_of.year, spec["last_year"] or as_of.year)
    return list(range(first if first is not None else START_YEAR, last + 1))


def year_sql(table: str, year: int) -> str:
    spec = TABLES[table]
    return spec["sql"].format(range=f"{spec['date']} BETWEEN '{year}-01-01' AND '{year}-12-31'")


def raw_dir(table: str, year: int | None = None) -> Path:
    folder = base.raw_root() / "wrds" / "prices" / table
    return folder / f"year={year}" if year is not None else folder


def partition_path(table: str, year: int) -> Path:
    return base.parquet_root() / "wrds" / "prices" / table / f"year={year}" / "part-0.parquet"


def snapshot_path(table: str, as_of: date) -> Path:
    return base.parquet_root() / "wrds" / "prices" / table / f"snapshot={as_of.isoformat()}" / "part-0.parquet"


def revision_path(table: str, year: int, run_id: str) -> Path:
    return base.parquet_root() / "wrds" / "prices" / "revisions" / table / f"year={year}" / f"part-{run_id}.parquet"


def _snapshots(table: str) -> list[tuple[str, Path]]:
    return stage.partitions(base.parquet_root() / "wrds" / "prices" / table, "snapshot")


def _meta(raw: Path) -> Path:
    return raw.with_name(raw.name + wrds.META_SUFFIX)


def _cast(column: str, kind: str) -> str:
    if kind in ("DATE", "DOUBLE"):
        return f'TRY_CAST("{column}" AS {kind})'
    if kind == "INTEGER":
        return f'TRY_CAST(TRY_CAST("{column}" AS DOUBLE) AS INTEGER)'
    return f'"{column}"'


def typed_rows(raw: Path, schema: list[tuple[str, str]]) -> str:
    """A SELECT of the raw file's rows typed to ``schema`` (every value arrives as text or null; a value that does
    not type is null, a column the answer lacks is null)."""
    cols = "{" + ", ".join(f"'{c}': 'VARCHAR'" for c, _ in schema) + "}"
    typed = ", ".join(f'{_cast(c, t)} AS "{c}"' for c, t in schema)
    return f"SELECT {typed} FROM read_json({base.sql_path(raw)}, format = 'newline_delimited', columns = {cols})"


def _current(table: str, year: int, raw: Path) -> bool:
    """Whether ``raw`` answers today's query of a year: a ``comp.secd`` year asked with another query (the exchange
    filter before ``COMPUSTAT_EXCHG``, or the columns before ``SECD_ADDED``) is not, so the backfill asks it again
    (step 5 of the 2 Nov 2026 plan). A raw file without a sidecar, and every other table, counts as current."""
    meta = _meta(raw)
    asked = base.read_raw_json(meta).get("query") if meta.is_file() else None
    return table != "secd" or asked is None or asked == year_sql(table, year)


def _stale(table: str, year: int) -> bool:
    """A partition that exists from a raw answer that is not ``_current``."""
    raws = sorted(raw_dir(table, year).glob(f"*{RAW_SUFFIX}"))
    return partition_path(table, year).is_file() and bool(raws) and not _current(table, year, raws[-1])


def _rows(path: Path) -> int:
    con = duckdb.connect()
    try:
        return con.execute(f"SELECT count(*) FROM read_parquet({base.sql_path(path)})").fetchone()[0]
    finally:
        con.close()


def _where(raw: Path) -> tuple[str, int | None]:
    """``(table, year)`` of a raw file (``year`` None for a snapshot)."""
    if raw.parent.name.startswith("year="):
        return raw.parent.parent.name, int(raw.parent.name.split("=", 1)[1])
    return raw.parent.name, None


class WrdsPrices:
    name = NAME
    kind = KIND
    providers = (wrds.PROVIDER,)
    tables = ("price_bar_daily", "corporate_action", "price_revision", "wrds_price_diff")
    cadence = "monthly"
    attended = property(lambda self: base.wrds_attended())   # RC_WRDS_UNATTENDED=1: the builder runs it
    backfill_needs_from = False
    starts_by_hand = True

    def default_as_of(self) -> date:
        return calendar.today_ny()

    def _ask(self, ctx: base.RunContext, table: str, year: int | None, as_of: date) -> Path:
        sql = year_sql(table, year) if year is not None else TABLES[table]["sql"]
        return wrds._ask(ctx, raw_dir(table, year) / f"{as_of.isoformat()}{RAW_SUFFIX}", sql, as_of)

    def _snapshot_due(self, table: str, as_of: date) -> bool:
        snaps = _snapshots(table)
        return not snaps or (as_of - date.fromisoformat(snaps[-1][0])) >= timedelta(days=SNAPSHOT_DAYS)

    @staticmethod
    def _plan(as_of: date, first: int | None, refresh: bool) -> list[tuple[str, int]]:
        """The (table, year) pairs to ask, newest year first, the three tables within a year."""
        lo = {t: years(t, as_of, first) for t in YEARLY}
        if refresh:
            lo = {t: ys[-REFRESH_YEARS:] for t, ys in lo.items()}
        every = sorted({y for ys in lo.values() for y in ys}, reverse=True)
        return [(t, y) for y in every for t in YEARLY if y in lo[t]]

    def _ask_or_skip(self, ctx: base.RunContext, table: str, year: int | None, as_of: date,
                     run: dict) -> Path | None:
        """``_ask``, except that a CRSP table the subscription refuses is skipped: once refused, every later CRSP
        query of the run is skipped unasked (``run["refused"]`` holds the schema), so a run never asks it year by
        year. A refusal of a Compustat table, and any other error, fails the run as before. The first query of a
        run is ``_check_columns``'s."""
        if not run["checked"]:
            self._check_columns(ctx, as_of, run)
            run["checked"] = True
        refused = run["refused"]
        if table in CRSP_TABLES and refused:
            return None
        try:
            return self._ask(ctx, table, year, as_of)
        except wrds.PermissionRefused as e:
            if table not in CRSP_TABLES:
                raise
            self._skip_crsp(ctx, table, e, refused, e.schema)
            return None

    @staticmethod
    def _skip_crsp(ctx: base.RunContext, table: str, e: Exception, refused: list[str], schema: str | None) -> None:
        schema = schema or TABLES[table]["wrds"]
        refused.append(schema)
        ctx.issue("wrds_no_access", schema,
                  f"{TABLES[table]['wrds']}: the WRDS subscription refuses it ({e}); CRSP skipped, the history "
                  "is Compustat's alone")
        ctx.notes.append(f"{TABLES[table]['wrds']}: permission denied for {schema}; CRSP skipped, Compustat only")

    def _check_columns(self, ctx: base.RunContext, as_of: date, run: dict) -> None:
        """One ``information_schema.columns`` query (``wrds_fund.catalog``, the columns this login can read) before
        any data is asked: a Compustat table the login cannot read, or a column a query names that WRDS lacks,
        fails the run here; a CRSP library the login cannot read, or a CRSP column it lacks, is skipped as a refusal
        (``wrds_no_access``)."""
        found = wrds_fund.catalog(ctx, NAME, as_of, sorted({TABLES[t]["wrds"].split(".")[1] for t in TABLES}))
        for table in (*SNAPSHOTS, *YEARLY):
            library, name = TABLES[table]["wrds"].split(".")
            columns = found.get((library, name))
            if not columns:
                if table in CRSP_TABLES:
                    if not run["refused"]:
                        self._skip_crsp(ctx, table, base.SourceError(f"{library}.{name} is not in "
                                                                     "information_schema for this login"),
                                        run["refused"], library)
                    continue
                elsewhere = sorted(lib for lib, t in found if t == name)
                raise base.SourceError(f"wrds {library}.{name} is not readable by this login; WRDS has {name} in "
                                       f"{', '.join(elsewhere) or 'no library this login can read'}")
            missing = [c for c in ASKED_COLUMNS[table] if c not in columns]
            if missing and table in CRSP_TABLES:
                # A CRSP column WRDS lacks degrades like a CRSP library it lacks: CRSP skipped, Compustat goes on.
                if not run["refused"]:
                    self._skip_crsp(ctx, table, base.SourceError(f"{library}.{name} has no {', '.join(missing)}, "
                                                                 "which the query names"), run["refused"], library)
                continue
            if missing:
                raise base.SourceError(f"wrds {library}.{name} has no {', '.join(missing)}, which the query names "
                                       f"(it has {', '.join(sorted(columns))}); nothing was asked")

    def fetch(self, ctx: base.RunContext, as_of: date) -> list[Path]:
        """The monthly run: the snapshots when due, then the last ``REFRESH_YEARS`` years of each table again."""
        run = {"refused": [], "checked": False}
        out = [self._ask_or_skip(ctx, t, None, as_of, run) for t in SNAPSHOTS if self._snapshot_due(t, as_of)]
        out += [self._ask_or_skip(ctx, t, y, as_of, run) for t, y in self._plan(as_of, None, refresh=True)]
        out = [p for p in out if p is not None]
        if not any(partition_path("secd", y).is_file() for y in years("secd", as_of)[:-REFRESH_YEARS]):
            ctx.notes.append("no history before the refreshed years: run the backfill (attended)")
        return out

    def backfill(self, ctx: base.RunContext, start: date | None, end: date) -> None:
        """Every missing snapshot and year partition up to ``end``; a partition that exists is never asked."""
        asked = 0
        run = {"refused": [], "checked": False}
        try:
            for table in SNAPSHOTS:
                if not _snapshots(table):
                    raw = self._ask_or_skip(ctx, table, None, end, run)
                    if raw is not None:
                        self.to_parquet_all(ctx, [raw])
                        asked += 1
            for table, year in self._plan(end, start.year if start else None, refresh=False):
                if partition_path(table, year).is_file() and not _stale(table, year):
                    continue
                have = [r for r in sorted(raw_dir(table, year).glob(f"*{RAW_SUFFIX}")) if _current(table, year, r)]
                same_day = raw_dir(table, year) / f"{end.isoformat()}{RAW_SUFFIX}"
                if not have and same_day.exists():
                    # ``wrds._ask`` takes a file of the day as answered: an old filter's is kept aside, not reused.
                    for f in (same_day, _meta(same_day)):
                        f.rename(f.with_name(f.name + ".superseded"))
                raw = have[-1] if have else self._ask_or_skip(ctx, table, year, end, run)
                if raw is None:
                    continue
                self.to_parquet_all(ctx, [raw])
                asked += 1
        except base.CapReached as e:
            ctx.notes.append(f"stopped at the WRDS day cap ({e}) after {asked} partition(s); the next backfill "
                             "goes on from the first missing year")

    def dry_run(self, start: date | None, end: date) -> list[str]:
        """What ``backfill`` would ask, without a login: one line per query and a total of queries, of the ``wrds``
        day cap's days, and of the expected rows. A missing year is expected to hold its table's mean of the years
        present; a year asked again holds at least its rows now (the new exchanges add to it)."""
        plan = [(t, None, "missing") for t in SNAPSHOTS if not _snapshots(t)]
        for table, year in self._plan(end, start.year if start else None, refresh=False):
            if _stale(table, year):
                plan.append((table, year, "asked with an older query (exchange filter or columns)"))
            elif not partition_path(table, year).is_file() and not any(
                    _current(table, year, r) for r in raw_dir(table, year).glob(f"*{RAW_SUFFIX}")):
                plan.append((table, year, "missing"))
        mean = {}
        for table in YEARLY:
            have = [_rows(p) for _, p in stage.partitions(base.parquet_root() / "wrds" / "prices" / table, "year")]
            mean[table] = sum(have) // len(have) if have else None
        lines, total, unknown = [], 0, 0
        for table, year, why in plan:
            est = (_rows(partition_path(table, year)) if why != "missing" else mean.get(table)) if year else None
            total, unknown = total + (est or 0), unknown + (est is None)
            lines.append(f"{TABLES[table]['wrds']} {year or 'snapshot'}: {why}, expected rows "
                         f"{'unknown' if est is None else ('>= ' if why != 'missing' else '~') + format(est, ',')}")
        queries = len(plan) + (1 if plan else 0)          # the columns query first
        cap = caps.DAY_CAPS[wrds.PROVIDER]
        lines.append(f"{NAME} dry run: {queries} queries ({-(-queries // cap)} day(s) of the {cap}-call wrds cap), "
                     f"expected rows ~{total:,}" + (f" plus {unknown} partition(s) with no estimate" if unknown else ""))
        return lines

    def to_parquet(self, ctx: base.RunContext, raw: Path) -> list[Path]:
        return self.to_parquet_all(ctx, [raw])

    def to_parquet_all(self, ctx: base.RunContext, raws: list[Path]) -> list[Path]:
        out = []
        for raw in raws:
            table, year = _where(raw)
            meta = base.read_raw_json(_meta(raw))
            as_of = date.fromisoformat(meta["as_of"])
            final = partition_path(table, year) if year is not None else snapshot_path(table, as_of)
            out.append(self._write(ctx, table, year, raw, meta, final))
        return out

    def _write(self, ctx: base.RunContext, table: str, year: int | None, raw: Path, meta: dict, final: Path) -> Path:
        spec = TABLES[table]
        con = duckdb.connect()
        try:
            keys = ", ".join(spec["key"])
            every = ", ".join(f'"{c}" NULLS LAST' for c, _ in spec["schema"])
            con.execute(f"CREATE TABLE new AS SELECT *, DATE '{meta['as_of']}' AS source_as_of, "
                        f"CAST({float(meta['fetched_at'])} AS DOUBLE) AS fetched_at, '{meta['run_id']}' AS run_id "
                        f"FROM ({typed_rows(raw, spec['schema'])}) "
                        f"QUALIFY row_number() OVER (PARTITION BY {keys} ORDER BY {every}) = 1")
            rows = stage.count(con, "new")
            for col in spec["needed"]:
                if rows and not stage.count(con, "new", f'"{col}" IS NOT NULL'):
                    ctx.issue("wrds_column_empty", f"{table}.{col}",
                              f"{spec['wrds']} answered {rows} row(s) and no {col} (year {year or 'snapshot'})")
            if spec.get("bar") and final.is_file():
                self._revisions(ctx, con, table, year, final, meta)
            ctx.notes.append(f"{spec['wrds']} {year or 'snapshot'}: {rows} rows")
            return base.write_parquet(con, f"SELECT * FROM new ORDER BY {keys}", final)
        finally:
            con.close()

    @staticmethod
    def _revisions(ctx: base.RunContext, con: duckdb.DuckDBPyConnection, table: str, year: int, final: Path,
                   meta: dict) -> None:
        """The bar values of the partition being replaced that the new answer changes or drops."""
        source, key, symbol, *ohlcv = TABLES[table]["bar"]
        keys = TABLES[table]["key"]
        on = " AND ".join(f'n."{k}" = o."{k}"' for k in keys)
        cols = ("open", "high", "low", "close", "volume")
        changed = " OR ".join(f"n.{c} IS DISTINCT FROM o.{c}" for c in cols)
        con.execute(f"CREATE TABLE old AS SELECT * FROM read_parquet({base.sql_path(final)})")
        shape = (f"SELECT {', '.join(keys)}, {key} AS security_key, {symbol} AS symbol, "
                 + ", ".join(f"{expr} AS {c}" for expr, c in zip(ohlcv, cols)) + ", fetched_at, run_id FROM {t}")
        con.execute(f"""CREATE TABLE rev AS
            SELECT '{source}' AS source, o.security_key, o.{TABLES[table]['date']} AS session_date, o.symbol,
                   o.open, o.high, o.low, o.close, o.volume, o.fetched_at, o.run_id,
                   CAST({float(meta['fetched_at'])} AS DOUBLE) AS revised_at, '{ctx.run_id}' AS revised_by
            FROM ({shape.format(t='old')}) o LEFT JOIN ({shape.format(t='new')}) n ON {on}
            WHERE n.security_key IS NULL OR {changed}""")
        n = stage.count(con, "rev")
        if n:
            base.write_parquet(con, "SELECT * FROM rev ORDER BY security_key, session_date",
                               revision_path(table, year, ctx.run_id))
            ctx.notes.append(f"{TABLES[table]['wrds']} {year}: {n} replaced value row(s) kept as revisions")

    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        for table in YEARLY:
            stage.create(con, STAGING[table], TABLES[table]["schema"] + PROVENANCE,
                         [f for _, f in stage.partitions(base.parquet_root() / "wrds" / "prices" / table, "year")],
                         added_later=SECD_ADDED if table == "secd" else ())
        for table in SNAPSHOTS:
            snaps = _snapshots(table)
            stage.create(con, STAGING[table], TABLES[table]["schema"] + PROVENANCE, [snaps[-1][1]] if snaps else [])
        revisions = sorted((base.parquet_root() / "wrds" / "prices" / "revisions").glob("*/year=*/part-*.parquet"))
        stage.create(con, "stg_wrds_revisions", REVISION, revisions)
        return {}

    def check(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict:
        # The served rows this dataset feeds against the live file: its bars, its actions and the revisions.
        mine = f"source IN ('{COMPUSTAT}', '{CRSP}')"
        added = removed = 0
        for table, cols, where in (
                ("price_bar_daily", ["security_id", "session_date", "open", "high", "low", "close", "volume",
                                     "source"], mine),
                ("corporate_action", ["security_id", "ex_date", "action", "ratio", "amount", "source_id"],
                 f"source = '{NAME}'"),
                ("price_revision", ["security_id", "session_date", "close", "source", "revised_by"], mine)):
            a, r = stage.compare_live(con, table, cols, where)
            added, removed = added + a, removed + r
        newest = max((con.execute(f"SELECT max(source_as_of) FROM {STAGING[t]}").fetchone()[0] for t in YEARLY),
                     default=None, key=lambda d: d or date.min)
        rows = sum(stage.count(con, STAGING[t]) for t in ("secd", "dsf"))
        for t in ("secd", "dsf"):
            span = con.execute(f"SELECT min(year({TABLES[t]['date']})), max(year({TABLES[t]['date']})), count(*) "
                               f"FROM {STAGING[t]}").fetchone()
            if span[2]:
                ctx.notes.append(f"{TABLES[t]['wrds']} {span[0]}-{span[1]}: {span[2]} rows staged")
        return {"dataset": NAME, "result": "changed" if added or removed else "unchanged",
                "source_as_of": (newest or ctx.as_of or self.default_as_of()).isoformat(), "rows": rows,
                "added": added, "removed": removed, "coverage": None}


# ── The served history (read by ``prices/serving``, over the staging tables above) ───────────────────────────

# The security of each CRSP PERMNO, without CCM (``serving`` makes it the temp table ``_permno_link``, over
# ``stg_wrds_dsf``, the spine's ``stg_spine`` and ``stg_wrds_secd``). A PERMNO is one security for its whole life,
# so the whole PERMNO links to one security, by the first rule that matches:
#   1. ``hdrcusip``: its header CUSIP (8 characters) is the first 8 of a spine security's 9-character CUSIP;
#   2. ``cusip``: any CUSIP it carried on a staged day is (a CUSIP changed since);
#   3. ``ticker``: its ticker on its newest staged row (NULL if that row has none) is a spine ticker, when that row is on
#      CRSP's newest staged day (still listed: a delisted PERMNO's old ticker is often someone else's today) and no other
#      PERMNO had that ticker that day (CRSP names both Berkshire classes ``BRK``); and the spine row existed then: it
#      has no CUSIP to contradict, or Compustat holds its ``gvkey:iid`` on that day, and an ETF's ``etf.inception`` is
#      on or before it (review M1 of #353: a 2026 listing that took a delisted name's ticker gets none of its history).
#      This is how ETFs link: the spine holds no CUSIP for them;
#   4. ``compustat``: as 2 against a Compustat security's CUSIP in ``comp.secd`` (a delisted name, off the spine).
# Then one PERMNO per security (the best rule, then the newest PERMNO), so two never fill one security's day.
# An unlinked PERMNO keeps ``permno:<PERMNO>``; it links at the next build once the spine holds it (no new query).
# Measured 1 Nov 2026 (homesrv spine, CRSP's 2025-12-31): Tier A 5,006 of 5,655 linked (88.5 %; common 93.3 %, the
# rest mostly listed after 2025, which CRSP does not hold yet), 4,572 by CUSIP and 434 by ticker; Tier B 567 of 2,015
# (ETPs 113 of 206, all by ticker, the rest ETNs CRSP does not carry or 2026 listings; CEFs 98 %; preferreds, units
# and warrants are not in CRSP). Of the 6,702 PERMNOs trading on 2010-12-31: 2,499 link to today's spine (2,448 of
# the 3,159 still listed at 2025's end; 610 of the rest are ETFs the spine does not hold), and at least 2,339 of the
# 3,543 since delisted have a Compustat CUSIP on a US exchange.
# shortcut: one security per PERMNO and the reverse; a security that changed PERMNO keeps only its newest one's
# history, measure ``permno:`` rows with a spine ticker once the full history is built.
PERMNO_LINK = """
    WITH p AS (SELECT permno, max(dlycaldt) AS last, arg_max_null(ticker, dlycaldt) AS ticker,
                      left(arg_max_null(hdrcusip, dlycaldt), 8) AS hdrcusip FROM stg_wrds_dsf GROUP BY permno),
    c AS (SELECT DISTINCT permno, left(cusip, 8) AS cusip FROM stg_wrds_dsf WHERE cusip IS NOT NULL),
    newest AS (SELECT * FROM p WHERE last = (SELECT max(last) FROM p)),
    alone AS (SELECT ticker FROM newest GROUP BY ticker HAVING count(*) = 1),
    s AS (SELECT security_id, ticker, left(cusip, 8) AS c8, coalesce(is_etf, false) AS is_etf, inception
          FROM stg_spine),
    comp AS (SELECT DISTINCT gvkey || ':' || iid AS security_id, left(cusip, 8) AS c8 FROM stg_wrds_secd
             WHERE cusip IS NOT NULL),
    cand AS (
        SELECT p.permno, s.security_id, 1 AS rule, 'hdrcusip' AS how FROM p JOIN s ON s.c8 = p.hdrcusip
        UNION ALL SELECT c.permno, s.security_id, 2, 'cusip' FROM c JOIN s ON s.c8 = c.cusip
        UNION ALL SELECT n.permno, s.security_id, 3, 'ticker' FROM newest n JOIN alone USING (ticker)
                  JOIN s ON s.ticker = n.ticker
                  WHERE (s.c8 IS NULL OR EXISTS (SELECT 1 FROM stg_wrds_secd d WHERE d.datadate = n.last
                                                 AND d.gvkey || ':' || d.iid = s.security_id))
                    AND (NOT s.is_etf OR coalesce(s.inception <= n.last, false))   -- no inception day: no link
        UNION ALL SELECT c.permno, m.security_id, 4, 'compustat' FROM c JOIN comp m ON m.c8 = c.cusip),
    best AS (SELECT cand.*, p.last FROM cand JOIN p USING (permno)
             QUALIFY row_number() OVER (PARTITION BY permno ORDER BY rule, security_id) = 1)
    SELECT permno, security_id, how FROM best
    QUALIFY row_number() OVER (PARTITION BY security_id ORDER BY rule, last DESC, permno) = 1"""

# ── CRSP, the narrow fallback (master plan of 2 Nov 2026, item 2) ─────────────────────────────────────────────
# A CRSP bar is served only on a day of ``CRSP_FALLBACK``; every other CRSP row stays in ``_hist_all`` for the
# difference report and is never a served bar. The served CRSP bars are exactly the fallback rows (``source`` =
# ``wrds/crsp``; the build note counts them per reason). Only a PERMNO linked by an identifier counts (rules
# ``hdrcusip``, ``cusip``, ``compustat`` of ``PERMNO_LINK``), never one linked by ticker alone. CRSP also stays the
# cross-check of every Compustat bar (``wrds_price_diff``).
LINKED_BY_ID = "l.how <> 'ticker'"
# The Compustat security of a ``comp.secd`` row, as ``COMP_BARS`` keys it.
_COMP_ID = ("coalesce((SELECT a.security_id FROM _gvkey_alias a WHERE a.gid = s.gvkey || ':' || s.iid), "
            "s.gvkey || ':' || s.iid)")
# Placeholder for ``delisted_final_days`` until the coordinator's measurement is in (see its TODO).
DELISTED_WINDOW_DAYS = 31


def delisted_final_days(window_days: int = DELISTED_WINDOW_DAYS) -> str:
    """The CRSP days of a delisted security that Compustat does not price: ``(permno, session_date, why)``.

    A PERMNO is delisted when its last ``dsf_v2`` day is before CRSP's newest staged day; its final days are those
    after its security's last priced ``comp.secd`` day, at most ``window_days`` calendar days after it. A security
    Compustat never priced has no final days here (its history is CRSP's alone and is not served)."""
    # TODO(coordinator): the delisted-names measurement is pending. Its final numbers set how many days after
    # Compustat's last priced day are CRSP's (``window_days`` is a placeholder) and whether dsf_v2's final
    # ``dlyret`` carries the delisting return to serve; until then this rule serves the bars as they are.
    return f"""
    SELECT d.permno, d.dlycaldt AS session_date, 'delisted' AS why
    FROM stg_wrds_dsf d JOIN _permno_link l USING (permno)
    JOIN (SELECT permno, max(dlycaldt) AS last FROM stg_wrds_dsf GROUP BY permno) p USING (permno)
    JOIN (SELECT {_COMP_ID} AS security_id, max(datadate) AS last FROM stg_wrds_secd s WHERE prccd > 0 GROUP BY 1) c
        ON c.security_id = l.security_id
    WHERE {LINKED_BY_ID} AND p.last < (SELECT max(dlycaldt) FROM stg_wrds_dsf)
      AND d.dlycaldt > c.last AND d.dlycaldt <= c.last + INTERVAL {int(window_days)} DAY"""


# The CRSP days served (``serving`` makes it the temp table ``_crsp_fallback`` after ``_gvkey_alias``): a delisted
# security's final days (``delisted_final_days``) only. A day Compustat prices without ``trfd`` is Compustat's
# (``COMP_RETURN``): the coordinator's measurement of 3 Nov 2026 dropped the "empty trfd -> CRSP dlyret" step.
CRSP_FALLBACK = delisted_final_days()

# The splits CRSP's cumulative price factor carries (Order 4 of the 2 Nov 2026 master plan: on homesrv 1,167 open
# ``price_gap_no_action`` issues sat on a split ``dlycumfacpr`` records and ``corporate_action`` lacked, 22 of them
# Tier A). A day whose ``dlycumfacpr`` differs from the previous row's of the same answer is a split of ratio previous
# / today's (new over old shares; the fixture's convention, the factor of the older days the larger, as ``ajexdi``),
# keyed on the PERMNO's security when an identifier links it. ``prices/serving`` adds one only where no split of
# another source is within ``serving.SPLIT_SAME_DAYS`` of it. In ``_act``'s shape (``serving`` reads it after
# ``_permno_link``).
# shortcut: the direction of dlycumfacpr is the fixture's, not read from WRDS here; check one known split on homesrv
# (AAPL's 4-for-1 ex 2020-08-31) before the switch, and flip the ratio if CRSP's older factor is the smaller.
CRSP_SPLITS = f"""
    SELECT l.security_id, 'wrds:crspsplit:' || x.permno || ':' || CAST(x.dlycaldt AS TEXT) AS action_id,
           CASE WHEN ratio < 1 THEN 'reverse_split' ELSE 'forward_split' END AS action_type, x.dlycaldt AS ex_date,
           x.dlycaldt AS eff, 1.0 AS old_rate, ratio AS new_rate, CAST(NULL AS DOUBLE) AS rate, false AS special,
           CAST(NULL AS TEXT) AS currency, CAST(NULL AS DATE) AS record_date, CAST(NULL AS DATE) AS payable_date,
           '{NAME}' AS source, x.source_as_of, x.fetched_at, x.run_id
    FROM (SELECT permno, dlycaldt, source_as_of, fetched_at, run_id, lag(dlycumfacpr) OVER w / dlycumfacpr AS ratio,
                 lag(run_id) OVER w AS prev_run
          FROM stg_wrds_dsf WHERE dlycumfacpr > 0
            AND permno IN (SELECT permno FROM stg_wrds_dsf WHERE dlycumfacpr > 0 GROUP BY permno
                           HAVING min(dlycumfacpr) <> max(dlycumfacpr))
          WINDOW w AS (PARTITION BY permno ORDER BY dlycaldt)) x
    JOIN _permno_link l USING (permno)
    WHERE {LINKED_BY_ID} AND x.prev_run = x.run_id AND abs(ln(ratio)) > 1e-6"""

# Compustat's ``gvkey:iid`` of a PERMNO whose own link is a security the spine names otherwise (``figi:``, ``cik:``,
# ``sym:``: the ETFs), when the PERMNO's CUSIP matches it and the spine holds no such ``gvkey:iid``. Its Compustat bars,
# actions and revisions are served as that security's, so one fund is one security (review S2 of #353; ``serving``
# makes it the temp table ``_gvkey_alias`` after ``_permno_link``). One security per ``gvkey:iid``.
GVKEY_ALIAS = """
    WITH c AS (SELECT DISTINCT permno, left(cusip, 8) AS c8 FROM stg_wrds_dsf WHERE cusip IS NOT NULL),
    comp AS (SELECT DISTINCT gvkey || ':' || iid AS gid, left(cusip, 8) AS c8 FROM stg_wrds_secd WHERE cusip IS NOT NULL)
    SELECT m.gid, l.security_id FROM _permno_link l JOIN c USING (permno) JOIN comp m USING (c8)
    WHERE (l.security_id LIKE 'figi:%' OR l.security_id LIKE 'cik:%' OR l.security_id LIKE 'sym:%')
      AND m.gid NOT IN (SELECT security_id FROM stg_spine WHERE security_id IS NOT NULL)
      AND m.gid NOT IN (SELECT security_id FROM _permno_link)        -- another PERMNO's own rule-4 security
    QUALIFY row_number() OVER (PARTITION BY m.gid ORDER BY l.security_id) = 1"""

# Every history bar of both sources (``PRICE_BAR``'s columns, then the source's own day return and the serving
# priority): ``prices/serving`` serves one per session (Compustat first, priority 1) and compares them all.
CRSP_BARS = f"""
    SELECT coalesce(l.security_id, 'permno:' || d.permno) AS security_id, dlycaldt AS session_date,
           CAST(NULL AS TEXT) AS symbol, CAST(NULL AS TEXT) AS cik, dlyopen AS open, dlyhigh AS high, dlylow AS low,
           abs(dlyprc) AS close, dlyvol AS volume, CAST(NULL AS DOUBLE) AS vwap, CAST(NULL AS BIGINT) AS trade_count,
           shrout * {CRSP_SHARES_UNIT} AS shares_outstanding, '{CRSP}' AS source, source_as_of, fetched_at, run_id,
           dlyret AS own_return, 'dlyret' AS return_basis, 2 AS priority, f.why AS fallback
    FROM stg_wrds_dsf d LEFT JOIN _permno_link l USING (permno)
    LEFT JOIN _crsp_fallback f ON f.permno = d.permno AND f.session_date = d.dlycaldt
    WHERE dlyprc IS NOT NULL AND dlyprc <> 0"""
COMP_BARS = f"""
    SELECT coalesce((SELECT a.security_id FROM _gvkey_alias a WHERE a.gid = gvkey || ':' || iid), gvkey || ':' || iid)
               AS security_id, datadate AS session_date, replace(upper(tic), '.', '-') AS symbol,
           CAST(NULL AS TEXT) AS cik, prcod AS open, prchd AS high, prcld AS low, prccd AS close, cshtrd AS volume,
           CAST(NULL AS DOUBLE) AS vwap, CAST(NULL AS BIGINT) AS trade_count, cshoc AS shares_outstanding,
           '{COMPUSTAT}' AS source, source_as_of, fetched_at, run_id, {COMP_RETURN} AS own_return,
           {COMP_RETURN_BASIS} AS return_basis, 1 AS priority, CAST(NULL AS TEXT) AS fallback
    -- secd holds rows of events to come (to 2027): a bar is a priced day up to today only.
    FROM stg_wrds_secd WHERE coalesce(upper(curcdd), 'USD') = 'USD' AND prccd > 0 AND datadate <= current_date
    WINDOW w AS (PARTITION BY gvkey, iid ORDER BY datadate)"""

# The splits ``ajexdi`` (Compustat's cumulative adjustment factor, adjusted price = ``prccd / ajexdi``) carries:
# a day whose ``ajexdi`` differs from the previous row's of the same answer is a split of ratio previous / today's
# (new over old shares), unless ``comp.sec_split`` holds one in between. In ``_act``'s shape.
# shortcut: a split on the first session of a year partition asked by another run than the year before it is not
# seen here (``COMP_RETURN``'s rule); ``comp.sec_split`` and the gap issue still catch it.
AJEXDI_SPLITS = f"""
    SELECT gvkey || ':' || iid AS security_id,
           'wrds:ajexdi:' || gvkey || ':' || iid || ':' || CAST(datadate AS TEXT) AS action_id,
           CASE WHEN ratio < 1 THEN 'reverse_split' ELSE 'forward_split' END AS action_type, datadate AS ex_date,
           datadate AS eff, 1.0 AS old_rate, ratio AS new_rate, CAST(NULL AS DOUBLE) AS rate, false AS special,
           CAST(NULL AS TEXT) AS currency, CAST(NULL AS DATE) AS record_date, CAST(NULL AS DATE) AS payable_date,
           '{NAME}' AS source, source_as_of, fetched_at, run_id
    FROM (SELECT *, lag(ajexdi) OVER w / ajexdi AS ratio, lag(run_id) OVER w AS prev_run,
                 lag(datadate) OVER w AS prev_day
          FROM stg_wrds_secd WHERE ajexdi > 0 AND prccd > 0 AND coalesce(upper(curcdd), 'USD') = 'USD'
            -- the window only over the securities whose ajexdi changes at all (a hash aggregate, not a sort)
            AND (gvkey, iid) IN (SELECT (gvkey, iid) FROM stg_wrds_secd WHERE ajexdi > 0 GROUP BY gvkey, iid
                                 HAVING min(ajexdi) <> max(ajexdi))
          WINDOW w AS (PARTITION BY gvkey, iid ORDER BY datadate)) x
    WHERE prev_run = run_id AND abs(ln(ratio)) > 1e-6
      AND NOT EXISTS (SELECT 1 FROM stg_wrds_split s WHERE s.gvkey = x.gvkey AND s.iid = x.iid AND s.split > 0
                      AND s.split <> 1 AND s.datadate > x.prev_day AND s.datadate <= x.datadate)"""

# Compustat's splits (``comp.sec_split``, then ``AJEXDI_SPLITS``) and dividends in the shape of ``_act`` (the Alpaca
# actions' columns, by name).
_ACTIONS = f"""
    SELECT gvkey || ':' || iid AS security_id, 'wrds:split:' || gvkey || ':' || iid || ':' || CAST(datadate AS TEXT) AS action_id,
           CASE WHEN split < 1 THEN 'reverse_split' ELSE 'forward_split' END AS action_type, datadate AS ex_date,
           datadate AS eff, 1.0 AS old_rate, split AS new_rate, CAST(NULL AS DOUBLE) AS rate, false AS special,
           CAST(NULL AS TEXT) AS currency, CAST(NULL AS DATE) AS record_date, CAST(NULL AS DATE) AS payable_date,
           '{NAME}' AS source, source_as_of, fetched_at, run_id
    FROM stg_wrds_split WHERE split > 0 AND split <> 1
    UNION ALL
    SELECT gvkey || ':' || iid, 'wrds:secddiv:' || gvkey || ':' || iid || ':' || CAST(datadate AS TEXT), 'cash_dividend',
           datadate, datadate, NULL, NULL, coalesce(divd, div), false, coalesce(curcddv, 'USD'), NULL, NULL, '{NAME}',
           source_as_of, fetched_at, run_id
    FROM stg_wrds_secd WHERE coalesce(divd, div) > 0
    UNION ALL
    SELECT gvkey || ':' || iid, 'wrds:secddivsp:' || gvkey || ':' || iid || ':' || CAST(datadate AS TEXT),
           'cash_dividend', datadate, datadate, NULL, NULL, divsp, true, coalesce(curcddv, 'USD'), NULL, NULL, '{NAME}',
           source_as_of, fetched_at, run_id
    FROM stg_wrds_secd WHERE divsp > 0
    -- comp.sec_divid only where the secd row of that security and day carries no distribution of the kind
    UNION ALL
    SELECT gvkey || ':' || iid, 'wrds:div:' || gvkey || ':' || iid || ':' || CAST(datadate AS TEXT), 'cash_dividend', datadate,
           datadate, NULL, NULL, coalesce(divd, div), false, coalesce(curcddv, 'USD'), recorddate, paydate, '{NAME}',
           source_as_of, fetched_at, run_id
    FROM stg_wrds_divid v WHERE coalesce(divd, div) > 0 AND NOT EXISTS (SELECT 1 FROM stg_wrds_secd s
        WHERE s.gvkey = v.gvkey AND s.iid = v.iid AND s.datadate = v.datadate AND coalesce(s.divd, s.div) > 0)
    UNION ALL
    SELECT gvkey || ':' || iid, 'wrds:divsp:' || gvkey || ':' || iid || ':' || CAST(datadate AS TEXT), 'cash_dividend', datadate,
           datadate, NULL, NULL, divsp, true, coalesce(curcddv, 'USD'), recorddate, paydate, '{NAME}', source_as_of,
           fetched_at, run_id
    FROM stg_wrds_divid v WHERE divsp > 0 AND NOT EXISTS (SELECT 1 FROM stg_wrds_secd s
        WHERE s.gvkey = v.gvkey AND s.iid = v.iid AND s.datadate = v.datadate AND s.divsp > 0)
    UNION ALL SELECT * FROM ({AJEXDI_SPLITS})"""
# The actions served: a ``gvkey:iid`` that ``_gvkey_alias`` names is its security's (review S2 of #353).
ACTIONS_SQL = f"""
    SELECT coalesce(a.security_id, w.security_id) AS security_id, w.* EXCLUDE (security_id)
    FROM ({_ACTIONS}) w LEFT JOIN _gvkey_alias a ON a.gid = w.security_id"""

# The revisions of the history bars, keyed on their security (a CRSP one through ``_permno_link``).
REVISIONS_SQL = f"""
    SELECT CASE WHEN r.source = '{CRSP}' THEN coalesce(
               (SELECT l.security_id FROM _permno_link l WHERE l.permno = r.security_key), 'permno:' || r.security_key)
           ELSE coalesce((SELECT a.security_id FROM _gvkey_alias a WHERE a.gid = r.security_key), r.security_key)
           END AS security_id, r.session_date, r.symbol, r.open, r.high, r.low, r.close, r.volume,
           r.source, r.fetched_at, r.run_id, r.revised_at, r.revised_by
    FROM stg_wrds_revisions r"""


DATASET = WrdsPrices()
