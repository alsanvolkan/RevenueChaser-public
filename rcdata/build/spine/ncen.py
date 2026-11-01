"""``sec/ncen``: the SEC's Form N-CEN data sets, the ETF universe's second half (#346), monthly.

``GET <RC_SEC_WWW_BASE>/files/dera/data/form-n-cen-data-sets/<YYYY>q<N>_ncen.zip`` (listed on
``/data-research/sec-markets-data/form-n-cen-data-sets``; 2026q3 about 5.9 MB, 2026q1 about 16 MB): every N-CEN
filed in the quarter, as tab-separated tables. Two are read, joined on ``FUND_ID``: ``FUND_REPORTED_INFO.tsv`` (one
row per series and filing: ``SERIES_ID``, ``FUND_NAME``, the fund-type flags ``IS_ETF``, ``IS_INDEX``,
``IS_MULTI_INVERSE_INDEX``, ``IS_MONEY_MARKET``, ``IS_FUND_OF_FUND`` written ``Y`` or blank, and the average net
assets ``DAILY_AVG_NET_ASSETS`` / ``MONTHLY_AVG_NET_ASSETS``) and ``SECURITY_EXCHANGE.tsv`` (the ticker and exchange,
filled for the ETFs). ``SUBMISSION.tsv`` gives each filing's CIK and dates.

**Cadence: monthly, the ``sec/fsds`` rule.** ``due.py`` knows daily, weekly and monthly, not quarterly. A run asks the
newest quarter that has ended, then the one before (a 404 is "not published yet"), and stops at the first quarter it
holds: a published quarter's file is not re-issued, so a held one is never asked again and a month is at most a
month late for a new quarter. If-Modified-Since is not needed for that, and the raw file stays the resume marker. A
backfill asks the last four quarters (an N-CEN is filed once a fiscal year, so four quarters hold every fund's
newest one; measured 1 Nov 2026 over 2025q4-2026q3: 12,422 series, 4,248 ``IS_ETF=Y``, every one with a ticker).

**Newest filing per series wins**: the build stages every held quarter and keeps each series' rows of its newest
filing (``filing_date``, then the accession). Every filer's CIK is kept as well (``filer.parquet``), a unit
investment trust's included (SPY and DIA file as S-6 registrants with no series row): ``etf`` reads it to tell a
trust that files no N-CEN at all (a commodity or crypto trust) from one that does.

Raw: ``raw/sec/ncen/<YYYY>q<N>.zip`` (as served) and its ``.meta.json``. Parquet:
``parquet/sec/ncen/quarter=<YYYY>q<N>/part-0.parquet`` (the series) and ``filer.parquet``. Staging: ``stg_ncen``
and ``stg_ncen_filer``; ``spine/security`` reads them to build ``etf``.
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
from ..fundamentals.fsds import ended_quarters
from .. import base
from . import KIND, stage

NAME = "sec/ncen"
PROVIDER = "sec"
STAGING = "stg_ncen"
FILERS = "stg_ncen_filer"
FILES = ("FUND_REPORTED_INFO.tsv", "SECURITY_EXCHANGE.tsv", "SUBMISSION.tsv")
BACKFILL_QUARTERS = 4
SCHEMA = [("quarter", "TEXT"), ("accession", "TEXT"), ("cik", "TEXT"), ("filing_date", "DATE"),
          ("report_end", "DATE"), ("series_id", "TEXT"), ("fund_name", "TEXT"), ("ticker", "TEXT"),
          ("exchange", "TEXT"), ("is_etf", "BOOLEAN"), ("is_index", "BOOLEAN"), ("is_multi_inverse_index", "BOOLEAN"),
          ("is_money_market", "BOOLEAN"), ("is_fund_of_fund", "BOOLEAN"), ("daily_avg_net_assets", "DOUBLE"),
          ("monthly_avg_net_assets", "DOUBLE"), ("source_as_of", "DATE"), ("fetched_at", "DOUBLE"),
          ("run_id", "TEXT")]
FILER = [("quarter", "TEXT"), ("accession", "TEXT"), ("cik", "TEXT"), ("filing_date", "DATE")]
META_SUFFIX = ".meta.json"


def url(quarter: str) -> str:
    from ....core.providers import sec_edgar
    return f"{sec_edgar.SEC_WWW_BASE.rstrip('/')}/files/dera/data/form-n-cen-data-sets/{quarter}_ncen.zip"


def raw_dir() -> Path:
    return base.raw_root() / "sec" / "ncen"


def parquet_dir() -> Path:
    return base.parquet_root() / "sec" / "ncen"


def raw_path(quarter: str) -> Path:
    return raw_dir() / f"{quarter}.zip"


def meta_path(raw: Path) -> Path:
    return raw.with_name(raw.name + META_SUFFIX)


def _refuse(quarter: str, content: bytes) -> None:
    """``SourceError`` unless ``content`` is a zip holding the three tables read, before it is kept."""
    if not zipfile.is_zipfile(io.BytesIO(content)):
        raise base.SourceError(f"sec ncen {quarter}: the answer is not a zip")
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        missing = [n for n in FILES if n not in {Path(m).name for m in z.namelist()}]
    if missing:
        raise base.SourceError(f"sec ncen {quarter}: no {', '.join(missing)} in the zip")


class Ncen:
    name = NAME
    kind = KIND
    providers = (PROVIDER,)
    tables: tuple[str, ...] = ()
    cadence = "monthly"
    backfill_needs_from = False
    starts_by_hand = True             # as every spine dataset: not due until a CLI run or a Fetch built it

    def default_as_of(self) -> date:
        return calendar.today_ny()

    def _get(self, ctx: base.RunContext, quarter: str, as_of: date) -> Path | None:
        """Fetch ``quarter`` and keep it; None when the SEC has not published it (404)."""
        r = base.http_get(ctx, PROVIDER, url(quarter), accept=(404,))
        if r.status_code == 404:
            return None
        _refuse(quarter, r.content)
        raw = raw_path(quarter)
        base.write_raw_json(meta_path(raw), {"quarter": quarter, "as_of": as_of.isoformat(),
                                             "fetched_at": time.time(), "run_id": ctx.run_id, "url": url(quarter)})
        ctx.pending.setdefault(NAME, []).append(quarter)
        return base.write_raw(raw, r.content)

    def fetch(self, ctx: base.RunContext, as_of: date) -> list[Path]:
        for quarter in ended_quarters(as_of):
            if raw_path(quarter).exists():
                ctx.notes.append(f"{quarter} held: nothing newer published")
                return []
            raw = self._get(ctx, quarter, as_of)
            if raw is not None:
                return [raw]
        ctx.notes.append(f"none of {', '.join(ended_quarters(as_of))} published yet")
        return []

    def backfill(self, ctx: base.RunContext, start: date | None, end: date) -> None:
        raws = []
        for quarter in ended_quarters(end, BACKFILL_QUARTERS):
            raw = raw_path(quarter) if raw_path(quarter).exists() else self._get(ctx, quarter, end)
            if raw is not None:
                raws.append(raw)
        self.to_parquet_all(ctx, raws)

    def to_parquet(self, ctx: base.RunContext, raw: Path) -> list[Path]:
        return self.to_parquet_all(ctx, [raw])

    def to_parquet_all(self, ctx: base.RunContext, raws: list[Path]) -> list[Path]:
        out = []
        for raw in raws:
            meta = base.read_raw_json(meta_path(raw))
            quarter = meta["quarter"]
            folder = parquet_dir() / f"quarter={quarter}"
            with tempfile.TemporaryDirectory(dir=raw.parent) as tmp, zipfile.ZipFile(raw) as z:
                names = {Path(n).name: n for n in z.namelist()}
                fund, exch, sub = (Path(z.extract(names[n], tmp)) for n in FILES)
                con = duckdb.connect()
                try:
                    out.append(base.write_parquet(con, self._series(fund, exch, sub, quarter, meta),
                                                  folder / "part-0.parquet"))
                    out.append(base.write_parquet(con, self._filers(sub, quarter), folder / "filer.parquet"))
                finally:
                    con.close()
        return out

    @staticmethod
    def _table(path: Path) -> str:
        # As sec/fsds: a field holding a tab or a line break comes in double quotes, an inner quote doubled.
        return (f"read_csv({base.sql_path(path)}, delim = '\\t', header = true, all_varchar = true, "
                "quote = '\"', escape = '\"')")

    @classmethod
    def _filers(cls, sub: Path, quarter: str) -> str:
        return f"""
            SELECT '{quarter}' AS quarter, ACCESSION_NUMBER AS accession,
                   CAST(TRY_CAST(CIK AS BIGINT) AS VARCHAR) AS cik,
                   TRY_STRPTIME(FILING_DATE, '%d-%b-%Y')::DATE AS filing_date
            FROM {cls._table(sub)} WHERE TRY_CAST(CIK AS BIGINT) IS NOT NULL ORDER BY cik, accession"""

    @classmethod
    def _series(cls, fund: Path, exch: Path, sub: Path, quarter: str, meta: dict) -> str:
        def flag(col: str) -> str:
            return f"coalesce(trim(f.{col}) = 'Y', false) AS {col.lower()}"
        return f"""
            SELECT '{quarter}' AS quarter, f.ACCESSION_NUMBER AS accession,
                   CAST(TRY_CAST(s.CIK AS BIGINT) AS VARCHAR) AS cik,
                   TRY_STRPTIME(s.FILING_DATE, '%d-%b-%Y')::DATE AS filing_date,
                   TRY_STRPTIME(s.REPORT_ENDING_PERIOD, '%d-%b-%Y')::DATE AS report_end,
                   trim(f.SERIES_ID) AS series_id, trim(f.FUND_NAME) AS fund_name,
                   nullif(replace(upper(trim(e.FUND_TICKER_SYMBOL)), '.', '-'), '') AS ticker, nullif(trim(e.FUND_EXCHANGE), '') AS exchange,
                   {flag('IS_ETF')}, {flag('IS_INDEX')}, {flag('IS_MULTI_INVERSE_INDEX')}, {flag('IS_MONEY_MARKET')},
                   {flag('IS_FUND_OF_FUND')},
                   TRY_CAST(f.DAILY_AVG_NET_ASSETS AS DOUBLE) AS daily_avg_net_assets,
                   TRY_CAST(f.MONTHLY_AVG_NET_ASSETS AS DOUBLE) AS monthly_avg_net_assets,
                   DATE '{meta['as_of']}' AS source_as_of, CAST({float(meta['fetched_at'])} AS DOUBLE) AS fetched_at,
                   '{meta['run_id']}' AS run_id
            FROM {cls._table(fund)} f
            LEFT JOIN {cls._table(exch)} e ON e.FUND_ID = f.FUND_ID
            LEFT JOIN {cls._table(sub)} s ON s.ACCESSION_NUMBER = f.ACCESSION_NUMBER
            WHERE coalesce(trim(f.SERIES_ID), '') <> ''
            ORDER BY series_id, ticker"""

    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        series = [f for _, f in stage.partitions(parquet_dir(), "quarter")]
        filers = [f for _, f in stage.partitions(parquet_dir(), "quarter", "filer.parquet")]
        # Each series' rows of its newest filing (one row per ticker it lists, none when it lists none).
        stage.create(con, STAGING, SCHEMA, series,
                     select="SELECT * FROM {src} QUALIFY accession = first_value(accession) OVER "
                            "(PARTITION BY series_id ORDER BY filing_date DESC NULLS LAST, accession DESC)")
        stage.create(con, FILERS, FILER, filers)
        return {}

    def check(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict:
        n_series, n_etf, newest = con.execute(
            f"SELECT count(DISTINCT series_id), count(DISTINCT series_id) FILTER (WHERE is_etf), max(quarter) "
            f"FROM {STAGING}").fetchone()
        on = ctx.as_of or self.default_as_of()
        ctx.notes.append(f"{n_series} series, {n_etf} ETF series; newest quarter {newest or 'none'}")
        return {"dataset": NAME, "result": "changed" if ctx.pending.get(NAME) else "unchanged",
                "source_as_of": on.isoformat(), "rows": stage.count(con, STAGING), "added": 0, "removed": 0}


DATASET = Ncen()
