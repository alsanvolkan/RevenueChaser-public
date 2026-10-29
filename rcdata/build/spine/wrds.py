"""The three WRDS datasets: ``wrds/sec_idhist``, ``wrds/co_hgic`` and ``wrds/idxcst_his`` (Compustat, monthly).

**Not run against the real source.** Built against Compustat's documented tables (``comp.company``,
``comp.security``, ``comp.sec_idhist``, ``comp.co_hgic``, ``comp.idxcst_his``); the tests answer with recorded-shape
rows from a fake worker, and run the real worker against a stub ``wrds`` package, never WRDS. The first real run is
the attended homesrv backfill.

**Attended, one connection, in its own interpreter.** ``wrds`` is not in the builder's venv: on the server it lives in a venv
of its own (its pandas has no wheel for the builder's Python 3.14). So the queries run in ``wrds_worker.py`` under
``RC_WRDS_PYTHON`` (that venv's python; no default, it is set in the server's env file), one subprocess per builder
process, started
on the first query (``session``): one connection, at most one Duo push, for every WRDS dataset of one CLI call. The
login is that venv's ``wrds.Connection`` (the user from ``WRDS_USERNAME``, required; the password from that user's
``~/.pgpass``). The three datasets are ``attended``: never due, and run only from the CLI (``python -m
RevenueChaser.rcdata.build backfill wrds/sec_idhist wrds/co_hgic wrds/idxcst_his``, in a foreground terminal: a
background job that reads ``/dev/tty`` for a prompt is stopped by SIGTTIN). ``WRDS_USERNAME`` is required:
it picks the line of the user's ``~/.pgpass``. A failed login, a worker that cannot start (no interpreter at
``RC_WRDS_PYTHON``), or a worker that answers out of protocol is a failed run: the last Parquet snapshot stays served
and nothing retries (the memory rule: ask avo). The worker asks ``wrds`` for chunks and writes each answer as it
comes, and the builder types the raw file in DuckDB; whether the installed ``wrds`` really streams (rather than
reading the whole answer before the first chunk) is not verified, so watch ``free -h`` during the first run.

**What is asked.** A full snapshot once a month: every row of the tables above (``comp.idxcst_his`` only for the
index keys in ``INDEX_GVKEYX``). Between snapshots, a run asks only the new names: the CIKs of the newest SEC file's
listed rows that the stored ``comp.company`` rows do not hold, and their securities and identifier history (one
query each, by CIK and then by gvkey). Every query counts as one ``wrds`` call.

Raw: ``raw/wrds/<dataset>/<as-of>/<table>.jsonl.gz`` (a full snapshot) and ``.../<as-of>/new-<table>.jsonl.gz`` (the
new-name rows), each with a ``.meta.json`` sidecar (the query, the row count, the run). A raw file that exists is not
asked again (resumable). Parquet: ``parquet/wrds/<dataset>/snapshot=<as-of>/<table>.parquet`` and
``.../delta=<as-of>/<table>.parquet``; the build reads the newest snapshot and every delta after it, a delta row
replacing the snapshot's row of its key.

Staging: ``stg_wrds_company``, ``stg_wrds_security``, ``stg_wrds_idhist``, ``stg_wrds_hgic``, ``stg_wrds_idx``.
"""
from __future__ import annotations

import atexit
import json
import os
import re
import time
from datetime import date, timedelta
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
# against WRDS from here: a key with no rows in a run that answered rows for another is an ``index_key_empty`` build
# issue (spine/security), and no rows at all is a failed run (an empty snapshot).
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


# ── The one connection: a worker subprocess under the interpreter that has ``wrds`` ──────────────────────────

class LoginFailed(base.SourceError):
    """The WRDS login (or its Duo confirmation) did not succeed, or the worker could not start: nothing was asked;
    the last snapshot stays."""


PYTHON_ENV = "RC_WRDS_PYTHON"          # the python of the venv that has wrds; no default (a server path)
WORKER = Path(__file__).with_name("wrds_worker.py")


