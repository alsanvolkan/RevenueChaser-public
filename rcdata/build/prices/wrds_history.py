"""``wrds/prices``: the price history from WRDS, Compustat's ``comp.secd`` from 1984 and CRSP's ``crsp.dsf_v2`` to 2025,
with Compustat's splits (``comp.sec_split``) and dividends (``comp.sec_divid``) and CRSP's link to Compustat
(``crsp.ccmxpf_lnkhist``).

**Not run against the real source.** Built against the tables as Compustat and CRSP document them; the column
names are not read from WRDS here (no cloud thread logs in), so a column the store needs that comes back empty for a
whole answer is a ``wrds_column_empty`` run issue, and the first attended run is the check. The long history run is
Phase 8 (avo, 27 Oct 2026); this PR ships the code.

**Attended**, like the spine's WRDS datasets (``spine/wrds.py``): never due, run only from the CLI on the server
(``python -m RevenueChaser.rcdata.build backfill wrds/prices``), on the one WRDS worker of the process (one login,
one Duo push). Every query counts as one ``wrds`` call against the day cap (``rcdata/caps.py``); a backfill that
reaches the cap stops, keeps and serves the years it has, and the next backfill goes on from the first missing one.

**What is asked.** One query per table and calendar year (``year_sql``): ``comp.secd`` on the US exchanges
(``exchg`` 11, 12, 14: NYSE, NYSE American, Nasdaq) and ``comp.sec_divid`` from ``START_YEAR``, ``crsp.dsf_v2``
from ``START_YEAR`` to ``CRSP_LAST_YEAR``; ``comp.sec_split`` and the CRSP link whole (``SNAPSHOT_SQL``). A
**backfill** asks the snapshots first (the links and splits the years need), then the years newest first and,
within a year, the three tables; **a partition that exists is skipped** with no query, and a raw file that exists
is typed again rather than asked again (a run killed between the two). The **monthly run** (``fetch``) asks the
last ``REFRESH_YEARS`` years of each table again, and the snapshots when the newest is ``SNAPSHOT_DAYS`` old.

**Raw**: ``raw/wrds/prices/<table>/year=<Y>/<as_of>.jsonl.gz`` (a snapshot: ``raw/wrds/prices/<table>/
<as_of>.jsonl.gz``), each with the worker's ``.meta.json`` sidecar. **Parquet**: ``parquet/wrds/prices/<table>/
year=<Y>/part-0.parquet`` (a snapshot: ``.../snapshot=<as_of>/part-0.parquet``); a refresh replaces the year whole,
and first keeps every bar value it replaces or drops (``secd`` and ``dsf``: open, high, low, close, volume) in
``parquet/wrds/prices/revisions/<table>/year=<Y>/part-<run_id>.parquet`` (``price_revision`` in the served file).

**Without CRSP.** A subscription that holds Compustat but not CRSP (homesrv's: run 32 on 30 Oct 2026 was refused
``crsp.ccmxpf_lnkhist`` with "permission denied for schema crsp_a_ccm") answers a CRSP query with PostgreSQL's
``InsufficientPrivilege`` (``spine/wrds.PermissionRefused``). The run then skips CRSP: the refusal is a
``wrds_no_access`` issue keyed by the schema and a run note, every later CRSP query of the run is skipped unasked,
and the history is Compustat's (``secd`` bars with ``cshoc`` shares, so the caps; ``sec_divid``; ``sec_split``). No
file is left for the refused query, so the next backfill asks it once again and a subscription that gains CRSP is
picked up. The CRSP rows of ``wrds_price_diff`` then count no session. A refused Compustat table, and every other
error, still fails the run.

**The columns first.** The first query of a run that asks anything is ``information_schema.columns`` for the five
tables (``fundamentals/wrds_fund.catalog``, the columns this login can read; one ``wrds`` call, the same login, so no
second Duo): a Compustat table the login cannot read, or a column a query names that WRDS lacks (``ASKED_COLUMNS``,
``exchg`` among them), fails the run before any data is asked; a CRSP table missing from it is skipped as above.

**Served** by ``prices/serving``: the bars (``COMP_BARS``, ``CRSP_BARS``; CRSP first where linked, an Alpaca bar
always winning its session), the actions (``ACTIONS_SQL``; an Alpaca action of the same kind and day winning), the
revisions, and the difference report ``wrds_price_diff`` (``COMPARISONS``). Compustat's own adjusted return is
``COMP_RETURN``: ``prccd / ajexdi * trfd`` over the previous session's.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import duckdb

from ....core import calendar
from .. import base
from ..fundamentals import wrds_fund
from ..spine import wrds
from ..spine.security import US_EXCHG
from . import KIND, stage

NAME = "wrds/prices"
COMPUSTAT, CRSP = "wrds/compustat", "wrds/crsp"          # the bar sources in ``price_bar_daily``
START_YEAR = 1984
CRSP_LAST_YEAR = 2025                                    # the CRSP subscription's last year
REFRESH_YEARS = 2
SNAPSHOT_DAYS = 30
# CRSP's ``shrout`` is in thousands of shares (CRSP's documentation; not read from WRDS here).
CRSP_SHARES_UNIT = 1000
RAW_SUFFIX = ".jsonl.gz"
PROVENANCE = [("source_as_of", "DATE"), ("fetched_at", "DOUBLE"), ("run_id", "TEXT")]

SECD = [("gvkey", "TEXT"), ("iid", "TEXT"), ("datadate", "DATE"), ("tic", "TEXT"), ("cusip", "TEXT"),
        ("curcdd", "TEXT"), ("prccd", "DOUBLE"), ("prcod", "DOUBLE"), ("prchd", "DOUBLE"), ("prcld", "DOUBLE"),
        ("cshtrd", "DOUBLE"), ("cshoc", "DOUBLE"), ("ajexdi", "DOUBLE"), ("trfd", "DOUBLE"), ("exchg", "INTEGER")]
DSF = [("permno", "TEXT"), ("dlycaldt", "DATE"), ("dlyprc", "DOUBLE"), ("dlyopen", "DOUBLE"), ("dlyhigh", "DOUBLE"),
       ("dlylow", "DOUBLE"), ("dlyvol", "DOUBLE"), ("dlyret", "DOUBLE"), ("shrout", "DOUBLE")]
DIVID = [("gvkey", "TEXT"), ("iid", "TEXT"), ("datadate", "DATE"), ("div", "DOUBLE"), ("divd", "DOUBLE"),
         ("divsp", "DOUBLE"), ("curcddv", "TEXT"), ("paydate", "DATE"), ("recorddate", "DATE")]
SPLIT = [("gvkey", "TEXT"), ("iid", "TEXT"), ("datadate", "DATE"), ("split", "DOUBLE")]
LINK = [("gvkey", "TEXT"), ("iid", "TEXT"), ("permno", "TEXT"), ("linktype", "TEXT"), ("linkprim", "TEXT"),
        ("linkdt", "DATE"), ("linkenddt", "DATE")]
REVISION = [("source", "TEXT"), ("security_key", "TEXT"), ("session_date", "DATE"), ("symbol", "TEXT"),
            ("open", "DOUBLE"), ("high", "DOUBLE"), ("low", "DOUBLE"), ("close", "DOUBLE"), ("volume", "DOUBLE"),
            ("fetched_at", "DOUBLE"), ("run_id", "TEXT"), ("revised_at", "DOUBLE"), ("revised_by", "TEXT")]

_EXCHG = ", ".join(str(e) for e in US_EXCHG)
# table -> its spec. ``date``: the column a year's query ranges over (None: asked whole, as a snapshot).
# ``needed``: the columns the store's numbers come from (an answer with rows and none of them is a run issue).
# ``bar``: the source label and the (security key, symbol, open, high, low, close, volume) columns of a bar
# table, for the revisions.
TABLES: dict[str, dict] = {
    "secd": {"wrds": "comp.secd", "schema": SECD, "key": ["gvkey", "iid", "datadate"], "date": "datadate",
             "last_year": None, "needed": ("prccd", "ajexdi", "trfd", "cshoc"),
             "sql": ("SELECT gvkey, iid, datadate, tic, cusip, curcdd, prccd, prcod, prchd, prcld, cshtrd, cshoc, "
                     "ajexdi, trfd, exchg FROM comp.secd WHERE {range} AND exchg IN (" + _EXCHG + ")"),
             "bar": (COMPUSTAT, "gvkey || ':' || iid", "replace(upper(tic), '.', '-')", "prcod", "prchd", "prcld",
                     "prccd", "cshtrd")},
    "dsf": {"wrds": "crsp.dsf_v2", "schema": DSF, "key": ["permno", "dlycaldt"], "date": "dlycaldt",
            "last_year": CRSP_LAST_YEAR, "needed": ("dlyprc", "dlyret", "shrout"),
            "sql": ("SELECT permno, dlycaldt, dlyprc, dlyopen, dlyhigh, dlylow, dlyvol, dlyret, shrout "
                    "FROM crsp.dsf_v2 WHERE {range}"),
            "bar": (CRSP, "permno", "CAST(NULL AS TEXT)", "dlyopen", "dlyhigh", "dlylow", "abs(dlyprc)", "dlyvol")},
    "sec_divid": {"wrds": "comp.sec_divid", "schema": DIVID, "key": ["gvkey", "iid", "datadate"], "date": "datadate",
                  "last_year": None, "needed": ("divd",), "sql": "SELECT * FROM comp.sec_divid WHERE {range}"},
    "sec_split": {"wrds": "comp.sec_split", "schema": SPLIT, "key": ["gvkey", "iid", "datadate"], "date": None,
                  "needed": ("split",), "sql": "SELECT * FROM comp.sec_split"},
    "link": {"wrds": "crsp.ccmxpf_lnkhist", "schema": LINK, "key": ["gvkey", "iid", "permno", "linkdt"], "date": None,
             "needed": ("permno",),
             "sql": ("SELECT gvkey, liid AS iid, lpermno AS permno, linktype, linkprim, linkdt, linkenddt "
                     "FROM crsp.ccmxpf_lnkhist WHERE linktype IN ('LU', 'LC') AND linkprim IN ('P', 'C') "
                     "AND lpermno IS NOT NULL")},
}
YEARLY = ("secd", "dsf", "sec_divid")
SNAPSHOTS = ("sec_split", "link")
# CRSP's tables: a subscription without CRSP (or without CCM, the link) answers them with a permission refusal,
# and the history is then Compustat's alone (``_skip_crsp``).
CRSP_TABLES = ("dsf", "link")
# The WRDS columns each query names (its SELECT list and filters), checked against ``information_schema.columns``
# before any data is asked (``_check_columns``): a name WRDS does not have fails the run on that one query, not on a
# data query after the Duo push.
ASKED_COLUMNS = {"secd": [c for c, _ in SECD], "dsf": [c for c, _ in DSF],
                 "sec_divid": ["gvkey", "iid", "datadate", "divd"], "sec_split": [c for c, _ in SPLIT],
                 "link": ["gvkey", "liid", "lpermno", "linktype", "linkprim", "linkdt", "linkenddt"]}
STAGING = {"secd": "stg_wrds_secd", "dsf": "stg_wrds_dsf", "sec_divid": "stg_wrds_divid",
           "sec_split": "stg_wrds_split", "link": "stg_wrds_link"}

# Compustat's adjusted price and its day return (WRDS: total return = (prccd / ajexdi * trfd) over the previous
# day's, minus 1), over a window ``w`` of one security by date.
COMP_ADJ = "prccd / ajexdi * trfd"
COMP_RETURN = f"({COMP_ADJ}) / lag({COMP_ADJ}) OVER w - 1"

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
    attended = True
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
        fails the run here; a CRSP library the login cannot read is skipped as a refusal (``wrds_no_access``)."""
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
                if partition_path(table, year).is_file():
                    continue
                have = sorted(raw_dir(table, year).glob(f"*{RAW_SUFFIX}"))
                raw = have[-1] if have else self._ask_or_skip(ctx, table, year, end, run)
                if raw is None:
                    continue
                self.to_parquet_all(ctx, [raw])
                asked += 1
        except base.CapReached as e:
            ctx.notes.append(f"stopped at the WRDS day cap ({e}) after {asked} partition(s); the next backfill "
                             "goes on from the first missing year")

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
                         [f for _, f in stage.partitions(base.parquet_root() / "wrds" / "prices" / table, "year")])
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

