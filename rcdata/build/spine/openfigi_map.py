"""``openfigi/map``: a FIGI for every CUSIP the spine holds, asking only the new ones (weekly).

``POST <RC_OPENFIGI_BASE>/v3/mapping`` with up to 100 jobs ``{"idType": "ID_CUSIP", "idValue": <cusip>, "exchCode":
"US"}`` (the US composite listing, as ``core/providers/openfigi.py`` asks; a CINS is asked as ``ID_CINS``, ``id_type``),
the key in the ``X-OPENFIGI-APIKEY`` header (``OPENFIGI_API_KEY``, through ``core.credentials``; never logged). Requests
take tokens of the box-wide OpenFIGI bucket the dashboard's mapping shares (``core/providers/openfigi.pace``: the
key's 250 a minute, ``caps.WINDOWS``), half the window when it cannot be used, at most ``MAX_REQUESTS`` a run. No
key: the run fails and asks nothing.

Which CUSIPs: the CUSIPs of the active Compustat securities (``secstat`` A) whose CIK is in the newest SEC file,
and the CUSIPs the fund files hold, less those already in the map: a 'present' answer is kept, an 'absent' one is
asked again after ``ABSENT_RETRY_DAYS``. A CUSIP whose job answered an ``error`` stays unasked and is asked on the next run.
An 'absent' answer counts only when its code was asked with its own id type (``_asked_right``): the map asked every code
as ``ID_CUSIP`` until 2026-10-28, and OpenFIGI found none of the 2,092 CINS asked that way, so those are asked again.

**By ticker** (the our-universe plan of 2026-11-04, item 1). A Tier A name Compustat does not link carries no CUSIP,
so no ``ID_CUSIP`` job can ever answer for it and the type rules have no OpenFIGI security type to read: the live
spine left 321 Tier A names untyped (homesrv, 4 Nov 2026). So the run also asks a ``TICKER`` job (the ticker in
OpenFIGI's spelling, ``core.identity.sources.openfigi.figi_symbol``) for every ``TICKER_TIERS`` row of the **live**
spine with no FIGI (``wanted_tickers``), whether or not it has a CUSIP: a row still without a FIGI either has no CUSIP
to ask with, or has one OpenFIGI's ``ID_CUSIP``/``ID_CINS`` jobs did not know, and a ``TICKER`` job may answer either
way. Measured on the 2026-11-02 live spine: 321 Tier A rows have no FIGI, 192 of them with no CUSIP and 129 with
one OpenFIGI did not answer for. That read is the previous build's file, so a name typed by a
ticker answer drops out of the ask on the next build; with no spine file yet nothing is asked. Both lanes share the
one ``MAX_REQUESTS`` budget and the one pacer, the CUSIPs first.

Raw: ``raw/openfigi/map/<as-of>/batch-<n>.json.gz`` (the CUSIPs asked, their id types and OpenFIGI's answer, one file
per request; a batch written before the id types were recorded asked ``ID_CUSIP``) with a ``.meta.json`` sidecar. A
run on the same day skips the codes the batch files already there asked with their own id type (resumable).
Parquet: ``parquet/openfigi/map/part-0.parquet``, merged by CUSIP, the newest answer winning. Staging: ``stg_figi``.
The ticker lane is its own raw folder, partition and staging table, keyed by the ticker asked:
``raw/openfigi/map_ticker/<as-of>/batch-<n>.json.gz`` (``tickers`` in place of ``cusips``),
``parquet/openfigi/map_ticker/part-0.parquet``, ``stg_figi_ticker``. The CUSIP map is untouched by it.
"""
from __future__ import annotations

import time
from datetime import date, timedelta
from pathlib import Path

import duckdb

from ....core import calendar, credentials
from ....core.identity.sources.openfigi import figi_symbol
from ....core.providers import openfigi as figi_client
from ... import caps, serving
from .. import base
from . import KIND, stage

