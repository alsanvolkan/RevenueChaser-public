"""``funds/etf_holdings``: the whole holdings of the rotation board's ETFs, from each issuer's own file (daily).

The sibling of ``funds/index_holdings``, which reads the index funds' files and votes index membership. This one
votes nothing: it keeps every holding line of every ETF, with its weight, as the file or the answer gives it, so the
relatedness peer sets have a source (``module_relatedness`` Step 0 derives them from the served table).

| Group | Funds | Source |
|---|---|---|
| SSGA SPDR (18) | the 11 sector funds, ``XBI XOP XME KRE XHB XRT KIE`` | the issuer's daily holdings xlsx |
| iShares (6) | ``SOXX IBB ITA ITB IGV IAK`` | the product page's ``latest-holdings.csv`` |
| Unusual Whales (14) | ``SMH PAVE AIQ LIT URA NLR AIRR SKYY WCLD ARKQ ROBO JETS TAN PBW`` | ``/etfs/<t>/holdings`` |
| ARK, behind UW (1) | ``ARKQ``, on a day UW holds nothing for it | ARK's daily holdings CSV |

**The issuer of each fund was confirmed by fetching it once, 30 Oct 2026**, not taken from memory: IAK is iShares'
insurance fund (the SPDR path answers 404; KIE is the SPDR one), and the iShares product ids are the issuer's own
product screener's. Both parsers are ``funds``' own, unchanged but for the two iShares shapes (``parse_ishares``).
Invesco answers 500 for TAN and PBW, so those two are UW's like the rest of the themes.

**UW first, the issuer file behind it (#339).** On 1 Nov 2026 UW answered ``{"data": []}`` for AIRR, SKYY, WCLD, ARKQ
and ROBO. Their issuers were checked that day: ARK publishes ARKQ's holdings as a CSV, and that file is ``FALLBACKS``.
First Trust (AIRR, SKYY) serves an HTML table whose Excel export is an ASP.NET postback, WisdomTree (WCLD) embeds
the list in its page and refused the builder's User-Agent, and Exchange Traded Concepts (ROBO) builds its CSV in
the browser. None of those is a file, so those four stay UW's alone and are not scraped. The raw file's name says
which source served a fund's day (``uw-ARKQ.json`` or ``ark-ARKQ.csv``), and the run note names each fallback.

The funds are the board's ``sector`` and ``theme`` ETFs (``core/rotation_reference.ROTATION_ETFS``) less GLD, SLV and
USO — commodity trusts, with no equity holdings — plus the seven in ``EXTRA`` that the peer sets need and the board
does not run. **UW's answer is kept whole:** the top-10 cap is the Rotation board's own
(``module_sector_rotation/etf_holdings.py``), and that board's ``raw.duckdb.etf_holdings`` is untouched by this
dataset, which writes only the market store.

One request per fund per run (24 issuer files, 14 UW calls, plus ARK's file on a day it is needed), each inside
its provider's day cap.

**The sweep (#358, avo 1-2 Nov 2026):** every other ETF of the live spine's ``etf`` table, no ETN and no CEF (5,854 on
2 Nov 2026), is asked from UW once every ``SWEEP_DAYS`` (30), at most ``SWEEP_PER_RUN`` (3,000) a run, under the
``uw`` day cap: a sweep is about 5,850 calls over two hourly runs, the daily funds 14 a day. Its state is
``raw/funds/etf_holdings/sweep.json`` (its day, empty and failed answers, every sweep day); a cut sweep goes on under
its first day, an empty answer is not asked again within the sweep and ends as one ``uw_empty`` issue.
**Retention:** the last 30 days whole; older, each month's last held day and each sweep day. It only names what it
would remove unless ``RC_ETF_HOLDINGS_PRUNE=1``. No key for the issuer files; without ``UNUSUAL_WHALES_API_KEY`` the issuer files are still
read (ARKQ's among them) and the missing key is one ``missing_key`` issue. A fund whose every source failed (a moved
shape or URL, an empty answer) is a ``source_failed`` issue for that fund, and the others go on, as the index job
does; every fund failing fails the run.

Raw: ``raw/funds/etf_holdings/<as-of>/<provider>-<etf>.<ext>`` (+ ``.meta.json``, written first: the raw file is the
resume marker). Parquet: ``parquet/funds/etf_holdings/date=<as-of>/part-0.parquet``, every day kept. Served:
``etf_holdings(as_of, etf, provider, ticker, cusip, name, weight)``, this dataset's own table in ``spine.duckdb``.
"""
from __future__ import annotations

