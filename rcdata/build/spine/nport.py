"""``sec/nport``: the SEC's Form N-PORT data sets, every ETF's holdings once a month (#354, ETF PR 3a), monthly.

``GET <RC_SEC_WWW_BASE>/files/dera/data/form-n-port-data-sets/<YYYY>q<N>_nport.zip`` (listed on
``/data-research/sec-markets-data/form-n-port-data-sets``, 2019q4 onwards; 2026q3 is 441 MB, 5.6 million holdings,
measured 1 Nov 2026): the N-PORTs disseminated in the quarter, as tab-separated tables named by the zip's own
``nport_metadata.json``. Five are read: ``SUBMISSION`` (``REPORT_DATE``, the month the holdings are as of, and
``FILING_DATE``), ``REGISTRANT`` (``CIK``), ``FUND_REPORTED_INFO`` (``SERIES_ID``, ``SERIES_NAME``, ``NET_ASSETS``),
``FUND_REPORTED_HOLDING`` (one row per holding: ``ISSUER_CUSIP``, ``BALANCE`` and ``UNIT``, ``CURRENCY_VALUE``, the
value in US dollars whatever the currency, ``PERCENTAGE`` of net assets, ``PAYOFF_PROFILE``, ``ASSET_CAT``,
``ISSUER_TYPE``) and ``IDENTIFIERS`` (the holding's ISIN and ticker, when given). A quarter holds one public report per
series, its fiscal quarter's last month (2026q3: 7,063 June, 4,195 July, 2,516 May reports), and the amendments
(``NPORT-P/A``, 128): the newest filing of a series and month wins.

**Only ETF series are kept** (a quarter holds every registered fund): a ``SERIES_ID`` that N-CEN calls an ETF
(``sec/ncen``'s Parquet, every held quarter), and a series whose name is, normalized, the one name of an active
Alpha Vantage ETF (``alphavantage/listing_status``'s newest list) that no other series or ETF shares: N-CEN's data
sets miss some registrants (First Trust's SKYY and AIRR, Abacus, American Century's Avantis), N-PORT has them. On
2026q3: 4,114 of N-CEN's 4,248 ETF series, and 701 series by the name (707 tickers, no conflict with an LEI
cross-check), so 1,385,241 holdings of 4,815 series (25% of the quarter), 56 MB of Parquet. With neither list held
the quarter is not typed (it would keep nothing): the run fails, and the raw zip is typed on the next run.

**Fetch: the newest published quarter.** From the newest quarter that has ended, a 404 is "not published yet" and
the walk goes one quarter back; a held quarter is never asked again (a published one is not re-issued), and one held
raw but never typed is typed. **Backfill** asks every quarter from ``--from`` to its end day the same way; history is
avo's to ask for (the default Fetch holds the newest quarter only). Each zip is one paced SEC request (``sec.Pacer``,
the ``sec`` day cap), about 440 MB: shortcut: the answer is held in memory whole, as ``sec/ncen``'s and the 13F sets'
are; stream it to disk if a quarter outgrows the builder's memory.

Raw: ``raw/sec/nport/<YYYY>q<N>.zip`` and its ``.meta.json``. Parquet: ``parquet/sec/nport/quarter=<YYYY>q<N>/``
``part-0.parquet`` (the holdings) and ``fund.parquet`` (one row per series and month: net assets, the equity and
bond shares, the Alpha Vantage ticker its name linked). Staging: ``stg_nport_fund`` (each series' newest month) and
``stg_nport`` (that month's holdings); ``spine/security`` reads them for ``etf`` and serves ``etf_holdings_nport``.
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
from ... import serving
from ..fundamentals.fsds import ended_quarters
from ..ownership import sec as sec_http
from .. import base
from . import KIND, av_listing, ncen, stage

NAME = "sec/nport"
PROVIDER = "sec"
STAGING = "stg_nport"
FUNDS = "stg_nport_fund"
TABLE = "etf_holdings_nport"
FIRST_QUARTER = "2019q4"          # the first data set the SEC lists
FILES = ("SUBMISSION.tsv", "REGISTRANT.tsv", "FUND_REPORTED_INFO.tsv", "FUND_REPORTED_HOLDING.tsv", "IDENTIFIERS.tsv")
META_SUFFIX = ".meta.json"

#: Form N-PORT Item C.4.a asset categories, as the data set writes them. Equity: ``EC`` common, ``EP`` preferred.
#: Debt: ``DBT`` debt, ``LON`` loan, ``SN`` structured note, ``ABS-MBS`` / ``ABS-APCP`` / ``ABS-CBDO`` / ``ABS-O``
#: asset-backed. Not counted in either: cash and its kin (``STIV`` short-term vehicle, ``RA`` repurchase agreement),
#: derivatives (``DE`` equity, ``DIR`` rate, ``DCR`` credit, ``DFE`` FX, ``DCO`` commodity, ``DO`` other), ``COMM``
#: commodity, ``RE`` real estate, ``OTHER``.
EQUITY_CATS = ("EC", "EP")
BOND_CATS = ("DBT", "LON", "SN", "ABS-MBS", "ABS-APCP", "ABS-CBDO", "ABS-O")
#: SEC rule 35d-1(a)(2): a fund named for a kind of investment holds "at least 80% of the value of its assets" in it.
SHARE = 80.0

HOLDING = [("quarter", "TEXT"), ("accession", "TEXT"), ("filing_date", "DATE"), ("report_date", "DATE"),
           ("series_id", "TEXT"), ("name", "TEXT"), ("title", "TEXT"), ("cusip", "TEXT"), ("isin", "TEXT"),
           ("ticker", "TEXT"), ("balance", "DOUBLE"), ("unit", "TEXT"), ("value_usd", "DOUBLE"), ("pct", "DOUBLE"),
           ("payoff", "TEXT"), ("asset_cat", "TEXT"), ("issuer_type", "TEXT"), ("net_assets", "DOUBLE"),
           ("source_as_of", "DATE"), ("fetched_at", "DOUBLE"), ("run_id", "TEXT")]
FUND = [("quarter", "TEXT"), ("accession", "TEXT"), ("filing_date", "DATE"), ("report_date", "DATE"),
        ("cik", "TEXT"), ("series_id", "TEXT"), ("series_name", "TEXT"), ("net_assets", "DOUBLE"),
        ("equity_pct", "DOUBLE"), ("bond_pct", "DOUBLE"), ("av_ticker", "TEXT"), ("source_as_of", "DATE"),
        ("fetched_at", "DOUBLE"), ("run_id", "TEXT")]


#: ``etf_holdings_nport``, served by ``spine/security`` after ``etf``: each ETF's newest month of holdings, one row
#: per ticker of ``etf`` that carries the series (a series listed under two tickers is served under both). ``ticker``
#: is the spine's, by the holding's CUSIP (``security.cusip``, the first ticker of a shared one); ``ticker_filed`` is
#: the filing's own. ``weight`` is N-PORT's percent of the fund's net assets (negative for a short), as
#: ``etf_holdings.weight`` is a percent.
SERVED = [("etf", "TEXT"), ("series_id", "TEXT"), ("report_date", "DATE"), ("accession", "TEXT"),
          ("filing_date", "DATE"), ("name", "TEXT"), ("title", "TEXT"), ("cusip", "TEXT"), ("isin", "TEXT"),
          ("ticker", "TEXT"), ("ticker_filed", "TEXT"), ("balance", "DOUBLE"), ("unit", "TEXT"),
          ("value_usd", "DOUBLE"), ("weight", "DOUBLE"), ("payoff", "TEXT"), ("asset_cat", "TEXT"),
          ("issuer_type", "TEXT"), ("net_assets", "DOUBLE"), ("source_as_of", "DATE"), ("fetched_at", "DOUBLE"),
          ("run_id", "TEXT")]


def serve(con: duckdb.DuckDBPyConnection) -> int:
    """Create :data:`TABLE` from the staging, ``etf`` and ``security`` of this build; its row count.

    The table holds each series' newest month, so a newer quarter whose funds hold fewer lines is no loss: the swap's
    shrink guard is told to compare only the newest month's rows (``serving.declare_window``)."""
    cols = ", ".join(f'"{c}" {t}' for c, t in SERVED)
    con.execute(f"CREATE TABLE {TABLE} ({cols})")
    con.execute(f"""
        INSERT INTO {TABLE}
        SELECT e.ticker, h.series_id, h.report_date, h.accession, h.filing_date, h.name, h.title, h.cusip, h.isin,
               s.ticker, h.ticker, h.balance, h.unit, h.value_usd, h.pct, h.payoff, h.asset_cat, h.issuer_type,
               h.net_assets, h.source_as_of, h.fetched_at, h.run_id
        FROM {STAGING} h JOIN etf e ON e.series_id = h.series_id
        LEFT JOIN (SELECT cusip, min(ticker) AS ticker FROM security WHERE cusip IS NOT NULL GROUP BY 1) s
               ON s.cusip = h.cusip
        ORDER BY e.ticker, h.pct DESC NULLS LAST, h.cusip""")
    newest = con.execute(f"SELECT max(report_date) FROM {TABLE}").fetchone()[0]
    if newest is not None:
        serving.declare_window(con, TABLE, "report_date", newest.isoformat())
    return stage.count(con, TABLE)


