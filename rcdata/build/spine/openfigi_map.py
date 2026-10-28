"""``openfigi/map``: a FIGI for every CUSIP the spine holds, asking only the new ones (weekly).

``POST <RC_OPENFIGI_BASE>/v3/mapping`` with up to 100 jobs ``{"idType": "ID_CUSIP", "idValue": <cusip>,
"exchCode": "US"}`` (the US composite listing, as ``core/providers/openfigi.py`` asks), the key in the
``X-OPENFIGI-APIKEY`` header (``OPENFIGI_API_KEY``, through ``core.credentials``; never logged). Requests are paced
under 25 a minute (the plan's rule, well inside OpenFIGI's keyed limit), at most ``MAX_REQUESTS`` a run. No key: the
run fails and asks nothing.

Which CUSIPs: the CUSIPs of the active Compustat securities (``secstat`` A) whose CIK is in the newest SEC file,
and the CUSIPs the fund files hold, less those already in the map: a 'present' answer is kept, an 'absent' one is
asked again after ``ABSENT_RETRY_DAYS``. A CUSIP whose job answered an ``error`` stays unasked and is asked on the next run.

Raw: ``raw/openfigi/map/<as-of>/batch-<n>.json.gz`` (the CUSIPs asked and OpenFIGI's answer, one file per request)
with a ``.meta.json`` sidecar. A run on the same day skips the CUSIPs of the batch files already there (resumable).
Parquet: ``parquet/openfigi/map/part-0.parquet``, merged by CUSIP, the newest answer winning. Staging: ``stg_figi``.
"""
from __future__ import annotations

import time
from datetime import date, timedelta
from pathlib import Path

import duckdb

from ....core import calendar, credentials
from ....core.providers import openfigi as figi_client
from .. import base
from . import KIND, stage

NAME = "openfigi/map"
PROVIDER = "openfigi"
PER_MINUTE = 24
JOBS_PER_REQUEST = 100
MAX_REQUESTS = 400
ABSENT_RETRY_DAYS = 90
PRESENT, ABSENT = "present", "absent"
SCHEMA = [("cusip", "TEXT"), ("status", "TEXT"), ("ticker", "TEXT"), ("figi", "TEXT"), ("composite_figi", "TEXT"),
          ("share_class_figi", "TEXT"), ("exch_code", "TEXT"), ("name", "TEXT"), ("security_type", "TEXT"),
          ("security_type2", "TEXT"), ("market_sector", "TEXT"), ("security_description", "TEXT"),
          ("source_as_of", "DATE"), ("fetched_at", "DOUBLE"), ("run_id", "TEXT")]
META_SUFFIX = ".meta.json"


def raw_dir() -> Path:
    return base.raw_root() / "openfigi" / "map"


def partition() -> Path:
    return base.parquet_root() / "openfigi" / "map" / "part-0.parquet"


def _meta(raw: Path) -> Path:
    return raw.with_name(raw.name + META_SUFFIX)


def valid_cusip(c) -> bool:
    text = str(c or "").strip().upper()
    return len(text) == 9 and text.isalnum()


def _query(sql: str) -> list[tuple]:
    con = duckdb.connect()
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def wanted_cusips() -> list[str]:
    """Every CUSIP the spine would map: Compustat's active ones for the SEC-file CIKs, and the fund files'. From Parquet
    only."""
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
    return sorted(c for c in out if valid_cusip(c))


def _mapped(as_of: date) -> set[str]:
    """CUSIPs the map answers for: every 'present' one, and an 'absent' one answered inside the retry window."""
    if not partition().exists():
        return set()
    floor = as_of - timedelta(days=ABSENT_RETRY_DAYS)
    rows = _query(f"SELECT cusip, status, source_as_of FROM read_parquet({base.sql_path(partition())})")
    return {c for c, st, on in rows if st == PRESENT or (on is not None and on > floor)}


def _asked_today(as_of: date) -> set[str]:
    folder = raw_dir() / as_of.isoformat()
    if not folder.is_dir():
        return set()
    return {c for raw in folder.glob("batch-*.json.gz") for c in base.read_raw_json(raw)["cusips"]}


