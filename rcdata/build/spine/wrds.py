"""The three WRDS datasets: ``wrds/sec_idhist``, ``wrds/co_hgic`` and ``wrds/idxcst_his`` (Compustat, monthly).

**Not run against the real source.** Built against Compustat's documented tables (``comp.company``,
``comp.security``, ``comp.sec_idhist``, ``comp.co_hgic``, ``comp.idxcst_his``); the tests answer with recorded-shape
rows from a fake connection, never WRDS. The first real run is the attended homesrv backfill.

**Attended, one connection.** WRDS logs in with ``WRDS_USERNAME`` (the password from the user's ``~/.pgpass``) and a
Duo push the person confirms, so the three datasets are ``attended``: never due, and run only from the CLI
(``python -m RevenueChaser.rcdata.build backfill wrds/sec_idhist wrds/co_hgic wrds/idxcst_his``). The connection is
opened once per builder process, on the first query, and every dataset of the run uses it (``session``); a failed
login is a failed run, the last Parquet snapshot stays served, and nothing retries (the memory rule: ask avo). The
``wrds`` package is imported only then, so it is a server-only dependency (not in ``requirements.txt``).

**What is asked.** A full snapshot once a month: every row of the tables above (``comp.idxcst_his`` only for the
index keys in ``INDEX_GVKEYX``). Between snapshots, a run asks only the new names: the CIKs of the newest SEC file's
listed rows that the stored ``comp.company`` rows do not hold, and their securities and identifier history (one
query each, by CIK and then by gvkey). Every query counts as one ``wrds`` call.

Raw: ``raw/wrds/<dataset>/<as-of>/<table>.json.gz`` (a full snapshot) and ``.../<as-of>/new-<table>.json.gz`` (the
new-name rows), each with a ``.meta.json`` sidecar (the query, the row count, the run). A raw file that exists is not
asked again (resumable). Parquet: ``parquet/wrds/<dataset>/snapshot=<as-of>/<table>.parquet`` and
``.../delta=<as-of>/<table>.parquet``; the build reads the newest snapshot and every delta after it, a delta row
replacing the snapshot's row of its key.

Staging: ``stg_wrds_company``, ``stg_wrds_security``, ``stg_wrds_idhist``, ``stg_wrds_hgic``, ``stg_wrds_idx``.
"""
from __future__ import annotations

import os
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import duckdb

from ....core import calendar
from .. import base
from . import KIND, stage

PROVIDER = "wrds"
USER_ENV = "WRDS_USERNAME"
SNAPSHOT_DAYS = 30
META_SUFFIX = ".meta.json"
# Compustat's index keys (``comp.idxcst_his.gvkeyx``) of the S&P 500, MidCap 400 and SmallCap 600. Not verified
# against WRDS from here: the homesrv run checks them (an unknown key answers no rows, a build issue).
INDEX_GVKEYX = {"sp500": "000003", "sp400": "024248", "sp600": "030824"}
_DIGITS = re.compile(r"^\d{1,10}$")
_GVKEY = re.compile(r"^\d{6}$")

# table -> (dataset, schema, key, full query); the new-name queries are built in ``_new_name_queries``.
COMPANY = [("gvkey", "TEXT"), ("conm", "TEXT"), ("cik", "TEXT")]
SECURITY = [("gvkey", "TEXT"), ("iid", "TEXT"), ("tic", "TEXT"), ("cusip", "TEXT"), ("exchg", "INTEGER"),
            ("excntry", "TEXT"), ("secstat", "TEXT"), ("tpci", "TEXT"), ("dldtei", "DATE")]
IDHIST = [("gvkey", "TEXT"), ("iid", "TEXT"), ("item", "TEXT"), ("itemvalue", "TEXT"), ("efffrom", "DATE"),
          ("effthru", "DATE")]
HGIC = [("gvkey", "TEXT"), ("indfrom", "DATE"), ("indthru", "DATE"), ("gsector", "TEXT"), ("ggroup", "TEXT"),
        ("gind", "TEXT"), ("gsubind", "TEXT")]
IDX = [("gvkey", "TEXT"), ("iid", "TEXT"), ("gvkeyx", "TEXT"), ("valid_from", "DATE"), ("thru", "DATE")]
PROVENANCE = [("source_as_of", "DATE"), ("fetched_at", "DOUBLE"), ("run_id", "TEXT")]