import csv
import io
import json
import os
import shutil
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb

from ....core import calendar, credentials
from .. import base
from . import KIND, stage
from .funds import _cusip, norm_ticker, parse_ishares, parse_ssga, weight_of

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
# ARK's daily holdings CSV, by fund: the file name under ``/fund-documents/funds-etf-csv/`` on ARK's asset host, as
# the fund page links it (read 1 Nov 2026, #339). ARK is the issuer file UW's empty answer falls back to.
ARK_FUNDS = {"ARKQ": "ARK_AUTONOMOUS_TECH._&_ROBOTICS_ETF_ARKQ_HOLDINGS.csv"}
ARK_COLUMNS = ("fund", "company", "ticker", "cusip", "weight (%)")
# Bloomberg's US exchange codes. ARK's file writes a few tickers with one (``SYM UQ``, 30 Oct 2026); any of these is
# the US listing itself, so dropping it is no guess. Any other suffix is a foreign listing (``6954 JT``, ``KOG NO``):
# ``ticker_raw`` keeps what the file wrote and the line has no ticker, because ``norm_ticker`` would read ``KOG NO`` as
# the US share class ``KOG-NO``.
US_EXCHANGE_CODES = frozenset({"UA", "UN", "UP", "UQ", "UR", "US", "UV", "UW"})
# The themes whose issuer has no keyless holdings file (VanEck, Global X, First Trust, WisdomTree, ROBO's adviser
# Exchange Traded Concepts, U.S. Global) and the two Invesco funds its API answers 500 for. ARKQ is UW's first and
# ARK's file second (``FALLBACKS``).
UW_FUNDS = ("SMH", "PAVE", "AIQ", "LIT", "URA", "NLR", "AIRR", "SKYY", "WCLD", "ARKQ", "ROBO", "JETS", "TAN", "PBW")
# The funds the relatedness peer sets need that the rotation board does not run.
EXTRA = frozenset({"XHB", "XRT", "KIE", "IAK", "ITB", "TAN", "PBW"})
# The sweep (#358, avo 1-2 Nov 2026): every other ETF of the spine's ``etf`` table, no ETN and no CEF, from UW once
# every ``SWEEP_DAYS``. One run asks at most ``SWEEP_PER_RUN`` of them (the step's own guard, under the ``uw`` cap);
# the rest wait for the next run, which goes on with the same sweep day.
SWEEP_DAYS = 30
SWEEP_PER_RUN = 3000
SWEEP_STATE = "sweep.json"
# Retention (avo, option 1): the last ``SWEEP_DAYS`` days whole; older, each month's last day and each sweep day. It
# only reports what it would remove unless ``PRUNE_ENV`` is "1": the first real removal is avo's word at that time.
PRUNE_ENV = "RC_ETF_HOLDINGS_PRUNE"


def parse_uw(data: bytes) -> list[dict]:
    """UW's ``/etfs/<ticker>/holdings`` answer: every holding, by weight, one row per ticker (the newest ``updated``
    wins, the client's own rule — ``core/providers/unusual_whales._holdings``). The ``cusip`` a row carries (the live
    answer has the key though ``docs/openapi.yaml`` does not name it, SMH on 1 Nov 2026) is kept when it is a CUSIP,
    normalised as the issuer parsers' are; None when absent or not one."""
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
    out = [{"ticker_raw": r.get("ticker"), "cusip": _cusip(r.get("cusip")), "name": r.get("name") or r.get("short_name"),
            "weight": weight_of(r.get("weight"))} for r in by_ticker.values()]
    out.sort(key=lambda h: h["weight"] if h["weight"] is not None else -1.0, reverse=True)
    return out


def _ark_ticker(raw: str) -> str:
    parts = raw.split()
    return parts[0] if len(parts) == 2 and parts[1] in US_EXCHANGE_CODES else raw


