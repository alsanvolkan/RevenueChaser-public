"""``funds/etf_holdings``: the whole holdings of the rotation board's ETFs, from each issuer's own file (daily).

The sibling of ``funds/index_holdings``, which reads the index funds' files and votes index membership. This one
votes nothing: it keeps every holding line of every ETF, with its weight, as the file or the answer gives it, so the
relatedness peer sets can have a source. Which table they read is the owner's decision E1 (#342): today
``module_relatedness`` reads ``raw.duckdb.etf_holdings`` (``themes.py``, ``validation/peer_sets.py``), not this one.

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
``uw`` day cap: a sweep is about 5,850 calls over two daily runs (about 3,014 on its first day with the daily funds'
14, the rest on the next trading day). Its state is ``raw/funds/etf_holdings/sweep.json`` (its day, empty and failed
answers, every sweep day), written before the first call: a cut or killed sweep goes on under its first day, and each
run types the sweep day's held raws its partition lacks. An empty answer, and a failed one (a 404, or a 5xx or a
timeout after the client's retries), is not asked again within the sweep; at its end they are one ``uw_empty`` and
one ``source_failed`` issue. A rejected key on an ETF's own call or an open breaker (UW down) stops the sweep with
nothing recorded, so the next run asks again; a stop at a new place is a ``source_failed`` issue, and when it stops
``SWEEP_STOPS`` (3) times in a row at the same ETF while UW answers other calls, those ETFs are recorded as failed and
the sweep goes on past them. A whole outage or a dead key records nothing, however long it lasts. A spine with no
ETF starts no sweep. A scoped request asks its ``names`` only and starts no sweep.
**Retention:** the last 30 days whole; older, each month's last held day and each sweep day. It only names what it
would remove unless ``RC_ETF_HOLDINGS_PRUNE=1`` **and** the request's ``allow_shrink`` (the swap's shrink guard would
refuse the smaller table after the files are gone). No key for the issuer files; without ``UNUSUAL_WHALES_API_KEY`` the issuer files are still
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
import re
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
# Stops at the same ETF, while UW answers other calls, before the ETFs there are taken as failed (review 2: five
# neighbours that answer 503 every time would open the breaker on every run and stall the sweep for good).
SWEEP_STOPS = 3
# Retention (avo, option 1): the last ``SWEEP_DAYS`` days whole; older, each month's last day and each sweep day. It
# only reports what it would remove unless ``PRUNE_ENV`` is "1": the first real removal is avo's word at that time.
PRUNE_ENV = "RC_ETF_HOLDINGS_PRUNE"
# Raws typed per pass: a sweep run types up to ``SWEEP_PER_RUN`` answers, merged into the day's partition in DuckDB.
TYPE_CHUNK = 500
# A requested name that can be a ticker (and so a path segment); the spine's own tickers are normalised already.
TICKER = re.compile(r"[A-Z0-9][A-Z0-9.\-]{0,11}")


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
        endpoint = _endpoint(self.etf)
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


def _endpoint(etf: str) -> str:
    from ....core.providers.unusual_whales import _seg
    return f"/etfs/{_seg(etf)}/holdings"


def _uw_down(ctx: base.RunContext, etf: str) -> str | None:
    """Why UW answers no ETF this run: the client's open breaker, or a rejected key on ``etf``'s own call (an earlier
    fund's 403 is that fund's), else None."""
    h = ctx.uw_client().health()
    if h["breaker_open"]:
        return "UW breaker open"
    auth = h["auth_error"]
    return "UW rejected the key" if auth and auth.get("endpoint") == _endpoint(etf) else None


def _is_day(name: str) -> bool:
    try:
        date.fromisoformat(name)
    except ValueError:
        return False
    return True


def raw_dir() -> Path:
    return base.raw_root() / "funds" / "etf_holdings"


def parquet_dir() -> Path:
    return base.parquet_root() / "funds" / "etf_holdings"


def _meta(raw: Path) -> Path:
    return raw.with_name(raw.name + META_SUFFIX)


def _untyped(day: str) -> list[Path]:
    """The day's held UW raws whose ETF its Parquet partition lacks: what a run cut after the download left."""
    folder = raw_dir() / day
    if not folder.is_dir():
        return []
    # A raw whose meta is gone (only a hand can remove it, the meta is written first) is left alone, not typed.
    held = {p.name[3:-5]: p for p in folder.glob("uw-*.json")
            if not p.name.endswith(META_SUFFIX) and _meta(p).exists()}
    part = parquet_dir() / f"date={day}" / "part-0.parquet"
    typed: set[str] = set()
    if part.exists():
        con = duckdb.connect()
        try:
            typed = {t for (t,) in con.execute(f"SELECT DISTINCT etf FROM read_parquet({base.sql_path(part)}, "
                                               "hive_partitioning = false)").fetchall()}
        finally:
            con.close()
    return [p for t, p in sorted(held.items()) if t not in typed]


