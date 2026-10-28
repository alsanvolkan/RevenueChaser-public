"""``sec/fsds``: the SEC's quarterly Financial Statement Data Sets, a check of the served facts (monthly).

``GET <RC_SEC_WWW_BASE>/files/dera/data/financial-statement-data-sets/<YYYY>q<N>.zip``: every XBRL statement
filed in the quarter, as tab-separated ``sub.txt`` (one row per filing: ``adsh``, ``cik``, ``form``, ``filed``)
and ``num.txt`` (one row per value: ``adsh``, ``tag``, ``ddate``, ``qtrs``, ``uom``, ``value``, and ``coreg`` /
``segments`` for a co-registrant's or a dimension's value). Read by header name. A run asks the newest quarter
that has ended, then the one before (the SEC publishes a quarter some days after it ends: a 404 is "not yet"),
and stops at the first quarter it holds. One zip of tens of MB a quarter, counted against the ``sec`` cap.

What is kept: the values of the Tier A companies' filings, the company's own (no co-registrant, no dimension), of
the tags in ``TAGS``. The check: each kept value against the served fact of the same company, field, period end and
period type (``qtrs`` 1 a quarter, 4 a year, 0 a balance-sheet instant), within 0.5% (EPS within a cent). The served
value may come from a later filing that restated the period, so a difference is a note to look at, not an error.

Raw: ``raw/sec/fsds/<YYYY>q<N>.zip`` (as served). Parquet: ``parquet/sec/fsds/quarter=<YYYY>q<N>/part-0.parquet``.
Staging: ``stg_sec_fsds``, every quarter held.
"""
from __future__ import annotations

import io
import tempfile
import time
import zipfile
from datetime import date
from pathlib import Path

import duckdb

from ....core import calendar
from .. import base
from . import KIND, common, facts

NAME = "sec/fsds"
STAGING = "stg_sec_fsds"
# tag -> served field
TAGS = {"Revenues": "revenue", "RevenueFromContractWithCustomerExcludingAssessedTax": "revenue",
        "NetIncomeLoss": "net_income", "Assets": "total_assets", "EarningsPerShareDiluted": "eps_diluted"}
LABELS = {"revenue": "revenue", "net_income": "net income", "total_assets": "total assets",
          "eps_diluted": "diluted EPS"}
TOLERANCE = 0.005
EPS_TOLERANCE = 0.01
SCHEMA = [("quarter", "TEXT"), ("cik", "TEXT"), ("adsh", "TEXT"), ("form", "TEXT"), ("filed", "DATE"),
          ("tag", "TEXT"), ("field", "TEXT"), ("ddate", "DATE"), ("qtrs", "INTEGER"), ("uom", "TEXT"),
          ("value", "DOUBLE"), ("source_as_of", "DATE"), ("fetched_at", "DOUBLE"), ("run_id", "TEXT")]


def ended_quarters(as_of: date, n: int = 2) -> list[str]:
    """The ``n`` newest quarters that ended before ``as_of``, newest first."""
    year, q = as_of.year, (as_of.month - 1) // 3 + 1
    out = []
    for _ in range(n):
        q -= 1
        if q == 0:
            year, q = year - 1, 4
        out.append(f"{year}q{q}")
    return out


def url(quarter: str) -> str:
    return f"{common.sec_www()}/files/dera/data/financial-statement-data-sets/{quarter}.zip"


def raw_path(quarter: str) -> Path:
    return common.raw_dir(NAME) / f"{quarter}.zip"


