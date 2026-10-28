"""``wrds/fundq``, ``wrds/funda``, ``wrds/firm_ratio``: Compustat's statements and WRDS's ratios, a check of the SEC.

**Not run against the real source.** Built against the documented tables (``comp.fundq``, ``comp.funda``,
``wrds_finratio.firm_ratio``); the tests answer from a fake worker. The first real run is the attended homesrv
backfill.

**A check source, not a served value.** The served statement values are the SEC's (``sec/companyfacts``). What WRDS
adds: (1) **the revision table**: each monthly snapshot of ``fundq`` / ``funda`` against the one before, every value
that changed for a period both hold (a restatement, or a correction at Compustat), served as
``fundamental_revision``; a new period is not a revision; (2) **the agreement notes**: revenue, net income and
diluted EPS against the SEC's facts of the same period (Compustat's millions times a million, within 0.5%; EPS
within a cent; period ends within 7 days, for the 52/53-week years); (3) **the debt check**: WRDS's ``debt_at``
(total debt over total assets) of each company's newest quarter against the SEC's total debt over total
assets, within 0.02. ``debt_at`` sums long-term debt and debt in current liabilities, so a company with short-term
borrowings outside those differs by design; the note counts, it does not fail.

**Attended and monthly, on the spine's one WRDS worker** (``spine/wrds.py``: one login, at most one Duo push per
builder process, ``WRDS_USERNAME``, ``RC_WRDS_PYTHON``): never due; ``python -m RevenueChaser.rcdata.build backfill
wrds/fundq wrds/funda wrds/firm_ratio wrds/segments`` in a foreground terminal on the server. A run asks a table once its newest
snapshot is ``SNAPSHOT_DAYS`` old (the CLI's backfill asks it now); one query per table, for the Tier A gvkeys (the
spine's Compustat link), Compustat's standard rows (``COMPUSTAT_FILTER``), ``YEARS`` of periods (``firm_ratio``:
``RATIO_YEARS`` of publication dates). Every query counts as one ``wrds`` call.

Raw: ``raw/wrds/<table>/<as-of>/<table>.jsonl.gz`` with a ``.meta.json`` sidecar (the query, rows, run); an existing
raw file is not asked again. Parquet: ``parquet/wrds/<table>/snapshot=<as-of>/<table>.parquet`` and, from the second
snapshot on, ``.../revision=<as-of>/<table>.parquet`` (its changes against the snapshot before). Staging:
``stg_wrds_<table>`` (the newest snapshot) and ``stg_wrds_<table>_revision`` (every revision file).
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import duckdb

from ....core import calendar
from .. import base
from ..spine import wrds as wrds_session
from . import KIND, common, facts

PROVIDER = wrds_session.PROVIDER
SNAPSHOT_DAYS = 30
YEARS = 10
RATIO_YEARS = 3
COMPUSTAT_FILTER = "indfmt = 'INDL' AND datafmt = 'STD' AND popsrc = 'D' AND consol = 'C'"
PERIOD_DAYS = 7
MONEY_TOLERANCE = 0.005
EPS_TOLERANCE = 0.01
DEBT_TOLERANCE = 0.02
MILLION = 1_000_000.0
SEGMENT_LIBRARY = "comp_segments_hist_daily"
SEGMENT_YEARS = 5
# Compustat's segment types: business and operating segments are one axis here, geographic the other.
SEGMENT_TYPES = {"BUSSEG": "business", "OPSEG": "business", "GEOSEG": "geographic"}
_GVKEY = re.compile(r"^\d{6}$")

REVISION = [("source", "TEXT"), ("gvkey", "TEXT"), ("datadate", "DATE"), ("item", "TEXT"), ("old_value", "DOUBLE"),
            ("new_value", "DOUBLE"), ("old_as_of", "DATE"), ("new_as_of", "DATE"), ("run_id", "TEXT")]
PROVENANCE = [("source_as_of", "DATE"), ("fetched_at", "DOUBLE"), ("run_id", "TEXT")]


def _years_before(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year - years)
    except ValueError:                                   # 29 February
        return day.replace(year=day.year - years, day=28)


class WrdsFund:
    """One WRDS table of the fundamentals layer: a full snapshot a month, its revisions, its agreement note."""

    kind = KIND
    providers = (PROVIDER,)
    tables: tuple[str, ...] = ()
    cadence = "monthly"
    attended = True
    backfill_needs_from = False
    starts_by_hand = True

    def __init__(self, table: str, library: str, attrs: list[tuple[str, str]], items: list[str], key: list[str],
                 since: str, years: int, revisions: bool):
        self.table = table
        self.name = f"wrds/{table}"
        self.library = library
        self.items = items
        self.key = key
        self.schema = attrs + [(i, "DOUBLE") for i in items]
        self.since, self.years, self.revisions = since, years, revisions
        self.staging = f"stg_wrds_{table}"
        self.revision_staging = f"stg_wrds_{table}_revision"

    def default_as_of(self) -> date:
        return calendar.today_ny()

    # ── what is asked ──
    def sql(self, gvkeys: list[str], as_of: date) -> str:
        cols = ", ".join(c for c, _ in self.schema)
        where = [COMPUSTAT_FILTER] if self.library == "comp" else []
        where += [f"gvkey IN ({', '.join(repr(g) for g in gvkeys)})",
                  f"{self.since} >= DATE '{_years_before(as_of, self.years).isoformat()}'"]
        return f"SELECT {cols} FROM {self.library}.{self.table} WHERE {' AND '.join(where)}"

    def raw_path(self, as_of: date) -> Path:
        return common.raw_dir(self.name) / as_of.isoformat() / f"{self.table}{wrds_session.RAW_SUFFIX}"

    def snapshots(self) -> list[str]:
        folder = common.parquet_dir(self.name)
        if not folder.is_dir():
            return []
        return sorted(d.name.split("=", 1)[1] for d in folder.iterdir()
                      if d.name.startswith("snapshot=") and (d / f"{self.table}.parquet").is_file())

    def snapshot_file(self, as_of: str) -> Path:
        return common.parquet_dir(self.name) / f"snapshot={as_of}" / f"{self.table}.parquet"

    def fetch(self, ctx: base.RunContext, as_of: date) -> list[Path]:
        snaps = self.snapshots()
        if snaps and (as_of - date.fromisoformat(snaps[-1])).days < SNAPSHOT_DAYS \
                and not self.raw_path(as_of).exists():
            ctx.notes.append(f"snapshot of {snaps[-1]} is current (asked again after {SNAPSHOT_DAYS} days)")
            return []
        return self._full(ctx, as_of)

    def _full(self, ctx: base.RunContext, as_of: date) -> list[Path]:
        gvkeys = sorted({str(r["gvkey"]) for r in common.universe() if _GVKEY.match(str(r.get("gvkey") or ""))})
        if not gvkeys:
            ctx.notes.append("no Tier A company has a Compustat link yet (wrds/sec_idhist): nothing asked")
            return []
        # The spine's resumable ask (one query on the shared worker, its sidecar first).
        return [wrds_session._ask(ctx, self.raw_path(as_of), self.sql(gvkeys, as_of), as_of)]

    def backfill(self, ctx: base.RunContext, start: date | None, end: date) -> None:
        # WRDS answers the whole history in one snapshot: a backfill is a snapshot as of the end day.
        self.to_parquet_all(ctx, self._full(ctx, end))

    # ── type ──
    def to_parquet(self, ctx: base.RunContext, raw: Path) -> list[Path]:
        return self.to_parquet_all(ctx, [raw])

    def to_parquet_all(self, ctx: base.RunContext, raws: list[Path]) -> list[Path]:
        out = []
        for raw in raws:
            meta = base.read_raw_json(common.meta_path(raw))
            as_of = meta["as_of"]
            con = duckdb.connect()
            try:
                rows = int(con.execute(f"SELECT count(*) FROM ({wrds_session.raw_rows(raw, self.schema)})")
                           .fetchone()[0])
                if not rows:
                    raise base.SourceError(f"wrds {self.table}: the snapshot came back empty")
                types = dict(self.schema)
                for col, n in wrds_session.untyped(con, raw, self.schema).items():
                    ctx.issue("value_untyped", f"{self.table}.{col}", f"{n} row(s) of {rows} nulled "
                                                                     f"(as {types[col]})")
                keys = ", ".join(self.key)
                every = ", ".join(f'"{c}" NULLS LAST' for c, _ in self.schema)
                select = (f"SELECT *, DATE '{as_of}' AS source_as_of, CAST({float(meta['fetched_at'])} AS DOUBLE) "
                          f"AS fetched_at, '{meta['run_id']}' AS run_id "
                          f"FROM ({wrds_session.raw_rows(raw, self.schema)}) "
                          f"QUALIFY row_number() OVER (PARTITION BY {keys} ORDER BY {every}) = 1 ORDER BY {keys}")
                out.append(base.write_parquet(con, select, self.snapshot_file(as_of)))
                before = [s for s in self.snapshots() if s < as_of]
                if self.revisions and before:
                    out.append(base.write_parquet(con, self._revision_sql(before[-1], as_of, meta["run_id"]),
                                                  common.parquet_dir(self.name) / f"revision={as_of}" /
                                                  f"{self.table}.parquet"))
            finally:
                con.close()
            ctx.pending.setdefault(self.name, []).append(as_of)
        return out

    def _revision_sql(self, old: str, new: str, run_id: str) -> str:
        """The values of snapshot ``new`` that differ from snapshot ``old``, for the periods both hold."""
        items = ", ".join(f'"{i}"' for i in self.items)

        def long(as_of: str) -> str:
            return (f"(UNPIVOT (SELECT gvkey, datadate, {items} FROM read_parquet("
                    f"{base.sql_path(self.snapshot_file(as_of))})) ON {items} INTO NAME item VALUE value)")
        both = (f"SELECT gvkey, datadate FROM read_parquet({base.sql_path(self.snapshot_file(old))}) INTERSECT "
                f"SELECT gvkey, datadate FROM read_parquet({base.sql_path(self.snapshot_file(new))})")
        return f"""
            SELECT '{self.name}' AS source, coalesce(o.gvkey, n.gvkey) AS gvkey,
                   coalesce(o.datadate, n.datadate) AS datadate, coalesce(o.item, n.item) AS item,
                   o.value AS old_value, n.value AS new_value, DATE '{old}' AS old_as_of, DATE '{new}' AS new_as_of,
                   '{run_id}' AS run_id
            FROM {long(old)} o FULL JOIN {long(new)} n
              ON n.gvkey = o.gvkey AND n.datadate = o.datadate AND n.item = o.item
            WHERE o.value IS DISTINCT FROM n.value
              AND (coalesce(o.gvkey, n.gvkey), coalesce(o.datadate, n.datadate)) IN ({both})
            ORDER BY gvkey, datadate, item"""

    # ── stage ──
    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        snaps = self.snapshots()
        folder = common.parquet_dir(self.name)
        common.stage_glob(con, self.staging, self.schema + PROVENANCE, folder,
                          f"snapshot={snaps[-1]}/{self.table}.parquet" if snaps else "snapshot=none/none")
        if self.revisions:
            common.stage_glob(con, self.revision_staging, REVISION, folder, f"revision=*/{self.table}.parquet")
        return {}

    # ── check ──
    def check(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict:
        added = removed = 0
        if self.revisions:
            cols = ["gvkey", "datadate", "item", "old_value", "new_value", "new_as_of"]
            where = f"source = '{self.name}'"
            added, removed = common.compare_live(
                con, f"SELECT {', '.join(cols)} FROM fundamental_revision WHERE {where}", "fundamental_revision",
                cols, where=where)
        as_of = con.execute(f"SELECT max(source_as_of) FROM {self.staging}").fetchone()[0]
        ctx.notes.append(self.note(con))
        on = ctx.as_of or self.default_as_of()
        return {"dataset": self.name, "result": "changed" if (ctx.pending.get(self.name) or added or removed)
                else "unchanged", "source_as_of": (as_of or on).isoformat(),
                "rows": common.count(con, self.staging), "added": added, "removed": removed}

    def note(self, con: duckdb.DuckDBPyConnection) -> str:
        period_type = facts.QUARTERLY if self.table == "fundq" else facts.ANNUAL
        sfx = "q" if self.table == "fundq" else ""
        parts = []
        for label, field, item, tolerance in (
                ("revenue", "revenue", f"revt{sfx}", None), ("net income", "net_income", f"ni{sfx}", None),
                ("diluted EPS", "eps_diluted", f"epsfx{sfx}", EPS_TOLERANCE)):
            n, ok = agreement(con, self.staging, item, field, period_type, tolerance)
            parts.append(f"{label} {ok} of {n}")
        note = f"WRDS {self.table} vs the SEC: {', '.join(parts)} agree"
        snaps = self.snapshots()
        if len(snaps) >= 2:
            n = con.execute(f"SELECT count(*) FROM {self.revision_staging} WHERE new_as_of = DATE '{snaps[-1]}'"
                            ).fetchone()[0]
            note += f"; {n} revision(s) in the snapshot of {snaps[-1]} against {snaps[-2]}"
        elif snaps:
            note += "; the first snapshot: no revisions yet"
        return note


def _links() -> str:
    return "(SELECT DISTINCT gvkey, cik FROM fundamental_company WHERE gvkey IS NOT NULL)"


def agreement(con: duckdb.DuckDBPyConnection, staging: str, item: str, field: str, period_type: str,
              eps_tolerance: float | None) -> tuple[int, int]:
    """``(compared, agreeing)``: the periods where Compustat's ``item`` and the SEC's ``field`` both have a value
    (period ends within ``PERIOD_DAYS``). Money in millions at Compustat; EPS as is."""
    if eps_tolerance is None:
        ok = (f"abs(f.value - w.v * {MILLION}) <= {MONEY_TOLERANCE} * "
              f"greatest(abs(f.value), abs(w.v * {MILLION}))")
    else:
        ok = f"abs(f.value - w.v) <= {eps_tolerance}"
    n, agree = con.execute(f"""
        WITH w AS (SELECT c.cik, s.datadate, s."{item}" AS v FROM {staging} s JOIN {_links()} c ON c.gvkey = s.gvkey
                   WHERE s."{item}" IS NOT NULL),
             f AS (SELECT cik, period_end, value FROM fundamental_fact
                   WHERE field = '{field}' AND period_type = '{period_type}' AND NOT preliminary)
        SELECT count(*), count(*) FILTER (WHERE {ok})
        FROM w JOIN f ON f.cik = w.cik AND abs(date_diff('day', f.period_end, w.datadate)) <= {PERIOD_DAYS}
        """).fetchone()
    return int(n or 0), int(agree or 0)


class FirmRatio(WrdsFund):
    def note(self, con: duckdb.DuckDBPyConnection) -> str:
        n, ok = con.execute(f"""
            WITH r AS (SELECT c.cik, s.qdate, s.debt_at FROM {self.staging} s JOIN {_links()} c ON c.gvkey = s.gvkey
                       WHERE s.debt_at IS NOT NULL AND s.qdate IS NOT NULL
                       QUALIFY row_number() OVER (PARTITION BY c.cik ORDER BY s.qdate DESC, s.public_date DESC) = 1),
                 q AS (SELECT cik, period_end, value, field FROM fundamental_fact
                       WHERE period_type = '{facts.QUARTERLY}' AND NOT preliminary
                         AND field IN ('total_debt', 'total_assets'))
            SELECT count(*), count(*) FILTER (WHERE abs(d.value / a.value - r.debt_at) <= {DEBT_TOLERANCE})
            FROM r JOIN q d ON d.cik = r.cik AND d.field = 'total_debt'
                                AND abs(date_diff('day', d.period_end, r.qdate)) <= {PERIOD_DAYS}
                   JOIN q a ON a.cik = r.cik AND a.field = 'total_assets' AND a.period_end = d.period_end
                                AND a.value <> 0""").fetchone()
        return (f"debt to assets within {DEBT_TOLERANCE} of WRDS on {int(ok or 0)} of {int(n or 0)} companies "
                "(each one's newest WRDS quarter)")


class WrdsSegments(WrdsFund):
    """``wrds/segments``: Compustat's segment sales (``wrds_segmerged``: business, operating and geographic
    segments) and major customers (``seg_customer``), ``SEGMENT_YEARS`` of fiscal years; per segment and year the
    newest source filing's row (``srcdate``). Served as rows of ``segment_revenue`` and ``customer_share``
    (``source`` ``wrds/segments``) beside the annual report's own (``sec/filings``). The library name
    (``SEGMENT_LIBRARY``) is not verified against WRDS."""

    PARTS = {
        "wrds_segmerged": ([("gvkey", "TEXT"), ("datadate", "DATE"), ("srcdate", "DATE"), ("stype", "TEXT"),
                            ("sid", "TEXT"), ("snms", "TEXT"), ("sales", "DOUBLE")],
                           ["gvkey", "datadate", "stype", "sid"],
                           "stype IN (" + ", ".join(f"'{t}'" for t in sorted(SEGMENT_TYPES)) + ")"),
        "seg_customer": ([("gvkey", "TEXT"), ("datadate", "DATE"), ("srcdate", "DATE"), ("cnms", "TEXT"),
                          ("ctype", "TEXT"), ("salecs", "DOUBLE"), ("stype", "TEXT"), ("sid", "TEXT")],
                         ["gvkey", "datadate", "cnms", "stype", "sid"], None),
    }
    STAGING = {"wrds_segmerged": "stg_wrds_segments", "seg_customer": "stg_wrds_customers"}

    def __init__(self):
        super().__init__("wrds_segmerged", SEGMENT_LIBRARY, [], [], [], "datadate", SEGMENT_YEARS, revisions=False)
        self.name = "wrds/segments"
        self.staging = self.STAGING["wrds_segmerged"]

    def raw_path(self, as_of: date, part: str = "wrds_segmerged") -> Path:
        return common.raw_dir(self.name) / as_of.isoformat() / f"{part}{wrds_session.RAW_SUFFIX}"

    def snapshot_file(self, as_of: str, part: str = "wrds_segmerged") -> Path:
        return common.parquet_dir(self.name) / f"snapshot={as_of}" / f"{part}.parquet"

    def snapshots(self) -> list[str]:
        folder = common.parquet_dir(self.name)
        if not folder.is_dir():
            return []
        return sorted(d.name.split("=", 1)[1] for d in folder.iterdir()
                      if d.name.startswith("snapshot=") and all((d / f"{p}.parquet").is_file() for p in self.PARTS))

    def _full(self, ctx: base.RunContext, as_of: date) -> list[Path]:
        gvkeys = sorted({str(r["gvkey"]) for r in common.universe() if _GVKEY.match(str(r.get("gvkey") or ""))})
        if not gvkeys:
            ctx.notes.append("no Tier A company has a Compustat link yet (wrds/sec_idhist): nothing asked")
            return []
        out = []
        for part, (schema, _, extra) in self.PARTS.items():
            where = [f"gvkey IN ({', '.join(repr(g) for g in gvkeys)})",
                     f"datadate >= DATE '{_years_before(as_of, self.years).isoformat()}'"] + ([extra] if extra else [])
            sql = (f"SELECT {', '.join(c for c, _ in schema)} FROM {self.library}.{part} "
                   f"WHERE {' AND '.join(where)}")
            out.append(wrds_session._ask(ctx, self.raw_path(as_of, part), sql, as_of))
        return out

    def to_parquet_all(self, ctx: base.RunContext, raws: list[Path]) -> list[Path]:
        out = []
        for raw in raws:
            part = raw.name[: -len(wrds_session.RAW_SUFFIX)]
            schema, key, _ = self.PARTS[part]
            meta = base.read_raw_json(common.meta_path(raw))
            as_of = meta["as_of"]
            con = duckdb.connect()
            try:
                keep = (" WHERE stype IN (" + ", ".join(f"'{t}'" for t in sorted(SEGMENT_TYPES)) + ")"
                        if part == "wrds_segmerged" else "")
                rows = f"SELECT * FROM ({wrds_session.raw_rows(raw, schema)}){keep}"
                if part == "wrds_segmerged" and not con.execute(f"SELECT count(*) FROM ({rows})").fetchone()[0]:
                    raise base.SourceError("wrds segments: the snapshot came back empty")
                for col, n in wrds_session.untyped(con, raw, schema).items():
                    ctx.issue("value_untyped", f"{part}.{col}", f"{n} row(s) nulled")
                every = ", ".join(f'"{c}" NULLS LAST' for c, _ in schema)
                select = (f"SELECT *, DATE '{as_of}' AS source_as_of, CAST({float(meta['fetched_at'])} AS DOUBLE) "
                          f"AS fetched_at, '{meta['run_id']}' AS run_id FROM ({rows}) "
                          f"QUALIFY row_number() OVER (PARTITION BY {', '.join(key)} "
                          f"ORDER BY srcdate DESC NULLS LAST, {every}) = 1 ORDER BY {', '.join(key)}")
                out.append(base.write_parquet(con, select, self.snapshot_file(as_of, part)))
            finally:
                con.close()
            ctx.pending.setdefault(self.name, []).append(as_of)
        return out

    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        snaps = self.snapshots()
        for part, (schema, _, _) in self.PARTS.items():
            common.stage_glob(con, self.STAGING[part], schema + PROVENANCE, common.parquet_dir(self.name),
                              f"snapshot={snaps[-1]}/{part}.parquet" if snaps else "snapshot=none/none")
        return {}

    def check(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict:
        cols = ["cik", "axis", "member", "period_end", "value"]
        where = f"source = '{self.name}'"
        added, removed = common.compare_live(con, f"SELECT {', '.join(cols)} FROM segment_revenue WHERE {where}",
                                             "segment_revenue", cols, where=where)
        return {**super().check(ctx, con), "added": added, "removed": removed,
                "result": "changed" if (ctx.pending.get(self.name) or added or removed) else "unchanged"}

    def note(self, con: duckdb.DuckDBPyConnection) -> str:
        tier_a = con.execute("SELECT count(DISTINCT cik) FROM fundamental_company").fetchone()[0]
        companies, rows = con.execute(f"SELECT count(DISTINCT cik), count(*) FROM segment_revenue "
                                      f"WHERE source = '{self.name}'").fetchone()
        customers, shared = con.execute(f"SELECT count(*), count(share) FROM customer_share "
                                        f"WHERE source = '{self.name}'").fetchone()
        return (f"{companies} of {tier_a} Tier A companies with segments ({rows} segment-years); {customers} "
                f"customer rows, {shared} with a share of the SEC's revenue")


FUNDQ = WrdsFund("fundq", "comp",
                 [("gvkey", "TEXT"), ("datadate", "DATE"), ("fyearq", "INTEGER"), ("fqtr", "INTEGER"),
                  ("fyr", "INTEGER"), ("rdq", "DATE")],
                 ["revtq", "cogsq", "oiadpq", "niq", "epspxq", "epsfxq", "atq", "dlttq", "dlcq", "cheq", "ceqq"],
                 ["gvkey", "datadate"], "datadate", YEARS, revisions=True)
FUNDA = WrdsFund("funda", "comp",
                 [("gvkey", "TEXT"), ("datadate", "DATE"), ("fyear", "INTEGER"), ("fyr", "INTEGER")],
                 ["revt", "cogs", "oiadp", "ni", "epspx", "epsfx", "at", "dltt", "dlc", "che", "ceq", "oancf", "capx"],
                 ["gvkey", "datadate"], "datadate", YEARS, revisions=True)
FIRM_RATIO = FirmRatio("firm_ratio", "wrds_finratio",
                       [("gvkey", "TEXT"), ("public_date", "DATE"), ("qdate", "DATE")], ["debt_at"],
                       ["gvkey", "public_date"], "public_date", RATIO_YEARS, revisions=False)
SEGMENTS = WrdsSegments()
DATASETS = (FUNDQ, FUNDA, FIRM_RATIO, SEGMENTS)