TABLES: dict[str, dict] = {
    "company": {"dataset": "wrds/sec_idhist", "schema": COMPANY, "key": ["gvkey"],
                "sql": "SELECT gvkey, conm, cik FROM comp.company"},
    "security": {"dataset": "wrds/sec_idhist", "schema": SECURITY, "key": ["gvkey", "iid"],
                 "sql": "SELECT gvkey, iid, tic, cusip, exchg, excntry, secstat, tpci, dldtei FROM comp.security"},
    "sec_idhist": {"dataset": "wrds/sec_idhist", "schema": IDHIST, "key": ["gvkey", "iid", "item", "efffrom"],
                   "sql": "SELECT gvkey, iid, item, itemvalue, efffrom, effthru FROM comp.sec_idhist"},
    "co_hgic": {"dataset": "wrds/co_hgic", "schema": HGIC, "key": ["gvkey", "indfrom"],
                "sql": "SELECT gvkey, indfrom, indthru, gsector, ggroup, gind, gsubind FROM comp.co_hgic"},
    "idxcst_his": {"dataset": "wrds/idxcst_his", "schema": IDX, "key": ["gvkey", "iid", "gvkeyx", "valid_from"],
                   "sql": ('SELECT gvkey, iid, gvkeyx, "from" AS valid_from, thru FROM comp.idxcst_his '
                           "WHERE gvkeyx IN (" + ", ".join(f"'{k}'" for k in INDEX_GVKEYX.values()) + ")")},
}
STAGING = {"company": "stg_wrds_company", "security": "stg_wrds_security", "sec_idhist": "stg_wrds_idhist",
           "co_hgic": "stg_wrds_hgic", "idxcst_his": "stg_wrds_idx"}


# ── The one connection ────────────────────────────────────────────────────────────────────────────────────────

class LoginFailed(base.SourceError):
    """The WRDS login (or its Duo confirmation) did not succeed: nothing was asked; the last snapshot stays."""


def _connect() -> Any:
    """A real WRDS connection (``wrds.Connection``): the password from ``~/.pgpass``, then Duo. Server only."""
    user = (os.getenv(USER_ENV) or "").strip()
    if not user:
        raise LoginFailed(f"no WRDS user: set {USER_ENV} (the password goes in ~/.pgpass, never here)")
    try:
        import wrds  # noqa: PLC0415 - a server-only dependency, imported only by an attended run
    except ImportError as e:
        raise LoginFailed("the wrds package is not installed in this venv (pip install wrds, on the server)") from e
    try:
        return wrds.Connection(wrds_username=user)
    except Exception as e:  # noqa: BLE001 - any login failure is the same outcome: stop, keep the snapshot
        raise LoginFailed(f"WRDS login failed: {type(e).__name__}") from e


_factory: Callable[[], Any] = _connect          # the seam the tests replace
_session: Any = None


def session(ctx: base.RunContext) -> Any:
    """The process's one WRDS connection, opened on first use (one login, one Duo push per builder process)."""
    global _session
    if _session is None:
        ctx.check_cap(PROVIDER)
        _session = _factory()
    return _session


def reset() -> None:
    """Close and forget the connection (end of a process; the tests)."""
    global _session
    if _session is not None:
        try:
            _session.close()
        except Exception:  # noqa: BLE001
            pass
    _session = None


def _records(result) -> list[dict]:
    """A query result (a pandas DataFrame from ``wrds``, or a list of dicts from a fake) as JSON-ready dicts."""
    rows = result.to_dict("records") if hasattr(result, "to_dict") else list(result or [])
    out = []
    for r in rows:
        clean = {}
        for k, v in r.items():
            if v is None or (isinstance(v, float) and v != v):
                v = None
            elif isinstance(v, (datetime, date)):
                v = v.isoformat()[:10]
            elif hasattr(v, "isoformat"):                    # pandas Timestamp
                v = v.isoformat()[:10]
            elif hasattr(v, "item"):                         # numpy scalars
                v = v.item()
            clean[str(k).lower()] = v
        out.append(clean)
    return out


def query(ctx: base.RunContext, sql: str) -> list[dict]:
    """One query on the shared connection, counted as one ``wrds`` call; a failure is a ``SourceError``."""
    con = session(ctx)
    ctx.check_cap(PROVIDER)
    ctx.count(PROVIDER, calls=1)
    try:
        return _records(con.raw_sql(sql))
    except Exception as e:  # noqa: BLE001
        ctx.count(PROVIDER, failures=1)
        raise base.SourceError(f"wrds query failed: {type(e).__name__}: {str(e)[:200]}") from e


# ── Files ─────────────────────────────────────────────────────────────────────────────────────────────────────

def raw_dir(dataset: str) -> Path:
    return base.raw_root() / "wrds" / dataset.split("/", 1)[1]


def parquet_dir(dataset: str) -> Path:
    return base.parquet_root() / "wrds" / dataset.split("/", 1)[1]


def _meta(raw: Path) -> Path:
    return raw.with_name(raw.name + META_SUFFIX)