NAME = "openfigi/map"
PROVIDER = "openfigi"
JOBS_PER_REQUEST = 100
MAX_REQUESTS = 400
ABSENT_RETRY_DAYS = 90
PRESENT, ABSENT = "present", "absent"
ID_CUSIP, ID_CINS, ID_TICKER = "ID_CUSIP", "ID_CINS", "TICKER"
# The tiers whose rows are asked by ticker: Tier A only, the names the boards screen. A Tier B, C or D row with no
# CUSIP is not worth a job (every OTC row and every excluded type would be one).
TICKER_TIERS = ("A",)
SCHEMA = [("cusip", "TEXT"), ("status", "TEXT"), ("ticker", "TEXT"), ("figi", "TEXT"), ("composite_figi", "TEXT"),
          ("share_class_figi", "TEXT"), ("exch_code", "TEXT"), ("name", "TEXT"), ("security_type", "TEXT"),
          ("security_type2", "TEXT"), ("market_sector", "TEXT"), ("security_description", "TEXT"),
          ("source_as_of", "DATE"), ("fetched_at", "DOUBLE"), ("run_id", "TEXT")]
# The ticker lane's rows: the same answer columns, keyed by the ticker asked instead of by a CUSIP.
TICKER_SCHEMA = [("asked_ticker", "TEXT")] + [col for col in SCHEMA if col[0] != "cusip"]
META_SUFFIX = ".meta.json"
# ``security.figi_source``: which job answered. The CUSIP lane keeps the plain dataset name it has always written.
FIGI_BY_CUSIP, FIGI_BY_TICKER = NAME, f"{NAME} ({ID_TICKER})"