# CRSP's rows with the Compustat security of their date (a primary link first); an unlinked one keeps its PERMNO.
_LINKED = """
    SELECT d.*, l.gvkey, l.iid FROM stg_wrds_dsf d
    LEFT JOIN stg_wrds_link l ON l.permno = d.permno AND d.dlycaldt >= coalesce(l.linkdt, DATE '0001-01-01')
         AND (l.linkenddt IS NULL OR d.dlycaldt <= l.linkenddt)
    QUALIFY row_number() OVER (PARTITION BY d.permno, d.dlycaldt ORDER BY l.linkprim = 'P' DESC NULLS LAST,
                                                                          l.gvkey NULLS LAST) = 1"""

# Every history bar of both sources (``PRICE_BAR``'s columns, then the source's own day return and the previous
# session it is over): ``prices/serving`` serves one per session (CRSP first) and compares them all.
CRSP_BARS = f"""
    SELECT coalesce(gvkey || ':' || iid, 'permno:' || permno) AS security_id, dlycaldt AS session_date,
           CAST(NULL AS TEXT) AS symbol, CAST(NULL AS TEXT) AS cik, dlyopen AS open, dlyhigh AS high, dlylow AS low,
           abs(dlyprc) AS close, dlyvol AS volume, CAST(NULL AS DOUBLE) AS vwap, CAST(NULL AS BIGINT) AS trade_count,
           shrout * {CRSP_SHARES_UNIT} AS shares_outstanding, '{CRSP}' AS source, source_as_of, fetched_at, run_id,
           dlyret AS own_return, 1 AS priority
    FROM ({_LINKED}) WHERE dlyprc IS NOT NULL AND dlyprc <> 0"""