def _save(ctx: base.RunContext, raw: Path, sql: str, rows: list[dict], as_of: date) -> Path:
    base.write_raw_json(_meta(raw), {"as_of": as_of.isoformat(), "fetched_at": time.time(), "run_id": ctx.run_id,
                                     "query": sql, "rows": len(rows)})
    return base.write_raw_json(raw, rows)


def _snapshots(dataset: str) -> list[str]:
    """The as-of dates of the full snapshots in Parquet, oldest first."""
    folder = parquet_dir(dataset)
    if not folder.is_dir():
        return []
    return sorted(d.name.split("=", 1)[1] for d in folder.iterdir() if d.name.startswith("snapshot="))


def _parquet_files(dataset: str, table: str) -> list[Path]:
    """The newest snapshot's file of ``table`` and every delta file of it after that snapshot."""
    folder = parquet_dir(dataset)
    snaps = _snapshots(dataset)
    files = []
    if snaps and (folder / f"snapshot={snaps[-1]}" / f"{table}.parquet").is_file():
        files.append(folder / f"snapshot={snaps[-1]}" / f"{table}.parquet")
    floor = snaps[-1] if snaps else ""
    if folder.is_dir():
        for d in sorted(folder.iterdir()):
            if d.name.startswith("delta=") and d.name.split("=", 1)[1] >= floor and (d / f"{table}.parquet").is_file():
                files.append(d / f"{table}.parquet")
    return files


def _sec_ciks() -> list[str]:
    """The CIKs of the listed rows (Nasdaq, NYSE, Cboe) of the newest SEC-file snapshot in Parquet (no network):
    the names that need a Compustat link. An OTC name is never asked for."""
    from . import sec_tickers, tier
    parts = stage.partitions(sec_tickers.parquet_dir(), "date")
    if not parts:
        return []
    listed = ", ".join(f"'{e.upper()}'" for e in tier.LISTED)
    con = duckdb.connect()
    try:
        rows = con.execute(f"SELECT DISTINCT cik FROM read_parquet({base.sql_path(parts[-1][1])}) "
                           f"WHERE cik IS NOT NULL AND upper(exchange) IN ({listed})").fetchall()
    finally:
        con.close()
    return sorted(str(r[0]) for r in rows if _DIGITS.match(str(r[0])))


def _known(dataset: str, table: str, column: str) -> set[str]:
    files = _parquet_files(dataset, table)
    if not files:
        return set()
    con = duckdb.connect()
    try:
        paths = ", ".join(base.sql_path(f) for f in files)
        rows = con.execute(f'SELECT DISTINCT "{column}" FROM read_parquet([{paths}])').fetchall()
    finally:
        con.close()
    return {str(r[0]) for r in rows if r[0] is not None}


def norm_cik(value) -> str | None:
    """Compustat's ``cik`` (``0000789019``) as the SEC file writes it (``789019``)."""
    text = str(value or "").strip()
    return str(int(text)) if text.isdigit() else None