class _KeyPacer:
    """A token of the key's box-wide bucket before each request (every attempt, ``base.http_post``); this run's half
    of the window when the bucket cannot be used."""

    def __init__(self, ctx: base.RunContext):
        limit, window = caps.WINDOWS[PROVIDER]
        self._fallback = base.Pacer(limit // 2, window, clock=ctx.clock, sleep=ctx.sleep)

    def wait(self) -> None:
        if not figi_client.pace():
            self._fallback.wait()


def key_pacer(ctx: base.RunContext) -> _KeyPacer:
    return _KeyPacer(ctx)


def raw_dir() -> Path:
    return base.raw_root() / "openfigi" / "map"


def partition() -> Path:
    return base.parquet_root() / "openfigi" / "map" / "part-0.parquet"


def ticker_raw_dir() -> Path:
    return base.raw_root() / "openfigi" / "map_ticker"


def ticker_partition() -> Path:
    return base.parquet_root() / "openfigi" / "map_ticker" / "part-0.parquet"


def _meta(raw: Path) -> Path:
    return raw.with_name(raw.name + META_SUFFIX)


def valid_cusip(c) -> bool:
    text = str(c or "").strip().upper()
    return len(text) == 9 and text.isalnum()


def id_type(code: str) -> str:
    """OpenFIGI's ``idType`` for a code: ``ID_CINS`` for a CINS (a CUSIP International Numbering System code, a letter
    first: ``G0450A105``; the letter is a country or region, ``C`` Canada and ``U`` a US issuer's offshore issue among
    them), else ``ID_CUSIP``. ``ID_CINS`` is OpenFIGI's own id type (its API documentation and
    ``/v3/mapping/values/idType``, read 2026-10-28); asked as ``ID_CUSIP``, OpenFIGI found none of the 2,092 CINS the
    first live map held (homesrv, 2026-10-28: "No identifier found" for every one)."""
    return ID_CINS if str(code or "")[:1].isalpha() else ID_CUSIP


def _query(sql: str) -> list[tuple]:
    con = duckdb.connect()
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def wanted_cusips() -> list[str]:
    """Every CUSIP the spine would map: Compustat's active ones for the SEC-file CIKs, the fund files', and the newest
    two quarters' 13F long-equity CUSIPs (the ownership store's fallback, PR 16). From Parquet only."""
    from . import funds, sec_tickers, wrds
    out: set[str] = set()
    sec = stage.partitions(sec_tickers.parquet_dir(), "date")
    company = wrds._parquet_files("wrds/sec_idhist", "company")
    security = wrds._parquet_files("wrds/sec_idhist", "security")
    if sec and company and security:
        ciks = {r[0] for r in _query(f"SELECT DISTINCT cik FROM read_parquet({base.sql_path(sec[-1][1])})")}
        rows = _query(f"SELECT c.cik, s.cusip FROM read_parquet([{', '.join(base.sql_path(f) for f in company)}]) c "
                      f"JOIN read_parquet([{', '.join(base.sql_path(f) for f in security)}]) s USING (gvkey) "
                      f"WHERE s.cusip IS NOT NULL AND s.secstat = 'A'")
        out |= {str(cu).upper() for cik, cu in rows if wrds.norm_cik(cik) in ciks}
    parts = stage.partitions(funds.parquet_dir(), "date")
    if parts:
        out |= {str(r[0]).upper() for r in _query(
            f"SELECT DISTINCT cusip FROM read_parquet({base.sql_path(parts[-1][1])}) WHERE cusip IS NOT NULL")}
    from ..ownership import sec13f              # imported here: the ownership package imports the spine's
    out |= sec13f.recent_cusips()
    return sorted(c for c in out if valid_cusip(c))


def wanted_tickers() -> list[str]:
    """The live spine's ``TICKER_TIERS`` tickers worth a ``TICKER`` job: the ones with no FIGI (no CUSIP to ask with,
    or one OpenFIGI did not know), **and** the ones whose FIGI came from this lane, so that answer can be checked
    again once it is older than ``ABSENT_RETRY_DAYS``. A ticker is not a stable key: a symbol can move to another
    issuer, and ``figi_ticker_differs`` cannot catch it because the asked and answered tickers are equal. Which of
    these is actually asked is ``_mapped_tickers``'s call — a ticker answered inside the window is not asked again,
    so a re-ask costs the lane's 4 requests once every 90 days, not one per build.

    From the served spine file (the previous build's), so nothing is asked before the first spine build. A spine file
    without these columns asks nothing."""
    try:
        con = serving.open_read(KIND)
    except serving.ServingFileMissing:
        return []
    try:
        tiers = ", ".join(f"'{t}'" for t in TICKER_TIERS)
        rows = con.execute(f"SELECT ticker FROM security WHERE tier IN ({tiers}) "
                           "AND (figi IS NULL OR figi_source = ?) ORDER BY ticker", [FIGI_BY_TICKER]).fetchall()
    except duckdb.Error:
        return []
    finally:
        con.close()
    return [str(r[0]) for r in rows if r[0]]


def _asked_right(batches) -> set[str]:
    """The codes the batch files ``batches`` asked with their own id type (``id_type``); a batch without ``id_types``
    was written before they were recorded and asked every code as ``ID_CUSIP``."""
    out: set[str] = set()
    for raw in batches:
        body = base.read_raw_json(raw)
        asked = body.get("id_types") or [ID_CUSIP] * len(body["cusips"])
        out |= {c for c, t in zip(body["cusips"], asked) if t == id_type(c)}
    return out


def _mapped(as_of: date) -> set[str]:
    """CUSIPs the map answers for: every 'present' one, and an 'absent' one answered inside the retry window when its
    code was asked with its own id type (a CINS the map asked as ``ID_CUSIP`` is asked again)."""
    if not partition().exists():
        return set()
    floor = as_of - timedelta(days=ABSENT_RETRY_DAYS)
    rows = _query(f"SELECT cusip, status, source_as_of FROM read_parquet({base.sql_path(partition())})")
    recent = {c for c, st, on in rows if st != PRESENT and on is not None and on > floor}
    cins = {c for c in recent if id_type(c) == ID_CINS}
    right = _asked_right(raw_dir().glob("*/batch-*.json.gz")) if cins else set()
    return {c for c, st, _on in rows if st == PRESENT} | (recent - cins) | (cins & right)


def _asked_today(as_of: date) -> set[str]:
    folder = raw_dir() / as_of.isoformat()
    if not folder.is_dir():
        return set()
    return _asked_right(folder.glob("batch-*.json.gz"))


def _mapped_tickers(as_of: date) -> set[str]:
    """Tickers the ticker map has a current answer for: any answer, 'present' or 'absent', inside the retry window.
    Unlike a CUSIP, a 'present' ticker answer expires too (``wanted_tickers``): the symbol may belong to another
    issuer by then, and ``upsert_partition`` replaces the row by ``asked_ticker``, so the newer answer wins."""
    if not ticker_partition().exists():
        return set()
    floor = as_of - timedelta(days=ABSENT_RETRY_DAYS)
    rows = _query(f"SELECT asked_ticker, status, source_as_of FROM read_parquet({base.sql_path(ticker_partition())})")
    return {t for t, _st, on in rows if on is not None and on > floor}


def _asked_tickers_today(as_of: date) -> set[str]:
    folder = ticker_raw_dir() / as_of.isoformat()
    if not folder.is_dir():
        return set()
    out: set[str] = set()
    for raw in folder.glob("batch-*.json.gz"):
        out |= {str(t) for t in base.read_raw_json(raw).get("tickers") or []}
    return out


def _present(hit: dict) -> dict:
    """The answer columns of one 'present' mapping entry (the key column is the caller's)."""
    return {"status": PRESENT, "ticker": figi_client.app_ticker(hit.get("ticker")), "figi": hit.get("figi"),
            "composite_figi": hit.get("compositeFIGI"), "share_class_figi": hit.get("shareClassFIGI"),
            "exch_code": hit.get("exchCode"), "name": hit.get("name"),
            "security_type": hit.get("securityType"), "security_type2": hit.get("securityType2"),
            "market_sector": hit.get("marketSector"), "security_description": hit.get("securityDescription")}


def answer(cusip: str, entry: dict) -> dict | None:
    """One job's answer as a map row; ``None`` for a job-level error (asked again next run)."""
    data = (entry or {}).get("data")
    if data:
        hit = next((d for d in data if (d or {}).get("ticker")), data[0]) or {}
        return {"cusip": cusip, **_present(hit)}
    if (entry or {}).get("error"):
        return None
    return {"cusip": cusip, "status": ABSENT}


def ticker_answer(ticker: str, entry: dict) -> dict | None:
    """One ``TICKER`` job's answer as a ticker-map row; ``None`` for a job-level error (asked again next run). The
    entry chosen is the US listing whose ticker is the one asked, as the identity adapter's ``_pick`` does, else the
    first with a ticker: a TICKER job can answer with several listings of the same symbol."""
    data = (entry or {}).get("data")
    if data:
        want = figi_symbol(ticker)
        hit = next((d for d in data if (d or {}).get("ticker") == want and (d or {}).get("exchCode") == "US"), None)
        hit = hit or next((d for d in data if (d or {}).get("ticker")), data[0]) or {}
        return {"asked_ticker": ticker, **_present(hit)}
    if (entry or {}).get("error"):
        return None
    return {"asked_ticker": ticker, "status": ABSENT}


class OpenFigiMap:
    name = NAME
    kind = KIND
    providers = (PROVIDER,)
    tables: tuple[str, ...] = ()
    cadence = "weekly"
    backfill_needs_from = False
    starts_by_hand = True             # not due until a first CLI run or Fetch built it (review should-fix 2)

    def default_as_of(self) -> date:
        return calendar.today_ny()

    @staticmethod
    def _ask(ctx: base.RunContext, as_of: date, todo: list[str], folder: Path, key: str, pacer, budget: int,
             job_of, body_of, what: str) -> int:
        """``todo`` in requests of ``JOBS_PER_REQUEST``, at most ``budget`` of them; the requests sent. ``job_of``
        makes one job from one item and ``body_of`` the raw file's own keys for a chunk."""
        n = len(list(folder.glob("batch-*.json.gz"))) if folder.is_dir() else 0
        url = figi_client.base_url() + figi_client.ENDPOINT
        headers = {"Content-Type": "application/json", "X-OPENFIGI-APIKEY": key}
        sent = 0
        for i in range(0, len(todo), JOBS_PER_REQUEST):
            if sent >= budget:
                ctx.notes.append(f"stopped at {budget} requests; {len(todo) - i} {what} left for the next run")
                break
            chunk = todo[i:i + JOBS_PER_REQUEST]
            jobs = [job_of(v) for v in chunk]
            r = base.http_post(ctx, PROVIDER, url, json=jobs, headers=headers, pacer=pacer)
            sent += 1
            try:
                body = r.json()
            except ValueError as e:
                raise base.SourceError("openfigi: the answer is not JSON") from e
            if not isinstance(body, list) or len(body) != len(chunk):
                raise base.SourceError("openfigi: the answer does not have one entry per job")
            raw = folder / f"batch-{n:05d}.json.gz"
            n += 1
            base.write_raw_json(_meta(raw), {"as_of": as_of.isoformat(), "fetched_at": time.time(),
                                             "run_id": ctx.run_id, "url": url, "jobs": len(chunk)})
            base.write_raw_json(raw, {**body_of(chunk), "answers": body})
        return sent

    def fetch(self, ctx: base.RunContext, as_of: date) -> list[Path]:
        key = (credentials.key_for(PROVIDER) or "").strip()
        if not key:
            raise base.SourceError("no OpenFIGI key: set OPENFIGI_API_KEY (core/credentials.py)")
        pacer = key_pacer(ctx)                      # one bucket for both lanes, so the key's window is kept once
        done = _mapped(as_of) | _asked_today(as_of)
        todo = [c for c in wanted_cusips() if c not in done]
        folder = raw_dir() / as_of.isoformat()
        sent = self._ask(ctx, as_of, todo, folder, key, pacer, MAX_REQUESTS,
                         lambda c: {"idType": id_type(c), "idValue": c, "exchCode": "US"},
                         lambda chunk: {"cusips": chunk, "id_types": [id_type(c) for c in chunk]}, "CUSIPs")
        ctx.notes.append(f"{len(todo)} CUSIPs to map, {sent} requests")
        out = sorted(folder.glob("batch-*.json.gz")) if folder.is_dir() else []

        t_done = _mapped_tickers(as_of) | _asked_tickers_today(as_of)
        t_todo = [t for t in wanted_tickers() if t not in t_done]
        t_folder = ticker_raw_dir() / as_of.isoformat()
        t_sent = self._ask(ctx, as_of, t_todo, t_folder, key, pacer, MAX_REQUESTS - sent,
                           lambda t: {"idType": ID_TICKER, "idValue": figi_symbol(t), "exchCode": "US"},
                           lambda chunk: {"tickers": chunk}, "tickers")
        ctx.notes.append(f"{len(t_todo)} tickers to map, {t_sent} requests")
        return out + (sorted(t_folder.glob("batch-*.json.gz")) if t_folder.is_dir() else [])

    def backfill(self, ctx: base.RunContext, start: date | None, end: date) -> None:
        # OpenFIGI answers today's mapping only: a backfill is a fetch of every CUSIP not mapped yet.
        self.to_parquet_all(ctx, self.fetch(ctx, end))

    def to_parquet(self, ctx: base.RunContext, raw: Path) -> list[Path]:
        return self.to_parquet_all(ctx, [raw])

    def to_parquet_all(self, ctx: base.RunContext, raws: list[Path]) -> list[Path]:
        rows: list[dict] = []
        ticker_rows: list[dict] = []
        errors = 0
        for raw in raws:
            meta = base.read_raw_json(_meta(raw))
            body = base.read_raw_json(raw)
            prov = {"source_as_of": date.fromisoformat(meta["as_of"]), "fetched_at": meta["fetched_at"],
                    "run_id": meta["run_id"]}
            # A ticker lane's batch names its items ``tickers``; a CUSIP lane's ``cusips``.
            asked, make, into = ((body["tickers"], ticker_answer, ticker_rows) if "tickers" in body
                                 else (body["cusips"], answer, rows))
            for item, entry in zip(asked, body["answers"]):
                row = make(item, entry)
                if row is None:
                    errors += 1
                    continue
                into.append({**row, **prov})
        if errors:
            ctx.notes.append(f"{errors} OpenFIGI job errors (asked again next run)")
        out = []
        if rows:
            out.append(base.upsert_partition(rows, SCHEMA, ["cusip"], partition()))
        if ticker_rows:
            out.append(base.upsert_partition(ticker_rows, TICKER_SCHEMA, ["asked_ticker"], ticker_partition()))
        return out

    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        stage.create(con, "stg_figi", SCHEMA, [partition()] if partition().exists() else [])
        stage.create(con, "stg_figi_ticker", TICKER_SCHEMA,
                     [ticker_partition()] if ticker_partition().exists() else [])
        return {}

    def check(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict:
        # Both lanes are this dataset's map, so the numbers count both tables.
        tables = ("stg_figi", "stg_figi_ticker")
        rows = sum(stage.count(con, t) for t in tables)
        present = sum(stage.count(con, t, f"status = '{PRESENT}'") for t in tables)
        # The map only grows or replaces an answer: what changed is what this run answered.
        added, removed = sum(stage.count(con, t, f"run_id = '{ctx.run_id}'") for t in tables), 0
        as_of = con.execute("SELECT max(source_as_of) FROM ("
                            "SELECT source_as_of FROM stg_figi UNION ALL "
                            "SELECT source_as_of FROM stg_figi_ticker)").fetchone()[0]
        return {"dataset": NAME, "result": "changed" if added or removed else "unchanged",
                "source_as_of": (as_of or ctx.as_of or self.default_as_of()).isoformat(), "rows": rows,
                "added": added, "removed": removed, "coverage": round(present / rows, 4) if rows else None}


DATASET = OpenFigiMap()