COMP_BARS = f"""
    SELECT gvkey || ':' || iid AS security_id, datadate AS session_date, replace(upper(tic), '.', '-') AS symbol,
           CAST(NULL AS TEXT) AS cik, prcod AS open, prchd AS high, prcld AS low, prccd AS close, cshtrd AS volume,
           CAST(NULL AS DOUBLE) AS vwap, CAST(NULL AS BIGINT) AS trade_count, cshoc AS shares_outstanding,
           '{COMPUSTAT}' AS source, source_as_of, fetched_at, run_id, {COMP_RETURN} AS own_return, 2 AS priority
    FROM stg_wrds_secd WHERE coalesce(upper(curcdd), 'USD') = 'USD' AND prccd > 0
    WINDOW w AS (PARTITION BY gvkey, iid ORDER BY datadate)"""

# Compustat's splits and dividends in the shape of ``_act`` (the Alpaca actions' columns, by name).
ACTIONS_SQL = f"""
    SELECT gvkey || ':' || iid AS security_id, 'wrds:split:' || gvkey || ':' || iid || ':' || CAST(datadate AS TEXT) AS action_id,
           CASE WHEN split < 1 THEN 'reverse_split' ELSE 'forward_split' END AS action_type, datadate AS ex_date,
           datadate AS eff, 1.0 AS old_rate, split AS new_rate, CAST(NULL AS DOUBLE) AS rate, false AS special,
           CAST(NULL AS TEXT) AS currency, CAST(NULL AS DATE) AS record_date, CAST(NULL AS DATE) AS payable_date,
           '{NAME}' AS source, source_as_of, fetched_at, run_id
    FROM stg_wrds_split WHERE split > 0 AND split <> 1
    UNION ALL
    SELECT gvkey || ':' || iid, 'wrds:div:' || gvkey || ':' || iid || ':' || CAST(datadate AS TEXT), 'cash_dividend', datadate,
           datadate, NULL, NULL, coalesce(divd, div), false, coalesce(curcddv, 'USD'), recorddate, paydate, '{NAME}',
           source_as_of, fetched_at, run_id
    FROM stg_wrds_divid WHERE coalesce(divd, div) > 0
    UNION ALL
    SELECT gvkey || ':' || iid, 'wrds:divsp:' || gvkey || ':' || iid || ':' || CAST(datadate AS TEXT), 'cash_dividend', datadate,
           datadate, NULL, NULL, divsp, true, coalesce(curcddv, 'USD'), recorddate, paydate, '{NAME}', source_as_of,
           fetched_at, run_id
    FROM stg_wrds_divid WHERE divsp > 0"""

# The revisions of the history bars, keyed on the security of their date.
REVISIONS_SQL = f"""
    SELECT CASE WHEN r.source = '{CRSP}' THEN coalesce(
               (SELECT l.gvkey || ':' || l.iid FROM stg_wrds_link l WHERE l.permno = r.security_key
                AND r.session_date >= coalesce(l.linkdt, DATE '0001-01-01')
                AND (l.linkenddt IS NULL OR r.session_date <= l.linkenddt)
                ORDER BY l.linkprim = 'P' DESC LIMIT 1), 'permno:' || r.security_key)
           ELSE r.security_key END AS security_id, r.session_date, r.symbol, r.open, r.high, r.low, r.close, r.volume,
           r.source, r.fetched_at, r.run_id, r.revised_at, r.revised_by
    FROM stg_wrds_revisions r"""


DATASET = WrdsPrices()