def url(quarter: str) -> str:
    return f"{sec_http.www_base()}/files/dera/data/form-n-port-data-sets/{quarter}_nport.zip"


def raw_dir() -> Path:
    return base.raw_root() / "sec" / "nport"


def parquet_dir() -> Path:
    return base.parquet_root() / "sec" / "nport"


def raw_path(quarter: str) -> Path:
    return raw_dir() / f"{quarter}.zip"


def meta_path(raw: Path) -> Path:
    return raw.with_name(raw.name + META_SUFFIX)


def typed(quarter: str) -> bool:
    folder = parquet_dir() / f"quarter={quarter}"
    return all((folder / n).exists() for n in ("part-0.parquet", "fund.parquet"))


def quarter_of(day: date) -> str:
    return f"{day.year}q{(day.month - 1) // 3 + 1}"


def asset_class(equity_pct: float | None, bond_pct: float | None) -> str:
    """``equity`` or ``bond`` at :data:`SHARE` percent of net assets or more, else ``mixed``."""
    if (equity_pct or 0) >= SHARE:
        return "equity"
    if (bond_pct or 0) >= SHARE:
        return "bond"
    return "mixed"


def _refuse(quarter: str, content: bytes) -> None:
    """``SourceError`` unless ``content`` is a zip holding the five tables read, before it is kept."""
    if not zipfile.is_zipfile(io.BytesIO(content)):
        raise base.SourceError(f"sec nport {quarter}: the answer is not a zip")
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        missing = [n for n in FILES if n not in {Path(m).name for m in z.namelist()}]
    if missing:
        raise base.SourceError(f"sec nport {quarter}: no {', '.join(missing)} in the zip")