def _in_list(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


# ── The datasets ──────────────────────────────────────────────────────────────────────────────────────────────

class WrdsDataset:
    """One WRDS dataset: its tables' full snapshot once a month, and (``wrds/sec_idhist``) the new names between."""

    kind = KIND
    providers = (PROVIDER,)
    tables: tuple[str, ...] = ()
    cadence = "monthly"
    attended = True
    backfill_needs_from = False
    starts_by_hand = True             # not due until a first CLI run or Fetch built it (review should-fix 2)

    def __init__(self, name: str):
        self.name = name
        self.own = [t for t, spec in TABLES.items() if spec["dataset"] == name]

    def default_as_of(self) -> date:
        return calendar.today_ny()

    def _snapshot_due(self, as_of: date) -> bool:
        snaps = _snapshots(self.name)
        return not snaps or (as_of - date.fromisoformat(snaps[-1])) >= timedelta(days=SNAPSHOT_DAYS)

    def fetch(self, ctx: base.RunContext, as_of: date) -> list[Path]:
        folder = raw_dir(self.name) / as_of.isoformat()
        if self._snapshot_due(as_of) or any((folder / f"{t}.json.gz").exists() for t in self.own):
            return self._full(ctx, as_of)
        return self._new_names(ctx, as_of)

    def _full(self, ctx: base.RunContext, as_of: date) -> list[Path]:
        out = []
        for table in self.own:
            raw = raw_dir(self.name) / as_of.isoformat() / f"{table}.json.gz"
            if not raw.exists():
                _save(ctx, raw, TABLES[table]["sql"], query(ctx, TABLES[table]["sql"]), as_of)
            out.append(raw)
        return out

    def _new_names(self, ctx: base.RunContext, as_of: date) -> list[Path]:
        if self.name != "wrds/sec_idhist":
            return []
        known = {norm_cik(c) for c in _known(self.name, "company", "cik")}
        new = [c for c in _sec_ciks() if c not in known]
        folder = raw_dir(self.name) / as_of.isoformat()
        if not new:
            ctx.notes.append("no new names since the snapshot")
            return []
        padded = [c.zfill(10) for c in new]
        out = []
        raw = folder / "new-company.json.gz"
        if not raw.exists():
            sql = f"SELECT gvkey, conm, cik FROM comp.company WHERE cik IN ({_in_list(padded)})"
            _save(ctx, raw, sql, query(ctx, sql), as_of)
        out.append(raw)
        gvkeys = sorted({str(r["gvkey"]) for r in base.read_raw_json(raw) if _GVKEY.match(str(r.get("gvkey") or ""))})
        ctx.notes.append(f"new names: {len(new)} CIKs asked, {len(gvkeys)} found")
        if not gvkeys:
            return out
        for table in ("security", "sec_idhist"):
            raw = folder / f"new-{table}.json.gz"
            if not raw.exists():
                sql = f"{TABLES[table]['sql']} WHERE gvkey IN ({_in_list(gvkeys)})"
                _save(ctx, raw, sql, query(ctx, sql), as_of)
            out.append(raw)
        return out

    def backfill(self, ctx: base.RunContext, start: date | None, end: date) -> None:
        # WRDS answers the whole history in one snapshot: a backfill is a full snapshot as of the end day.
        self.to_parquet_all(ctx, self._full(ctx, end))

    def to_parquet(self, ctx: base.RunContext, raw: Path) -> list[Path]:
        return self.to_parquet_all(ctx, [raw])

    def to_parquet_all(self, ctx: base.RunContext, raws: list[Path]) -> list[Path]:
        out = []
        for raw in raws:
            meta = base.read_raw_json(_meta(raw))
            table = raw.name[: -len(".json.gz")]
            delta = table.startswith("new-")
            table = table[4:] if delta else table
            spec = TABLES[table]
            schema = spec["schema"] + PROVENANCE
            as_of = date.fromisoformat(meta["as_of"])
            rows = base.read_raw_json(raw)
            if not rows and not delta:
                raise base.SourceError(f"wrds {table}: the snapshot came back empty")
            for r in rows:
                r.update(source_as_of=as_of, fetched_at=meta["fetched_at"], run_id=meta["run_id"])
            folder = f"{'delta' if delta else 'snapshot'}={as_of.isoformat()}"
            final = parquet_dir(self.name) / folder / f"{table}.parquet"
            con = duckdb.connect()
            try:
                base.rows_table(con, "rows", schema, rows)
                keys = ", ".join(spec["key"])
                out.append(base.write_parquet(con, f"SELECT * FROM rows QUALIFY row_number() OVER (PARTITION BY {keys} "
                                                   f"ORDER BY fetched_at DESC) = 1 ORDER BY {keys}", final))
            finally:
                con.close()
        return out

    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        for table in self.own:
            spec = TABLES[table]
            schema = spec["schema"] + PROVENANCE
            keys = ", ".join(spec["key"])
            # The newest snapshot plus the deltas after it; a delta's row replaces the snapshot's (newest fetch wins).
            stage.create(con, STAGING[table], schema, _parquet_files(self.name, table),
                         select=f"SELECT * FROM {{src}} QUALIFY row_number() OVER (PARTITION BY {keys} "
                                f"ORDER BY fetched_at DESC) = 1")
        return {}

    def check(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict:
        rows = sum(stage.count(con, STAGING[t]) for t in self.own)
        as_of = max((con.execute(f"SELECT max(source_as_of) FROM {STAGING[t]}").fetchone()[0] for t in self.own),
                    default=None, key=lambda d: d or date.min)
        # The served rows this dataset feeds, in this build against the live file (spine/security built them).
        table, cols, where = {
            "wrds/co_hgic": ("gics_history", ["gvkey", "valid_from", "gsubind"], ""),
            "wrds/idxcst_his": ("index_membership", ["index_slug", "gvkey", "iid", "valid_from", "valid_to"],
                                "source = 'wrds/idxcst_his'"),
            "wrds/sec_idhist": ("security", ["ticker", "gvkey", "iid", "cusip"], "cusip_source = 'wrds/sec_idhist'"),
        }[self.name]
        names = ", ".join(cols)
        added, removed = stage.compare_live(con, f"SELECT {names} FROM {table}" + (f" WHERE {where}" if where else ""),
                                            table, cols, where=where)
        return {"dataset": self.name, "result": "changed" if added or removed else "unchanged",
                "source_as_of": (as_of or ctx.as_of or self.default_as_of()).isoformat(), "rows": rows,
                "added": added, "removed": removed}


SEC_IDHIST = WrdsDataset("wrds/sec_idhist")
CO_HGIC = WrdsDataset("wrds/co_hgic")
IDXCST_HIS = WrdsDataset("wrds/idxcst_his")