def parse_ark(data: bytes, fund: str) -> list[dict]:
    """ARK's daily holdings CSV (read 1 Nov 2026, #339): a header ``date, fund, company, ticker, cusip, shares,
    market value ($), weight (%)``, one line per position, then one quoted disclaimer line.

    **Kept:** the lines with a ticker (the file's 36 stocks and ADRs on 30 Oct 2026), with their CUSIP and the weight
    in percent. **Dropped:** a line with no ticker — on that day the one money-market fund (``GOLDMAN FS TRSY OBLIG
    INST 468``, identifier ``X9USDGSFT``, which looks like a CUSIP and is none) — and the disclaimer, which has no
    ``fund``. A trailing US Bloomberg exchange code is dropped (``SYM UQ`` is ``SYM``); nothing else is rewritten. A
    ticker with any other suffix is a foreign listing (``6954 JT``, ``KOG NO``): ``ticker_raw`` keeps it and the
    line's ``ticker`` is None, so the served table drops it and its CUSIP stays in the Parquet.

    A file without the header, without one of its columns, or whose lines name another fund is a failed file (a
    moved URL answers a page or another fund's file), never a quiet reread."""
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    fields = [f.strip() for f in reader.fieldnames or ()]
    if "ticker" not in fields or "fund" not in fields:
        raise base.SourceError("ark: no header line with fund and ticker")
    missing = [c for c in ARK_COLUMNS if c not in fields]
    if missing:
        raise base.SourceError(f"ark: the file has no {', '.join(missing)} column")
    reader.fieldnames = fields
    out = []
    for r in reader:
        name_of_fund = (r.get("fund") or "").strip()
        if not name_of_fund:
            continue                                         # the disclaimer line
        if name_of_fund != fund:
            raise base.SourceError(f"ark: the file for {fund} holds {name_of_fund} lines")
        ticker = _ark_ticker((r.get("ticker") or "").strip())
        if not ticker:
            continue                                         # the money-market fund (cash)
        line = {"ticker_raw": ticker, "cusip": _cusip(r.get("cusip")), "name": (r.get("company") or "").strip(),
                "weight": weight_of(r.get("weight (%)"))}
        if " " in ticker:
            line["ticker"] = None                            # a foreign listing: no US ticker, the CUSIP stays
        out.append(line)
    return out


def served_ticker(line: dict) -> str | None:
    """The ticker a parsed line is served under: the parser's own verdict when it gave one (ARK's foreign listings
    carry ``ticker`` None), else ``norm_ticker`` of the raw ticker."""
    return line["ticker"] if "ticker" in line else norm_ticker(line["ticker_raw"])


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
        if self.provider == "ark":
            return (provider_base("RC_ARK_BASE", "https://assets.ark-funds.com")
                    + "/fund-documents/funds-etf-csv/" + ARK_FUNDS[self.etf])
        from ....core.providers import unusual_whales
        return f"{unusual_whales.BASE}/etfs/{self.etf}/holdings"

    def parse(self, data: bytes) -> list[dict]:
        if self.provider == "ssga":
            return parse_ssga(data)
        if self.provider == "ishares":
            return parse_ishares(data, typed=ISHARES_PRODUCTS[self.etf][2])
        if self.provider == "ark":
            return parse_ark(data, self.etf)
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
# The issuer file behind a UW fund, asked only when UW holds nothing for it that day (or there is no UW key).
FALLBACKS = {t: EtfFile(t, "ark", "csv") for t in ARK_FUNDS}
PROVIDERS = tuple(dict.fromkeys([f.provider for f in FILES] + [f.provider for f in FALLBACKS.values()]))
_BY_RAW = {f.raw_name(): f for f in FILES + tuple(FALLBACKS.values())}
DAILY = frozenset(f.etf for f in FILES)


def _file_of(raw_name: str) -> "EtfFile | None":
    """The daily fund a raw file is, else the swept ETF its ``uw-<ticker>.json`` name says."""
    f = _BY_RAW.get(raw_name)
    if f is None and raw_name.startswith("uw-") and raw_name.endswith(".json"):
        f = EtfFile(raw_name[3:-5], "uw", "json")
    return f


