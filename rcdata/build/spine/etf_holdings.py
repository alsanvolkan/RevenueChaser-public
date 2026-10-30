"""``funds/etf_holdings``: the whole holdings of the rotation board's ETFs, from each issuer's own file (daily).

The sibling of ``funds/index_holdings``, which reads the index funds' files and votes index membership. This one
votes nothing: it keeps every holding line of every ETF, with its weight, as the file or the answer gives it, so the
relatedness peer sets have a source (``module_relatedness`` Step 0 derives them from the served table).

| Group | Funds | Source |
|---|---|---|
| SSGA SPDR (18) | the 11 sector funds, ``XBI XOP XME KRE XHB XRT KIE`` | the issuer's daily holdings xlsx |
| iShares (6) | ``SOXX IBB ITA ITB IGV IAK`` | the product page's ``latest-holdings.csv`` |
| Unusual Whales (14) | ``SMH PAVE AIQ LIT URA NLR AIRR SKYY WCLD ARKQ ROBO JETS TAN PBW`` | ``/etfs/<t>/holdings`` |

**The issuer of each fund was confirmed by fetching it once, 30 Oct 2026**, not taken from memory: IAK is iShares'
insurance fund (the SPDR path answers 404; KIE is the SPDR one), and the iShares product ids are the issuer's own
product screener's. Both parsers are ``funds``' own, unchanged but for the two iShares shapes (``parse_ishares``).
Invesco answers 500 for TAN and PBW, so those two are UW's like the rest of the themes.

The funds are the board's ``sector`` and ``theme`` ETFs (``core/rotation_reference.ROTATION_ETFS``) less GLD, SLV and
USO — commodity trusts, with no equity holdings — plus the seven in ``EXTRA`` that the peer sets need and the board
does not run. **UW's answer is kept whole:** the top-10 cap is the Rotation board's own
(``module_sector_rotation/etf_holdings.py``), and that board's ``raw.duckdb.etf_holdings`` is untouched by this
dataset, which writes only the market store.

One request per fund per run (24 issuer files, 14 UW calls), each inside its provider's day cap. No key for the
issuer files; without ``UNUSUAL_WHALES_API_KEY`` the issuer files are still read and the missing key is one
``missing_key`` issue. A file whose shape or URL moved is a ``source_failed`` issue for that fund and the others go
on, as the index job does; every fund failing fails the run.

Raw: ``raw/funds/etf_holdings/<as-of>/<provider>-<etf>.<ext>`` (+ ``.meta.json``, written first: the raw file is the
resume marker). Parquet: ``parquet/funds/etf_holdings/date=<as-of>/part-0.parquet``, every day kept. Served:
``etf_holdings(as_of, etf, provider, ticker, cusip, name, weight)``, this dataset's own table in ``spine.duckdb``.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb

from ....core import calendar, credentials
from .. import base
from . import KIND, stage
from .funds import norm_ticker, parse_ishares, parse_ssga, weight_of

NAME = "funds/etf_holdings"
TABLE = "etf_holdings"
UW_KEY = "unusual_whales"
# What the Parquet keeps, and what the table serves (the raw ticker and the run stamps stay in the Parquet).
SCHEMA = [("as_of", "DATE"), ("etf", "TEXT"), ("provider", "TEXT"), ("ticker", "TEXT"), ("ticker_raw", "TEXT"),
          ("cusip", "TEXT"), ("name", "TEXT"), ("weight", "DOUBLE"), ("fetched_at", "DOUBLE"), ("run_id", "TEXT")]
SERVED = [(c, t) for c, t in SCHEMA if c not in ("ticker_raw", "fetched_at", "run_id")]
META_SUFFIX = ".meta.json"

# The SPDR funds: the 11 sector funds and the seven industry ones (all one URL template, by ticker).
SSGA_FUNDS = ("XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY",
              "XBI", "XOP", "XME", "KRE", "XHB", "XRT", "KIE")
# The iShares funds, by (product id, page slug, typed), from the issuer's product screener (30 Oct 2026). The slug
# is the page's own; the id is what answers, as ``funds.ISHARES_PRODUCTS`` says for the core funds. ``typed`` is
# which of the issuer's two CSV shapes the fund serves, measured per fund on that day: the five sector and industry
# funds carry no ``Type`` column, IAK does. The shape is the fund's, so a file that changes shape fails loudly.
ISHARES_PRODUCTS = {"SOXX": ("239705", "ishares-semiconductor-etf", False),
                    "IBB": ("239699", "ishares-biotechnology-etf", False),
                    "ITA": ("239502", "ishares-u-s-aerospace-defense-etf", False),
                    "ITB": ("239512", "ishares-u-s-home-construction-etf", False),
                    "IGV": ("239771", "ishares-expanded-tech-software-sector-etf", False),
                    "IAK": ("239515", "ishares-u-s-insurance-etf", True)}
# The themes whose issuer has no keyless holdings file (VanEck, Global X, First Trust, WisdomTree, ARK, ROBO Global,
# U.S. Global) and the two Invesco funds its API answers 500 for.
UW_FUNDS = ("SMH", "PAVE", "AIQ", "LIT", "URA", "NLR", "AIRR", "SKYY", "WCLD", "ARKQ", "ROBO", "JETS", "TAN", "PBW")
# The funds the relatedness peer sets need that the rotation board does not run.
EXTRA = frozenset({"XHB", "XRT", "KIE", "IAK", "ITB", "TAN", "PBW"})


def parse_uw(data: bytes) -> list[dict]:
    """UW's ``/etfs/<ticker>/holdings`` answer: every holding, by weight, one row per ticker (the newest ``updated``
    wins, the client's own rule — ``core/providers/unusual_whales._holdings``). No CUSIP: UW's rows carry none."""
    try:
        rows = json.loads(data)["data"]
    except (ValueError, KeyError, TypeError) as e:
        raise base.SourceError(f"uw holdings: not the documented shape ({type(e).__name__}: {e})") from e
    if not isinstance(rows, list):
        raise base.SourceError("uw holdings: data is not a list")
    by_ticker: dict[str, dict] = {}
    for r in rows:
        t = (r or {}).get("ticker")
        if not t:
            continue
        prev = by_ticker.get(t)
        if prev is None or (r.get("updated") or "") > (prev.get("updated") or ""):
            by_ticker[t] = r
    out = [{"ticker_raw": r.get("ticker"), "cusip": None, "name": r.get("name") or r.get("short_name"),
            "weight": weight_of(r.get("weight"))} for r in by_ticker.values()]
    out.sort(key=lambda h: h["weight"] if h["weight"] is not None else -1.0, reverse=True)
    return out


@dataclass(frozen=True)
class EtfFile:
    etf: str
    provider: str
    ext: str

    def url(self) -> str:
        from .funds import provider_base
        if self.provider == "ssga":
            return (provider_base("RC_SSGA_BASE", "https://www.ssga.com")
                    + "/us/en/intermediary/library-content/products/fund-data/etfs/us/"
                    + f"holdings-daily-us-en-{self.etf.lower()}.xlsx")
        if self.provider == "ishares":
            pid, slug, _typed = ISHARES_PRODUCTS[self.etf]
            return (provider_base("RC_ISHARES_BASE", "https://www.ishares.com")
                    + f"/us/products/{pid}/{slug}/latest-holdings.csv")
        from ....core.providers import unusual_whales
        return f"{unusual_whales.BASE}/etfs/{self.etf}/holdings"

    def parse(self, data: bytes) -> list[dict]:
        if self.provider == "ssga":
            return parse_ssga(data)
        if self.provider == "ishares":
            return parse_ishares(data, typed=ISHARES_PRODUCTS[self.etf][2])
        return parse_uw(data)

    def raw_name(self) -> str:
        return f"{self.provider}-{self.etf}.{self.ext}"

    def download(self, ctx: base.RunContext) -> bytes:
        if self.provider != "uw":
            return base.http_get(ctx, self.provider, self.url(), headers={"Accept": "*/*"}).content
        ctx.check_cap("uw")
        endpoint = f"/etfs/{self.etf}/holdings"
        res = ctx.uw_client().get_result(endpoint)
        if res.status != "ok":
            raise base.SourceError(f"uw {endpoint}: {res.error}")
        return json.dumps(res.data).encode()


FILES = (tuple(EtfFile(t, "ssga", "xlsx") for t in SSGA_FUNDS)
         + tuple(EtfFile(t, "ishares", "csv") for t in ISHARES_PRODUCTS)
         + tuple(EtfFile(t, "uw", "json") for t in UW_FUNDS))
PROVIDERS = tuple(dict.fromkeys(f.provider for f in FILES))
_BY_RAW = {f.raw_name(): f for f in FILES}


def raw_dir() -> Path:
    return base.raw_root() / "funds" / "etf_holdings"


def parquet_dir() -> Path:
    return base.parquet_root() / "funds" / "etf_holdings"


def _meta(raw: Path) -> Path:
    return raw.with_name(raw.name + META_SUFFIX)


class EtfHoldings:
    name = NAME
    kind = KIND
    providers = PROVIDERS
    tables = (TABLE,)
    cadence = "daily"
    backfill_needs_from = False
    starts_by_hand = True             # not due until a first CLI run or Fetch built it

    def default_as_of(self) -> date:
        return calendar.today_ny()

    def fetch(self, ctx: base.RunContext, as_of: date) -> list[Path]:
        keyed = bool(credentials.key_for(UW_KEY))
        if not keyed:
            ctx.issue("missing_key", UW_KEY, credentials.missing_message(UW_KEY))
            ctx.notes.append(f"no key for {UW_KEY}: the {len(UW_FUNDS)} UW funds were not asked")
        out, failed = [], []
        for f in FILES:
            if f.provider == "uw" and not keyed:
                continue
            raw = raw_dir() / as_of.isoformat() / f.raw_name()
            if raw.exists():
                out.append(raw)
                continue
            try:
                content = f.download(ctx)
                # Refuse a file of the wrong shape, and an answer with no holding in it, before keeping it: the raw
                # file is the resume marker, so a kept empty answer would be the fund's whole day.
                if not f.parse(content):
                    raise base.SourceError(f"{f.raw_name()}: the answer holds no holding line")
            except Exception as e:  # noqa: BLE001 - one fund down must not stop the others
                failed.append(f.raw_name())
                ctx.issue("source_failed", f.raw_name(), f"{type(e).__name__}: {e}"[:300])
                continue
            base.write_raw_json(_meta(raw), {"as_of": as_of.isoformat(), "fetched_at": time.time(),
                                             "run_id": ctx.run_id, "etf": f.etf, "provider": f.provider,
                                             "url": f.url().split("?")[0]})
            out.append(base.write_raw(raw, content))
        if failed:
            ctx.notes.append("failed files: " + ", ".join(failed))
            if not out:
                raise base.SourceError("every fund file failed")
        return out

    def backfill(self, ctx: base.RunContext, start: date | None, end: date) -> None:
        # Each file holds today's holdings only: a backfill is today's fetch, as the index job's is.
        self.to_parquet_all(ctx, self.fetch(ctx, end))

    def to_parquet(self, ctx: base.RunContext, raw: Path) -> list[Path]:
        return self.to_parquet_all(ctx, [raw])

    def to_parquet_all(self, ctx: base.RunContext, raws: list[Path]) -> list[Path]:
        by_day: dict[date, list[dict]] = {}
        for raw in raws:
            f = _BY_RAW.get(raw.name)
            if f is None:
                continue
            meta = base.read_raw_json(_meta(raw))
            as_of = date.fromisoformat(meta["as_of"])
            for line in f.parse(base.read_raw(raw)):
                by_day.setdefault(as_of, []).append({
                    "as_of": as_of, "etf": f.etf, "provider": f.provider,
                    "ticker": norm_ticker(line["ticker_raw"]), "ticker_raw": line["ticker_raw"],
                    "cusip": line["cusip"], "name": line["name"], "weight": line["weight"],
                    "fetched_at": meta["fetched_at"], "run_id": meta["run_id"]})
        dropped = sum(1 for rows in by_day.values() for r in rows if r["ticker"] is None)
        if dropped:
            ctx.notes.append(f"{dropped} lines with no ticker of their own (not served)")
        out = []
        for as_of, rows in sorted(by_day.items()):
            final = parquet_dir() / f"date={as_of.isoformat()}" / "part-0.parquet"
            if final.exists():                       # the day's other funds, typed by an earlier run, stay in
                con = duckdb.connect()
                try:
                    keep = [dict(zip([c for c, _ in SCHEMA], r)) for r in con.execute(
                        f"SELECT * FROM read_parquet({base.sql_path(final)})").fetchall()]
                finally:
                    con.close()
                new = {r["etf"] for r in rows}
                rows = [r for r in keep if r["etf"] not in new] + rows
            con = duckdb.connect()
            try:
                base.rows_table(con, "rows", SCHEMA, rows)
                out.append(base.write_parquet(
                    con, "SELECT * FROM rows ORDER BY etf, weight DESC NULLS LAST, ticker", final))
            finally:
                con.close()
        return out

    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        files = [f for _, f in stage.partitions(parquet_dir(), "date")]
        cols = ", ".join(f'"{c}"' for c, _ in SERVED)
        con.execute("CREATE TABLE {} ({})".format(TABLE, ", ".join(f'"{c}" {t}' for c, t in SERVED)))
        if files:
            src = f"read_parquet([{', '.join(base.sql_path(f) for f in files)}], union_by_name = true)"
            # A line whose ticker does not parse is no holding of a stock. In the SPDR files of 30 Oct 2026 those
            # are two or three lines a fund, each with a made-up identifier: the money-market fund (924QSGII3),
            # the dollar (999USDZ92), the CVRs of past deals (CONTRA METSERA, 592CVR013) and earnout rights
            # (OMNIAB 12.5 EARNOUT). Keeping them would put identifiers in the table that are no CUSIP.
            con.execute(f"INSERT INTO {TABLE} SELECT {cols} FROM {src} WHERE ticker IS NOT NULL "
                        "ORDER BY as_of, etf, weight DESC NULLS LAST, ticker")
        return {TABLE: stage.count(con, TABLE)}

    def check(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict:
        latest = con.execute(f"SELECT max(as_of) FROM {TABLE}").fetchone()[0]
        day = f"as_of = DATE '{latest}'"
        funds = int(con.execute(f"SELECT count(DISTINCT etf) FROM {TABLE} WHERE {day}").fetchone()[0]) if latest else 0
        keys = ["as_of", "etf", "ticker"]
        added, removed = stage.compare_live(con, f"SELECT as_of, etf, ticker FROM {TABLE}", TABLE, keys)
        return {"dataset": NAME, "result": "changed" if added or removed else "unchanged",
                "source_as_of": (latest or ctx.as_of or self.default_as_of()).isoformat(),
                "rows": stage.count(con, TABLE, day) if latest else 0, "added": added, "removed": removed,
                "coverage": round(funds / len(FILES), 4)}


DATASET = EtfHoldings()