def answer(cusip: str, entry: dict) -> dict | None:
    """One job's answer as a map row; ``None`` for a job-level error (asked again next run)."""
    data = (entry or {}).get("data")
    if data:
        hit = next((d for d in data if (d or {}).get("ticker")), data[0]) or {}
        return {"cusip": cusip, "status": PRESENT, "ticker": figi_client.app_ticker(hit.get("ticker")),
                "figi": hit.get("figi"), "composite_figi": hit.get("compositeFIGI"),
                "share_class_figi": hit.get("shareClassFIGI"), "exch_code": hit.get("exchCode"),
                "name": hit.get("name"), "security_type": hit.get("securityType"),
                "security_type2": hit.get("securityType2"), "market_sector": hit.get("marketSector"),
                "security_description": hit.get("securityDescription")}
    if (entry or {}).get("error"):
        return None
    return {"cusip": cusip, "status": ABSENT}


class OpenFigiMap:
    name = NAME
    kind = KIND
    providers = (PROVIDER,)
    tables: tuple[str, ...] = ()
    cadence = "weekly"
    backfill_needs_from = False

    def default_as_of(self) -> date:
        return calendar.today_ny()

    def fetch(self, ctx: base.RunContext, as_of: date) -> list[Path]:
        key = (credentials.key_for(PROVIDER) or "").strip()
        if not key:
            raise base.SourceError("no OpenFIGI key: set OPENFIGI_API_KEY (core/credentials.py)")
        done = _mapped(as_of) | _asked_today(as_of)
        todo = [c for c in wanted_cusips() if c not in done]
        folder = raw_dir() / as_of.isoformat()
        n = len(list(folder.glob("batch-*.json.gz"))) if folder.is_dir() else 0
        pacer = base.Pacer(PER_MINUTE, clock=ctx.clock, sleep=ctx.sleep)
        url = figi_client.base_url() + figi_client.ENDPOINT
        headers = {"Content-Type": "application/json", "X-OPENFIGI-APIKEY": key}
        sent = 0
        for i in range(0, len(todo), JOBS_PER_REQUEST):
            if sent >= MAX_REQUESTS:
                ctx.notes.append(f"stopped at {MAX_REQUESTS} requests; {len(todo) - i} CUSIPs left for the next run")
                break
            chunk = todo[i:i + JOBS_PER_REQUEST]
            jobs = [{"idType": "ID_CUSIP", "idValue": c, "exchCode": "US"} for c in chunk]
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
            base.write_raw_json(raw, {"cusips": chunk, "answers": body})
        ctx.notes.append(f"{len(todo)} CUSIPs to map, {sent} requests")
        return sorted(folder.glob("batch-*.json.gz")) if folder.is_dir() else []

    def backfill(self, ctx: base.RunContext, start: date | None, end: date) -> None:
        # OpenFIGI answers today's mapping only: a backfill is a fetch of every CUSIP not mapped yet.
        self.to_parquet_all(ctx, self.fetch(ctx, end))

    def to_parquet(self, ctx: base.RunContext, raw: Path) -> list[Path]:
        return self.to_parquet_all(ctx, [raw])

    def to_parquet_all(self, ctx: base.RunContext, raws: list[Path]) -> list[Path]:
        rows, errors = [], 0
        for raw in raws:
            meta = base.read_raw_json(_meta(raw))
            body = base.read_raw_json(raw)
            for cusip, entry in zip(body["cusips"], body["answers"]):
                row = answer(cusip, entry)
                if row is None:
                    errors += 1
                    continue
                rows.append({**row, "source_as_of": date.fromisoformat(meta["as_of"]),
                             "fetched_at": meta["fetched_at"], "run_id": meta["run_id"]})
        if errors:
            ctx.notes.append(f"{errors} OpenFIGI job errors (asked again next run)")
        if not rows:
            return []
        return [base.upsert_partition(rows, SCHEMA, ["cusip"], partition())]

    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        stage.create(con, "stg_figi", SCHEMA, [partition()] if partition().exists() else [])
        return {}

    def check(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict:
        rows = stage.count(con, "stg_figi")
        present = stage.count(con, "stg_figi", f"status = '{PRESENT}'")
        # The map only grows or replaces an answer: what changed is what this run answered.
        added, removed = stage.count(con, "stg_figi", f"run_id = '{ctx.run_id}'"), 0
        as_of = con.execute("SELECT max(source_as_of) FROM stg_figi").fetchone()[0]
        return {"dataset": NAME, "result": "changed" if added or removed else "unchanged",
                "source_as_of": (as_of or ctx.as_of or self.default_as_of()).isoformat(), "rows": rows,
                "added": added, "removed": removed, "coverage": round(present / rows, 4) if rows else None}


DATASET = OpenFigiMap()