class Fsds:
    name = NAME
    kind = KIND
    providers = (common.SEC,)
    tables: tuple[str, ...] = ()
    cadence = "monthly"
    backfill_needs_from = False
    starts_by_hand = True

    def default_as_of(self) -> date:
        return calendar.today_ny()

    def fetch(self, ctx: base.RunContext, as_of: date) -> list[Path]:
        for quarter in ended_quarters(as_of):
            raw = raw_path(quarter)
            if raw.exists():
                ctx.notes.append(f"{quarter} held: nothing newer published")
                return []
            r = common.sec_get(ctx, url(quarter), missing=(404,))
            if r is None:
                continue
            if not zipfile.is_zipfile(io.BytesIO(r.content)):
                raise base.SourceError(f"sec fsds {quarter}: the answer is not a zip")
            base.write_raw_json(common.meta_path(raw), {"quarter": quarter, "as_of": as_of.isoformat(),
                                                        "fetched_at": time.time(), "run_id": ctx.run_id,
                                                        "url": url(quarter)})
            ctx.pending.setdefault(NAME, []).append(quarter)
            return [base.write_raw(raw, r.content)]
        ctx.notes.append(f"none of {', '.join(ended_quarters(as_of))} published yet")
        return []

    def backfill(self, ctx: base.RunContext, start: date | None, end: date) -> None:
        # A check of the newest quarter: a backfill is the monthly rule at its end day.
        self.to_parquet_all(ctx, self.fetch(ctx, end))

    def to_parquet(self, ctx: base.RunContext, raw: Path) -> list[Path]:
        return self.to_parquet_all(ctx, [raw])

    def to_parquet_all(self, ctx: base.RunContext, raws: list[Path]) -> list[Path]:
        ciks = common.ciks(common.universe())
        out = []
        for raw in raws:
            meta = base.read_raw_json(common.meta_path(raw))
            quarter = meta["quarter"]
            with tempfile.TemporaryDirectory(dir=raw.parent) as tmp, zipfile.ZipFile(raw) as z:
                names = {Path(n).name: n for n in z.namelist()}
                missing = [n for n in ("sub.txt", "num.txt") if n not in names]
                if missing:
                    raise base.SourceError(f"sec fsds {quarter}: no {', '.join(missing)} in the zip")
                sub, num = (Path(z.extract(names[n], tmp)) for n in ("sub.txt", "num.txt"))
                con = duckdb.connect()
                try:
                    out.append(base.write_parquet(con, self._select(con, sub, num, quarter, ciks, meta),
                                                  common.parquet_dir(NAME) / f"quarter={quarter}" /
                                                  "part-0.parquet"))
                finally:
                    con.close()
        return out

    @staticmethod
    def _select(con, sub: Path, num: Path, quarter: str, ciks: list[str], meta: dict) -> str:
        def table(path: Path) -> str:
            return (f"read_csv({base.sql_path(path)}, delim = '\\t', header = true, all_varchar = true, "
                    "quote = '', escape = '')")
        cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {table(num)}").fetchall()}
        missing = {"adsh", "tag", "ddate", "qtrs", "uom", "value"} - cols
        if missing:
            raise base.SourceError(f"sec fsds {quarter}: num.txt has no {', '.join(sorted(missing))}")
        own = "".join(f" AND coalesce(n.{c}, '') = ''" for c in ("coreg", "segments") if c in cols)
        tags = ", ".join(f"'{t}'" for t in TAGS)
        field = "CASE n.tag " + " ".join(f"WHEN '{t}' THEN '{f}'" for t, f in TAGS.items()) + " END"
        wanted = ", ".join(f"'{c}'" for c in ciks) or "NULL"
        return f"""
            SELECT '{quarter}' AS quarter, CAST(TRY_CAST(s.cik AS BIGINT) AS VARCHAR) AS cik, n.adsh, s.form,
                   TRY_STRPTIME(s.filed, '%Y%m%d')::DATE AS filed, n.tag, {field} AS field,
                   TRY_STRPTIME(n.ddate, '%Y%m%d')::DATE AS ddate, TRY_CAST(n.qtrs AS INTEGER) AS qtrs, n.uom,
                   TRY_CAST(n.value AS DOUBLE) AS value, DATE '{meta['as_of']}' AS source_as_of,
                   CAST({float(meta['fetched_at'])} AS DOUBLE) AS fetched_at, '{meta['run_id']}' AS run_id
            FROM {table(num)} n JOIN {table(sub)} s ON s.adsh = n.adsh
            WHERE n.tag IN ({tags}) AND CAST(TRY_CAST(s.cik AS BIGINT) AS VARCHAR) IN ({wanted})
                  {own} AND TRY_CAST(n.value AS DOUBLE) IS NOT NULL
            ORDER BY cik, adsh, tag, ddate"""

    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        common.stage_glob(con, STAGING, SCHEMA, common.parquet_dir(NAME), "quarter=*/part-0.parquet")
        return {}

    def check(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict:
        quarter = con.execute(f"SELECT max(quarter) FROM {STAGING}").fetchone()[0]
        on = ctx.as_of or self.default_as_of()
        if quarter is None:
            return {"dataset": NAME, "result": "unchanged", "source_as_of": on.isoformat(), "rows": 0,
                    "added": 0, "removed": 0}
        rows = con.execute(f"""
            WITH f AS (SELECT * FROM {STAGING} WHERE quarter = ?
                       QUALIFY row_number() OVER (PARTITION BY cik, field, ddate, qtrs
                                                  ORDER BY tag = 'Revenues' DESC, filed DESC) = 1)
            SELECT f.field, count(*),
                   count(*) FILTER (WHERE CASE WHEN f.field = 'eps_diluted'
                                               THEN abs(s.value - f.value) <= {EPS_TOLERANCE}
                                               ELSE abs(s.value - f.value)
                                                    <= {TOLERANCE} * greatest(abs(s.value), abs(f.value)) END)
            FROM f JOIN fundamental_fact s
              ON s.cik = f.cik AND s.field = f.field AND s.period_end = f.ddate AND NOT s.preliminary
             AND (f.qtrs = 0 OR s.period_type = CASE f.qtrs WHEN 1 THEN '{facts.QUARTERLY}'
                                                            WHEN 4 THEN '{facts.ANNUAL}' END)
            GROUP BY f.field""", [quarter]).fetchall()
        by = {field: (int(n), int(ok)) for field, n, ok in rows}
        total = sum(n for n, _ in by.values())
        agree = sum(ok for _, ok in by.values())
        parts = ", ".join(f"{LABELS[f]} {by[f][1]} of {by[f][0]}" for f in LABELS if f in by)
        ctx.notes.append(f"FSDS {quarter}: {agree} of {total} values agree with the served facts"
                         + (f" ({parts})" if parts else ""))
        new = quarter in ctx.pending.get(NAME, [])
        return {"dataset": NAME, "result": "changed" if new else "unchanged", "source_as_of": on.isoformat(),
                "rows": common.count(con, STAGING), "added": 0, "removed": 0,
                "coverage": round(agree / total, 4) if total else None}


DATASET = Fsds()