def sweep_etfs() -> list[str]:
    """The live spine's ETFs the sweep asks: no ETN, no CEF, none of the daily funds. Empty before the first spine."""
    from ... import serving
    if not serving.live_path(KIND).exists():
        return []
    with serving.open_read(KIND) as con:
        if not con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = 'etf'").fetchone()[0]:
            return []
        rows = con.execute("SELECT ticker FROM etf WHERE NOT coalesce(is_etn, false) AND NOT coalesce(is_cef, false) "
                           "ORDER BY ticker").fetchall()
    return [t for (t,) in rows if t not in DAILY]


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
        """Each fund's file of the day, once. A UW fund with an issuer file in ``FALLBACKS`` takes UW's answer when
        it holds at least one holding, else the issuer file; the raw file's name says which one served the day and
        the run note names every fallback. Only when every source of a fund fails is it a ``source_failed`` issue,
        keyed by the fund's own file and naming each source's failure."""
        keyed = bool(credentials.key_for(UW_KEY))
        if not keyed:
            ctx.issue("missing_key", UW_KEY, credentials.missing_message(UW_KEY))
            ctx.notes.append(f"no key for {UW_KEY}: the {len(UW_FUNDS)} UW funds were not asked")
        out, failed = [], []
        day = raw_dir() / as_of.isoformat()
        for f in FILES:
            sources = [f] + ([FALLBACKS[f.etf]] if f.provider == "uw" and f.etf in FALLBACKS else [])
            if f.provider == "uw" and not keyed:
                sources = sources[1:]
            if not sources:
                continue
            done = next((day / s.raw_name() for s in sources if (day / s.raw_name()).exists()), None)
            if done is not None:
                out.append(done)
                continue
            errors = [] if keyed or f.provider != "uw" else [f"{f.raw_name()}: no {UW_KEY} key"]
            for src in sources:
                try:
                    content = src.download(ctx)
                    # Refuse a file of the wrong shape, and an answer with no holding in it, before keeping it: the
                    # raw file is the resume marker, so a kept empty answer would be the fund's whole day.
                    if not src.parse(content):
                        raise base.SourceError("the answer holds no holding line")
                except Exception as e:  # noqa: BLE001 - one fund down must not stop the others
                    errors.append(f"{src.raw_name()}: {type(e).__name__}: {e}"[:200])
                    continue
                raw = day / src.raw_name()
                base.write_raw_json(_meta(raw), {"as_of": as_of.isoformat(), "fetched_at": time.time(),
                                                 "run_id": ctx.run_id, "etf": src.etf, "provider": src.provider,
                                                 "url": src.url().split("?")[0]})
                out.append(base.write_raw(raw, content))
                if src is not f:
                    ctx.notes.append(f"fallback: {f.etf} served by {src.raw_name()} ({errors[0][:140]})")
                break
            else:
                failed.append(f.raw_name())
                ctx.issue("source_failed", f.raw_name(), " | ".join(errors)[:400])
        if failed:
            ctx.notes.append("failed files: " + ", ".join(failed))
            if not out:
                raise base.SourceError("every fund file failed")
        if keyed:
            out += self._sweep(ctx, as_of)
        self._retain(ctx, as_of)
        return out

    def _retain(self, ctx: base.RunContext, as_of: date) -> None:
        """The days past ``SWEEP_DAYS`` that are neither their month's last held day nor a sweep day: named in the
        note, and removed (raw day folder and Parquet partition together) only when ``PRUNE_ENV`` is "1"."""
        path = raw_dir() / SWEEP_STATE
        sweeps = set(json.loads(path.read_text()).get("days", [])) if path.exists() else set()
        folders = {p.name: p for p in raw_dir().iterdir() if p.is_dir()} if raw_dir().exists() else {}
        parts = {p.name.split("=", 1)[1]: p for p in parquet_dir().iterdir()
                 if p.is_dir() and p.name.startswith("date=")} if parquet_dir().exists() else {}
        days = sorted(set(folders) | set(parts))
        last = {}
        for d in days:
            last[d[:7]] = d
        drop = [d for d in days if (as_of - date.fromisoformat(d)).days > SWEEP_DAYS
                and d not in sweeps and last[d[:7]] != d]
        if not drop:
            return
        n = f"{len(drop)} day{'s' if len(drop) != 1 else ''}"
        if os.environ.get(PRUNE_ENV) != "1":
            ctx.notes.append(f"retention (dry run): {n} past {SWEEP_DAYS} would be removed: " + ", ".join(drop))
            return
        for d in drop:
            for p in (folders.get(d), parts.get(d)):
                if p is not None:
                    shutil.rmtree(p)
        ctx.notes.append(f"retention: removed {n} past {SWEEP_DAYS}: " + ", ".join(drop))

    def _sweep(self, ctx: base.RunContext, as_of: date) -> list[Path]:
        """The sweep's share of this run: start one when the last is ``SWEEP_DAYS`` old (or there is none), else go on
        with an unfinished one under its own day. An empty or failed answer is recorded and not asked again within
        the sweep; the raw file is the resume marker. A cap stop leaves the sweep for the next run."""
        path = raw_dir() / SWEEP_STATE
        state = json.loads(path.read_text()) if path.exists() else None
        if state is None or (state["done"] and (as_of - date.fromisoformat(state["day"])).days >= SWEEP_DAYS):
            state = {"day": as_of.isoformat(), "done": False, "empty": [], "failed": [],
                     "days": (state or {}).get("days", []) + [as_of.isoformat()]}
        elif state["done"]:
            return []
        day = date.fromisoformat(state["day"])
        folder = raw_dir() / day.isoformat()
        skip = set(state["empty"]) | set(state["failed"])
        todo = [t for t in sweep_etfs() if t not in skip and not (folder / f"uw-{t}.json").exists()]
        out, asked, stopped = [], 0, None
        for t in todo:
            if asked >= SWEEP_PER_RUN:
                stopped = f"the run's {SWEEP_PER_RUN} sweep calls"
                break
            f = EtfFile(t, "uw", "json")
            try:
                asked += 1
                content = f.download(ctx)
                if not f.parse(content):
                    state["empty"].append(t)
                    continue
            except base.CapReached as e:
                asked -= 1
                stopped = str(e)
                break
            except Exception:  # noqa: BLE001 - one ETF down must not stop the sweep
                state["failed"].append(t)
                continue
            raw = folder / f.raw_name()
            base.write_raw_json(_meta(raw), {"as_of": day.isoformat(), "fetched_at": time.time(),
                                             "run_id": ctx.run_id, "etf": t, "provider": "uw", "url": f.url()})
            out.append(base.write_raw(raw, content))
        left = len(todo) - asked
        state["done"] = stopped is None
        base.write_raw_json(path, state)
        ctx.notes.append(f"sweep {day}: {asked} asked this run, {len(out)} held, {len(state['empty'])} empty, "
                         f"{len(state['failed'])} failed, {max(left, 0)} left" + (f" (stopped: {stopped})" if stopped else ""))
        if state["done"] and (state["empty"] or state["failed"]):
            names = state["empty"] + state["failed"]
            ctx.issue("uw_empty", f"sweep-{day}", f"{len(names)} ETFs with no UW holdings: " + ", ".join(names[:30]))
        return out

    def backfill(self, ctx: base.RunContext, start: date | None, end: date) -> None:
        # Each file holds today's holdings only: a backfill is today's fetch, as the index job's is.
        self.to_parquet_all(ctx, self.fetch(ctx, end))

    def to_parquet(self, ctx: base.RunContext, raw: Path) -> list[Path]:
        return self.to_parquet_all(ctx, [raw])

    def to_parquet_all(self, ctx: base.RunContext, raws: list[Path]) -> list[Path]:
        by_day: dict[date, list[dict]] = {}
        for raw in raws:
            f = _file_of(raw.name)
            if f is None:
                continue
            meta = base.read_raw_json(_meta(raw))
            as_of = date.fromisoformat(meta["as_of"])
            for line in f.parse(base.read_raw(raw)):
                by_day.setdefault(as_of, []).append({
                    "as_of": as_of, "etf": f.etf, "provider": f.provider,
                    "ticker": served_ticker(line), "ticker_raw": line["ticker_raw"],
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
        daily = ", ".join(f"'{t}'" for t in sorted(DAILY))   # the sweep's ETFs on a sweep day are no daily fund
        funds = int(con.execute(f"SELECT count(DISTINCT etf) FROM {TABLE} WHERE {day} AND etf IN ({daily})")
                    .fetchone()[0]) if latest else 0
        keys = ["as_of", "etf", "ticker"]
        added, removed = stage.compare_live(con, f"SELECT as_of, etf, ticker FROM {TABLE}", TABLE, keys)
        return {"dataset": NAME, "result": "changed" if added or removed else "unchanged",
                "source_as_of": (latest or ctx.as_of or self.default_as_of()).isoformat(),
                "rows": stage.count(con, TABLE, day) if latest else 0, "added": added, "removed": removed,
                "coverage": round(funds / len(FILES), 4)}


DATASET = EtfHoldings()