def _norm(col: str) -> str:
    """A name compared across sources: upper case, every run of other characters one space."""
    return f"trim(regexp_replace(upper({col}), '[^A-Z0-9]+', ' ', 'g'))"


class Nport:
    name = NAME
    kind = KIND
    providers = (PROVIDER,)
    tables: tuple[str, ...] = ()
    cadence = "monthly"
    starts_by_hand = True             # as every spine dataset: not due until a CLI run or a Fetch built it

    def default_as_of(self) -> date:
        return calendar.today_ny()

    def _get(self, ctx: base.RunContext, pacer: sec_http.Pacer, quarter: str, as_of: date) -> Path | None:
        """Fetch ``quarter`` and keep it; None when the SEC has not published it (404)."""
        r = sec_http.get(ctx, pacer, url(quarter), accept=(404,))
        if r.status_code == 404:
            return None
        _refuse(quarter, r.content)
        raw = raw_path(quarter)
        base.write_raw_json(meta_path(raw), {"quarter": quarter, "as_of": as_of.isoformat(),
                                             "fetched_at": time.time(), "run_id": ctx.run_id, "url": url(quarter),
                                             "bytes": len(r.content)})
        ctx.pending.setdefault(NAME, []).append(quarter)
        return base.write_raw(raw, r.content)

    def _hold(self, ctx: base.RunContext, quarters: list[str], as_of: date, newest_only: bool) -> list[Path]:
        """The raw files of ``quarters`` (newest first) to type: a held and typed one is not asked again, one held
        raw but never typed is typed, a 404 is "not published yet". ``newest_only`` stops at the first held or
        published quarter."""
        pacer, out = sec_http.Pacer(ctx), []
        for quarter in quarters:
            raw = raw_path(quarter)
            if raw.exists():
                if not typed(quarter):
                    ctx.pending.setdefault(NAME, []).append(quarter)
                    out.append(raw)
            else:
                raw = self._get(ctx, pacer, quarter, as_of)
                if raw is None:
                    ctx.notes.append(f"{quarter} not published yet")
                    continue
                out.append(raw)
            if newest_only:
                break
        return out

    def fetch(self, ctx: base.RunContext, as_of: date) -> list[Path]:
        return self._hold(ctx, ended_quarters(as_of, 2), as_of, newest_only=True)

    def backfill(self, ctx: base.RunContext, start: date | None, end: date) -> None:
        first = max(quarter_of(start), FIRST_QUARTER) if start else FIRST_QUARTER
        quarters = [q for q in ended_quarters(end, 4 * (end.year - 2019) + 4) if q >= first]
        self.to_parquet_all(ctx, self._hold(ctx, quarters, end, newest_only=False))

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
                paths = {n: Path(z.extract(names[n], tmp)) for n in FILES}
                cfg = serving.build_config()
                con = duckdb.connect(config={**cfg, "temp_directory": str(Path(tmp) / "spill")})
                try:
                    kept = self._funds(con, paths, quarter, meta)
                    ctx.notes.append(f"{quarter}: {kept} ETF series kept")
                    out.append(base.write_parquet(con, self._holdings(paths, quarter, meta),
                                                  folder / "part-0.parquet"))
                    out.append(base.write_parquet(con, f"SELECT {', '.join(c for c, _ in FUND)} FROM fund",
                                                  folder / "fund.parquet"))
                finally:
                    con.close()
        return out

    @staticmethod
    def _table(path: Path) -> str:
        # As sec/ncen: a field holding a tab or a line break comes in double quotes, an inner quote doubled (the
        # 2026q3 holdings file has no quote at all, and every line reads: 5,627,083 rows of 5,627,084 lines).
        return (f"read_csv({base.sql_path(path)}, delim = '\\t', header = true, all_varchar = true, "
                "quote = '\"', escape = '\"')")

    def _funds(self, con: duckdb.DuckDBPyConnection, paths: dict, quarter: str, meta: dict) -> int:
        """Create ``fund`` (the kept series' newest filing per month, with their mix) and ``keep``; the series kept."""
        etf_series = [f for _, f in stage.partitions(ncen.parquet_dir(), "quarter")]
        av_lists = stage.partitions(av_listing.parquet_dir(), "date")
        con.execute("CREATE TEMP TABLE ncen_etf (series_id TEXT)")
        if etf_series:
            con.execute(f"INSERT INTO ncen_etf SELECT DISTINCT series_id FROM read_parquet("
                        f"[{', '.join(base.sql_path(f) for f in etf_series)}]) WHERE is_etf")
        con.execute("CREATE TEMP TABLE av_etf (ticker TEXT, k TEXT)")
        if av_lists:
            con.execute(f"INSERT INTO av_etf SELECT ticker, {_norm('name')} FROM read_parquet("
                        f"{base.sql_path(av_lists[-1][1])}) WHERE asset_type = 'ETF' AND status = 'Active'")
        if not con.execute("SELECT (SELECT count(*) FROM ncen_etf) + (SELECT count(*) FROM av_etf)").fetchone()[0]:
            raise base.SourceError(f"sec nport {quarter}: no ETF list held yet (run sec/ncen and "
                                   "alphavantage/listing_status first); the quarter is typed on the next run")
        sub, reg, info = (self._table(paths[n]) for n in FILES[:3])
        con.execute(f"""
            CREATE TEMP TABLE filing AS
            SELECT s.ACCESSION_NUMBER AS accession, TRY_STRPTIME(s.FILING_DATE, '%d-%b-%Y')::DATE AS filing_date,
                   TRY_STRPTIME(s.REPORT_DATE, '%d-%b-%Y')::DATE AS report_date,
                   CAST(TRY_CAST(r.CIK AS BIGINT) AS VARCHAR) AS cik, trim(f.SERIES_ID) AS series_id,
                   trim(f.SERIES_NAME) AS series_name, TRY_CAST(f.NET_ASSETS AS DOUBLE) AS net_assets
            FROM {sub} s JOIN {info} f USING (ACCESSION_NUMBER) LEFT JOIN {reg} r USING (ACCESSION_NUMBER)
            WHERE coalesce(trim(f.SERIES_ID), '') <> ''
            -- The parsed dates by expression: a bare filing_date would bind to the text column FILING_DATE.
            QUALIFY row_number() OVER (PARTITION BY trim(f.SERIES_ID), TRY_STRPTIME(s.REPORT_DATE, '%d-%b-%Y')
                                       ORDER BY TRY_STRPTIME(s.FILING_DATE, '%d-%b-%Y') DESC NULLS LAST,
                                                s.ACCESSION_NUMBER DESC) = 1""")
        # The name link: one series and one AV ETF to a name, on both sides (a shared name links nothing).
        con.execute(f"""
            CREATE TEMP TABLE keep AS
            WITH s AS (SELECT DISTINCT series_id, {_norm('series_name')} AS k FROM filing),
                 su AS (SELECT k, any_value(series_id) AS series_id FROM s GROUP BY k HAVING count(*) = 1),
                 au AS (SELECT k, any_value(ticker) AS ticker FROM av_etf GROUP BY k HAVING count(*) = 1),
                 linked AS (SELECT su.series_id, au.ticker FROM su JOIN au USING (k))
            SELECT DISTINCT s.series_id, l.ticker AS av_ticker FROM s LEFT JOIN linked l USING (series_id)
            WHERE s.series_id IN (SELECT series_id FROM ncen_etf) OR l.ticker IS NOT NULL""")
        hold = self._table(paths["FUND_REPORTED_HOLDING.tsv"])
        con.execute(f"""
            CREATE TEMP TABLE mix AS
            SELECT h.ACCESSION_NUMBER AS accession,
                   coalesce(sum(TRY_CAST(h.PERCENTAGE AS DOUBLE)) FILTER (WHERE {self._share(EQUITY_CATS)}), 0)
                       AS equity_pct,
                   coalesce(sum(TRY_CAST(h.PERCENTAGE AS DOUBLE)) FILTER (WHERE {self._share(BOND_CATS)}), 0)
                       AS bond_pct
            FROM {hold} h WHERE h.ACCESSION_NUMBER IN (SELECT accession FROM filing f JOIN keep USING (series_id))
            GROUP BY 1""")
        con.execute(f"""
            CREATE TEMP TABLE fund AS
            SELECT '{quarter}' AS quarter, f.*, m.equity_pct, m.bond_pct, k.av_ticker,
                   DATE '{meta['as_of']}' AS source_as_of, CAST({float(meta['fetched_at'])} AS DOUBLE) AS fetched_at,
                   '{meta['run_id']}' AS run_id
            FROM filing f JOIN keep k USING (series_id) LEFT JOIN mix m USING (accession)
            ORDER BY series_id, report_date""")
        return int(con.execute("SELECT count(DISTINCT series_id) FROM fund").fetchone()[0])

    @staticmethod
    def _share(cats: tuple[str, ...]) -> str:
        # A long position in the category, not a registered fund's shares: a fund of funds' ETFs are filed EC, and
        # on 2026q3 312 series would read as equity only by them (a CLO fund of funds, a Treasury futures fund).
        listed = ", ".join(f"'{c}'" for c in cats)
        return (f"h.ASSET_CAT IN ({listed}) AND coalesce(h.PAYOFF_PROFILE, 'Long') = 'Long' "
                "AND coalesce(h.ISSUER_TYPE, '') <> 'RF'")

    def _holdings(self, paths: dict, quarter: str, meta: dict) -> str:
        hold, ident = self._table(paths["FUND_REPORTED_HOLDING.tsv"]), self._table(paths["IDENTIFIERS.tsv"])
        return f"""
            WITH h AS (SELECT h.*, f.* FROM {hold} h JOIN fund f ON f.accession = h.ACCESSION_NUMBER),
                 i AS (SELECT HOLDING_ID, max(nullif(upper(trim(IDENTIFIER_ISIN)), '')) AS isin,
                              max(nullif(upper(trim(IDENTIFIER_TICKER)), '')) AS ticker
                       FROM {ident} WHERE HOLDING_ID IN (SELECT HOLDING_ID FROM h) GROUP BY 1)
            SELECT '{quarter}' AS quarter, h.accession, h.filing_date, h.report_date, h.series_id,
                   trim(h.ISSUER_NAME) AS name, trim(h.ISSUER_TITLE) AS title,
                   CASE WHEN length(trim(h.ISSUER_CUSIP)) = 9 AND trim(h.ISSUER_CUSIP) <> '000000000'
                        THEN upper(trim(h.ISSUER_CUSIP)) END AS cusip,
                   i.isin, i.ticker, TRY_CAST(h.BALANCE AS DOUBLE) AS balance, nullif(trim(h.UNIT), '') AS unit,
                   TRY_CAST(h.CURRENCY_VALUE AS DOUBLE) AS value_usd, TRY_CAST(h.PERCENTAGE AS DOUBLE) AS pct,
                   nullif(trim(h.PAYOFF_PROFILE), '') AS payoff, nullif(trim(h.ASSET_CAT), '') AS asset_cat,
                   nullif(trim(h.ISSUER_TYPE), '') AS issuer_type, h.net_assets,
                   DATE '{meta['as_of']}' AS source_as_of, CAST({float(meta['fetched_at'])} AS DOUBLE) AS fetched_at,
                   '{meta['run_id']}' AS run_id
            FROM h LEFT JOIN i ON i.HOLDING_ID = h.HOLDING_ID
            ORDER BY h.series_id, h.report_date, pct DESC NULLS LAST, cusip"""

    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        funds = [f for _, f in stage.partitions(parquet_dir(), "quarter", "fund.parquet")]
        holdings = [f for _, f in stage.partitions(parquet_dir(), "quarter")]
        # Each series' newest month over every held quarter, and that filing's holdings.
        stage.create(con, FUNDS, FUND, funds,
                     select="SELECT * FROM {src} QUALIFY row_number() OVER (PARTITION BY series_id "
                            "ORDER BY report_date DESC NULLS LAST, filing_date DESC NULLS LAST, accession DESC) = 1")
        stage.create(con, STAGING, HOLDING, holdings,
                     select=f"SELECT * FROM {{src}} WHERE accession IN (SELECT accession FROM {FUNDS})")
        return {}

    def check(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict:
        n_series, newest = con.execute(f"SELECT count(*), max(report_date) FROM {FUNDS}").fetchone()
        on = ctx.as_of or self.default_as_of()
        ctx.notes.append(f"{n_series} ETF series, newest report {newest or 'none'}")
        return {"dataset": NAME, "result": "changed" if ctx.pending.get(NAME) else "unchanged",
                "source_as_of": on.isoformat(), "rows": stage.count(con, STAGING), "added": 0, "removed": 0}


DATASET = Nport()