class Worker:
    """The ``wrds_worker.py`` subprocess (``<RC_WRDS_PYTHON> -P wrds_worker.py``): one per builder process, so one
    WRDS connection and at most one Duo push. Its stderr is the builder's, so WRDS's own messages reach the person
    at the terminal. ``query`` sends one request and waits for its one answer line."""

    def __init__(self, python: str | None = None):
        import subprocess
        python = python or (os.getenv(PYTHON_ENV) or "").strip()
        if not python:
            raise LoginFailed(f"no WRDS interpreter: set {PYTHON_ENV} to the python of the venv that has wrds")
        if not Path(python).exists():
            raise LoginFailed(f"no WRDS interpreter at {python}: set {PYTHON_ENV} to the python of the venv that "
                              "has wrds")
        if not (os.getenv(USER_ENV) or "").strip():
            raise LoginFailed(f"no WRDS user: set {USER_ENV} (it picks the ~/.pgpass line; the password stays there)")
        self.dead = False
        try:
            self.proc = subprocess.Popen([python, "-P", str(WORKER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         text=True, encoding="utf-8", env=worker_env())
        except OSError as e:
            raise LoginFailed(f"the WRDS worker did not start: {type(e).__name__}: {e}") from e

    def _kill(self, why: str) -> LoginFailed:
        self.dead = True
        try:
            self.proc.kill()
        except Exception:  # noqa: BLE001
            pass
        return LoginFailed(why)

    def query(self, sql: str, out: Path) -> int:
        if self.dead:
            raise LoginFailed("the WRDS worker was stopped after an answer out of protocol")
        try:
            self.proc.stdin.write(json.dumps({"sql": sql, "out": str(out)}) + "\n")
            self.proc.stdin.flush()
            line = self.proc.stdout.readline()
        except (BrokenPipeError, OSError) as e:
            raise LoginFailed(f"the WRDS worker is gone: {type(e).__name__}") from e
        if not line:
            raise LoginFailed(f"the WRDS worker ended (exit {self.proc.poll()}) without an answer")
        try:
            answer = json.loads(line)
            if not isinstance(answer, dict):
                raise ValueError("not an object")
        except ValueError:
            # Never read past a line that is not an answer: the next one could belong to another query.
            raise self._kill(f"the WRDS worker answered out of protocol ({line.strip()[:60]!r}); it was stopped")
        if answer.get("ok"):
            return int(answer["rows"])
        if answer.get("login"):
            raise LoginFailed(f"WRDS login failed: {answer.get('error')}")
        raise base.SourceError(f"wrds query failed: {answer.get('error')}")

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except OSError:                                  # the worker is already gone: nothing to flush
            pass
        try:
            self.proc.wait(timeout=60)
        except Exception:  # noqa: BLE001 - a worker that will not stop is killed
            self.proc.kill()
            self.proc.wait()


# What the worker's environment keeps: the process basics, the TLS certificate and library paths, the WRDS user,
# PostgreSQL's own variables, the worker's own RC_WRDS_* switches and PYTHONPATH. No other key of the builder
# (OpenFIGI, DeepSeek, ...) reaches a third-party venv.
_WORKER_ENV = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "TZ", "TMPDIR", "PYTHONPATH", "SSL_CERT_FILE", "SSL_CERT_DIR",
               "LD_LIBRARY_PATH", USER_ENV)


def worker_env() -> dict[str, str]:
    """The worker's environment. Server only (Linux): a Windows worker would also need SYSTEMROOT, APPDATA and
    USERPROFILE, which are not kept."""
    return {k: v for k, v in os.environ.items()
            if k in _WORKER_ENV or k.startswith(("PG", "LC_", "RC_WRDS_"))}


_factory: Callable[[], Any] = Worker            # the seam the tests replace
_session: Any = None


def session(ctx: base.RunContext) -> Any:
    """The process's one WRDS worker, started on the first query (one login, one Duo push per builder process)."""
    global _session
    if _session is None:
        ctx.check_cap(PROVIDER)
        _session = _factory()
    return _session


def reset() -> None:
    """Stop and forget the worker (end of a process; the tests)."""
    global _session
    if _session is not None:
        try:
            _session.close()
        except Exception:  # noqa: BLE001
            pass
    _session = None


atexit.register(reset)


def query(ctx: base.RunContext, sql: str, out: Path) -> int:
    """One query on the shared worker, its answer streamed to ``out`` (gzip JSON lines); counted as one ``wrds``
    call. A failed query is a ``SourceError``, a failed login a ``LoginFailed``."""
    worker = session(ctx)
    ctx.check_cap(PROVIDER)
    ctx.count(PROVIDER, calls=1)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        return worker.query(sql, out)
    except base.SourceError:
        ctx.count(PROVIDER, failures=1)
        raise


# ── Files ─────────────────────────────────────────────────────────────────────────────────────────────────────

def raw_dir(dataset: str) -> Path:
    return base.raw_root() / "wrds" / dataset.split("/", 1)[1]


def parquet_dir(dataset: str) -> Path:
    return base.parquet_root() / "wrds" / dataset.split("/", 1)[1]


def _meta(raw: Path) -> Path:
    return raw.with_name(raw.name + META_SUFFIX)


RAW_SUFFIX = ".jsonl.gz"


def _ask(ctx: base.RunContext, raw: Path, sql: str, as_of: date) -> Path:
    """Ask ``sql`` into the raw file ``raw`` (the worker writes it whole or not at all); its sidecar says what was
    asked and how many rows came back. A raw file that exists is the resume marker: it is not asked again."""
    if raw.exists():
        return raw
    meta = {"as_of": as_of.isoformat(), "fetched_at": time.time(), "run_id": ctx.run_id, "query": sql, "rows": None}
    base.write_raw_json(_meta(raw), meta)
    meta["rows"] = query(ctx, sql, raw)
    base.write_raw_json(_meta(raw), meta)
    return raw


def _typed(c: str, t: str) -> str:
    """The SQL that types the raw text column ``c`` to ``t`` (a value it cannot type becomes null)."""
    if t == "DATE":
        return f'TRY_CAST("{c}" AS DATE)'
    if t == "INTEGER":
        return f'TRY_CAST(TRY_CAST("{c}" AS DOUBLE) AS INTEGER)'
    if t == "DOUBLE":
        return f'TRY_CAST("{c}" AS DOUBLE)'
    return f'"{c}"'


def _read_raw(raw: Path, schema: list[tuple[str, str]]) -> str:
    cols = "{" + ", ".join(f"'{c}': 'VARCHAR'" for c, _ in schema) + "}"
    return f"read_json({base.sql_path(raw)}, format = 'newline_delimited', columns = {cols})"


def raw_rows(raw: Path, schema: list[tuple[str, str]]) -> str:
    """A SELECT of the raw file's rows typed to ``schema`` (every value arrives as text or null)."""
    typed = ", ".join(f'{_typed(c, t)} AS "{c}"' for c, t in schema)
    return f"SELECT {typed} FROM {_read_raw(raw, schema)}"


def untyped(con: duckdb.DuckDBPyConnection, raw: Path, schema: list[tuple[str, str]]) -> dict[str, int]:
    """Per typed column, the rows whose raw value is not null but did not type (``raw_rows`` made it null)."""
    cols = [(c, t) for c, t in schema if t in ("DATE", "INTEGER", "DOUBLE")]
    if not cols:
        return {}
    counts = ", ".join(f'count(*) FILTER (WHERE "{c}" IS NOT NULL AND {_typed(c, t)} IS NULL)' for c, t in cols)
    row = con.execute(f"SELECT {counts} FROM {_read_raw(raw, schema)}").fetchone()
    return {c: int(n) for (c, _), n in zip(cols, row) if n}


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


def parquet_files(dataset: str, table: str) -> list[Path]:
    """``_parquet_files`` for a reader outside the spine package (the ownership store's ``ownership/serve``)."""
    return _parquet_files(dataset, table)


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
        if self._snapshot_due(as_of) or any((folder / f"{t}{RAW_SUFFIX}").exists() for t in self.own):
            return self._full(ctx, as_of)
        return self._new_names(ctx, as_of)

    def _full(self, ctx: base.RunContext, as_of: date) -> list[Path]:
        out = []
        for table in self.own:
            out.append(_ask(ctx, raw_dir(self.name) / as_of.isoformat() / f"{table}{RAW_SUFFIX}",
                            TABLES[table]["sql"], as_of))
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
        raw = _ask(ctx, folder / f"new-company{RAW_SUFFIX}",
                   f"SELECT gvkey, conm, cik FROM comp.company WHERE cik IN ({_in_list(padded)})", as_of)
        out.append(raw)
        con = duckdb.connect()
        try:
            found = con.execute(f"SELECT DISTINCT gvkey FROM ({raw_rows(raw, COMPANY)})").fetchall()
        finally:
            con.close()
        gvkeys = sorted({str(r[0]) for r in found if _GVKEY.match(str(r[0] or ""))})
        ctx.notes.append(f"new names: {len(new)} CIKs asked, {len(gvkeys)} found")
        if not gvkeys:
            return out
        for table in ("security", "sec_idhist"):
            out.append(_ask(ctx, folder / f"new-{table}{RAW_SUFFIX}",
                            f"{TABLES[table]['sql']} WHERE gvkey IN ({_in_list(gvkeys)})", as_of))
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
            table = raw.name[: -len(RAW_SUFFIX)]
            delta = table.startswith("new-")
            table = table[4:] if delta else table
            spec = TABLES[table]
            as_of = date.fromisoformat(meta["as_of"])
            folder = f"{'delta' if delta else 'snapshot'}={as_of.isoformat()}"
            final = parquet_dir(self.name) / folder / f"{table}.parquet"
            con = duckdb.connect()
            try:
                # Typed in DuckDB straight from the raw file (no Python row per record).
                rows = int(con.execute(f"SELECT count(*) FROM ({raw_rows(raw, spec['schema'])})").fetchone()[0])
                if not rows and not delta:
                    raise base.SourceError(f"wrds {table}: the snapshot came back empty")
                types = dict(spec["schema"])
                for col, n in untyped(con, raw, spec["schema"]).items():
                    ctx.issue("value_untyped", f"{table}.{col}", f"{n} row(s) of {rows} nulled (as {types[col]})")
                keys = ", ".join(spec["key"])
                # Two raw rows of one key keep the same one whatever their order: the first by every column.
                every = ", ".join(f'"{c}" NULLS LAST' for c, _ in spec["schema"])
                select = (f"SELECT *, DATE '{as_of.isoformat()}' AS source_as_of, "
                          f"CAST({float(meta['fetched_at'])} AS DOUBLE) AS fetched_at, "
                          f"'{meta['run_id']}' AS run_id FROM ({raw_rows(raw, spec['schema'])}) "
                          f"QUALIFY row_number() OVER (PARTITION BY {keys} ORDER BY {every}) = 1 ORDER BY {keys}")
                out.append(base.write_parquet(con, select, final))
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