class EtfHoldings:
    name = NAME
    kind = KIND
    providers = PROVIDERS
    tables = (TABLE,)
    cadence = "daily"
    backfill_needs_from = False
    starts_by_hand = True             # not due until a first CLI run or Fetch built it
    honours_scope = True              # a request's ``names``: those funds and ETFs only, no sweep (the trial)

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
        names = set(ctx.scope.names)
        for f in FILES:
            if names and f.etf not in names:
                continue
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
        if keyed and names:
            out += self._named(ctx, as_of, sorted(names - DAILY))
        elif keyed and ctx.scope:
            ctx.notes.append(f"no sweep: the request's scope ({ctx.scope.text()}) names no ETF")
        elif keyed:
            out += self._sweep(ctx, as_of)
        try:
            self._retain(ctx, as_of)
        except Exception as e:  # noqa: BLE001 - retention must not cost the run its typing
            ctx.notes.append(f"retention failed: {type(e).__name__}: {e}"[:200])
        return out

    def _named(self, ctx: base.RunContext, as_of: date, tickers: list[str]) -> list[Path]:
        """The ETFs a request names that are no daily fund, from UW under ``as_of``; the sweep's state is untouched."""
        out, empty, failed = [], [], []
        bad = [t for t in tickers if not TICKER.fullmatch(t)]
        if bad:
            ctx.notes.append("named: not a ticker, not asked: " + ", ".join(bad)[:200])
        tickers = [t for t in tickers if t not in bad]
        for t in tickers[:SWEEP_PER_RUN]:
            f = EtfFile(t, "uw", "json")
            raw = raw_dir() / as_of.isoformat() / f.raw_name()
            if raw.exists():
                out.append(raw)
                continue
            try:
                content = f.download(ctx)
            except base.CapReached:
                raise
            except Exception:  # noqa: BLE001 - one ETF down must not stop the others
                failed.append(t)
                continue
            if not f.parse(content):
                empty.append(t)
                continue
            base.write_raw_json(_meta(raw), {"as_of": as_of.isoformat(), "fetched_at": time.time(),
                                             "run_id": ctx.run_id, "etf": t, "provider": "uw", "url": f.url()})
            out.append(base.write_raw(raw, content))
        ctx.notes.append(f"named: {len(tickers)} ETFs, {len(out)} held, {len(empty)} empty, {len(failed)} failed"
                         + (f" ({', '.join(empty + failed)[:200]})" if empty or failed else ""))
        return out

    def _retain(self, ctx: base.RunContext, as_of: date) -> None:
        """The days past ``SWEEP_DAYS`` that are neither their month's last held day nor a sweep day: named in the
        note, and removed (raw day folder and Parquet partition together) only when ``PRUNE_ENV`` is "1"."""
        path = raw_dir() / SWEEP_STATE
        sweeps = set(json.loads(path.read_text()).get("days", [])) if path.exists() else set()
        folders = {p.name: p for p in raw_dir().iterdir() if p.is_dir()} if raw_dir().exists() else {}
        parts = {p.name.split("=", 1)[1]: p for p in parquet_dir().iterdir()
                 if p.is_dir() and p.name.startswith("date=")} if parquet_dir().exists() else {}
        days = sorted(d for d in set(folders) | set(parts) if _is_day(d))
        last = {}
        for d in days:
            last[d[:7]] = d
        drop = [d for d in days if (as_of - date.fromisoformat(d)).days > SWEEP_DAYS
                and d not in sweeps and last[d[:7]] != d]
        if not drop:
            return
        n = f"{len(drop)} day{'s' if len(drop) != 1 else ''}"
        listed = ", ".join(drop[:20]) + (f" and {len(drop) - 20} more" if len(drop) > 20 else "")
        if os.environ.get(PRUNE_ENV) != "1" or not ctx.allow_shrink:
            why = ", the request has no allow_shrink" if os.environ.get(PRUNE_ENV) == "1" else ""
            ctx.notes.append(f"retention (dry run{why}): {n} past {SWEEP_DAYS} would be removed: {listed}")
            return
        for d in drop:
            for p in (folders.get(d), parts.get(d)):
                if p is not None:
                    shutil.rmtree(p)
        ctx.notes.append(f"retention: removed {n} past {SWEEP_DAYS}: {listed}")

    def _sweep(self, ctx: base.RunContext, as_of: date) -> list[Path]:
        """The sweep's share of this run: start one when the last is ``SWEEP_DAYS`` old (or there is none), else go on
        with an unfinished one under its own day, and type the day's held raws its partition lacks. An empty answer
        or a failed one (a 404, a 5xx after retries) is recorded and not asked again within the sweep; the raw file is
        the resume marker.
        A cap stop, a rejected key or an open breaker records nothing and leaves the sweep for the next run, until it
        stopped ``SWEEP_STOPS`` times at the same ETF while UW answered other calls."""
        path = raw_dir() / SWEEP_STATE
        state = json.loads(path.read_text()) if path.exists() else None
        etfs = sweep_etfs()
        if not etfs:                                    # no spine or no etf table: no sweep that then waits 30 days
            ctx.notes.append("no sweep: the spine holds no ETF to sweep")
            return _untyped(state["day"]) if state else []
        if state is None or (state["done"] and (as_of - date.fromisoformat(state["day"])).days >= SWEEP_DAYS):
            state = {"day": as_of.isoformat(), "done": False, "empty": [], "failed": [],
                     "days": (state or {}).get("days", []) + [as_of.isoformat()]}
            base.write_raw_json(path, state)            # before the first call: a killed run goes on under this day
        day = date.fromisoformat(state["day"])
        out = _untyped(day.isoformat())
        if state["done"]:
            return out
        folder = raw_dir() / day.isoformat()
        skip = set(state["empty"]) | set(state["failed"])
        todo = [t for t in etfs if t not in skip and not (folder / f"uw-{t}.json").exists()]
        held, asked, streak, stopped = 0, 0, [], None
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
                    streak = []
                    base.write_raw_json(path, state)
                    continue
            except base.CapReached as e:
                asked -= 1
                stopped = str(e)
                break
            except Exception:  # noqa: BLE001 - one ETF down must not stop the sweep
                stopped = _uw_down(ctx, t)
                if stopped:
                    # A stop counts only where the last one was and while UW answered this run: a whole outage or a
                    # dead key stops every run at the same ETF and is UW's state, never the ETF's (review 3).
                    at = (streak or [t])[0]
                    h = ctx.uw_client().health()
                    answered = h["calls"] > h["failed"]
                    if at != state.get("stop_at"):
                        ctx.issue("source_failed", f"sweep-{day}-stopped", f"{stopped} at {at}: the sweep waits")
                    same = answered and at == state.get("stop_at")
                    state["stops"], state["stop_at"] = (state.get("stops", 0) + 1 if same else int(answered)), at
                    if state["stops"] < SWEEP_STOPS:   # UW, not these ETFs: the failures since its last answer either
                        state["failed"] = [x for x in state["failed"] if x not in streak]
                    else:                              # the same ETFs again and again: theirs, the sweep goes past
                        state["failed"].append(t)
                        state["stops"], state["stop_at"] = 0, None
                    break
                state["failed"].append(t)
                streak.append(t)
                base.write_raw_json(path, state)
                continue
            streak = []
            raw = folder / f.raw_name()
            base.write_raw_json(_meta(raw), {"as_of": day.isoformat(), "fetched_at": time.time(),
                                             "run_id": ctx.run_id, "etf": t, "provider": "uw", "url": f.url()})
            out.append(base.write_raw(raw, content))
            held += 1
        left = len(todo) - held - (len(state["empty"]) + len(state["failed"]) - len(skip))
        state["done"] = stopped is None
        base.write_raw_json(path, state)
        ctx.notes.append(f"sweep {day}: {asked} asked this run, {held} held, {len(state['empty'])} empty, "
                         f"{len(state['failed'])} failed, {left} left" + (f" (stopped: {stopped})" if stopped else ""))
        if state["done"] and state["empty"]:
            ctx.issue("uw_empty", f"sweep-{day}", f"{len(state['empty'])} ETFs with no UW holdings: "
                      + ", ".join(state["empty"][:30]))
        if state["done"] and state["failed"]:
            ctx.issue("source_failed", f"sweep-{day}", f"{len(state['failed'])} ETFs UW answered with an error: "
                      + ", ".join(state["failed"][:30]))
        return out

    def backfill(self, ctx: base.RunContext, start: date | None, end: date) -> None:
        # Each file holds today's holdings only: a backfill is today's fetch, as the index job's is.
        self.to_parquet_all(ctx, self.fetch(ctx, end))

    def to_parquet(self, ctx: base.RunContext, raw: Path) -> list[Path]:
        return self.to_parquet_all(ctx, [raw])

    def to_parquet_all(self, ctx: base.RunContext, raws: list[Path]) -> list[Path]:
        raws = list(dict.fromkeys(raws))             # a sweep day's daily UW fund is the day's and the sweep's raw
        out, dropped = [], 0
        for i in range(0, len(raws), TYPE_CHUNK):
            by_day: dict[date, list[dict]] = {}
            for raw in raws[i:i + TYPE_CHUNK]:
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
            dropped += sum(1 for rows in by_day.values() for r in rows if r["ticker"] is None)
            for as_of, rows in sorted(by_day.items()):
                final = parquet_dir() / f"date={as_of.isoformat()}" / "part-0.parquet"
                con = duckdb.connect()
                try:
                    base.rows_table(con, "rows", SCHEMA, rows)
                    select = "SELECT * FROM rows"
                    if final.exists():               # the day's other ETFs, typed by an earlier run or pass, stay in
                        select = (f"SELECT * FROM read_parquet({base.sql_path(final)}, hive_partitioning = false) "
                                  f"WHERE etf NOT IN (SELECT etf FROM rows) UNION ALL BY NAME {select}")
                    out.append(base.write_parquet(
                        con, f"SELECT * FROM ({select}) ORDER BY etf, weight DESC NULLS LAST, ticker", final))
                finally:
                    con.close()
        if dropped:
            ctx.notes.append(f"{dropped} lines with no ticker of their own (not served)")
        return list(dict.fromkeys(out))

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
        # The daily funds' share on their own latest day: a sweep ETF is no daily fund, a named trial no day of them.
        daily = f"etf IN ({', '.join(repr(t) for t in sorted(DAILY))})"
        funds = con.execute(f"SELECT count(DISTINCT etf) FROM {TABLE} WHERE {daily} AND as_of = "
                            f"(SELECT max(as_of) FROM {TABLE} WHERE {daily})").fetchone()[0]
        keys = ["as_of", "etf", "ticker"]
        added, removed = stage.compare_live(con, f"SELECT as_of, etf, ticker FROM {TABLE}", TABLE, keys)
        return {"dataset": NAME, "result": "changed" if added or removed else "unchanged",
                "source_as_of": (latest or ctx.as_of or self.default_as_of()).isoformat(),
                "rows": stage.count(con, TABLE, day) if latest else 0, "added": added, "removed": removed,
                "coverage": round(funds / len(FILES), 4)}


DATASET = EtfHoldings()
