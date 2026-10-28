"""``ibkr/shortable``: IBKR's public short-stock file, one download a day.

IBKR publishes the shares it can lend, and the borrow fee and rebate, as a file on a public FTP server
(``RC_IBKR_SHORT_FTP_HOST``, default ``ftp3.interactivebrokers.com``; the user ``shortstock`` with an empty
password, as IBKR documents it), refreshed during the day. This is not the gateway and no account: market data only.

The file (``usa.txt``): ``#BOF|2026.10.28|09:45:02``, a header ``#SYM|CUR|NAME|CON|ISIN|REBATERATE|FEERATE|AVAILABLE|``
(``FIGI`` after it in newer files), one row per security (``AVAILABLE`` may read ``>10000000``, served as 10,000,000
with ``available_capped``; a rate may read ``NA``), and ``#EOF|<rows>``. It is parsed by the header's names; a file
without ``#BOF``, the three columns it needs, or an ``#EOF`` whose count matches is refused and not kept. Not read from
the cloud (no FTP egress there: the download timed out on 2026-10-28): the first live run on the server checks the
format.

One download a run, each attempt counted against ``ibkr`` (``base.HTTP_ATTEMPTS`` attempts on an FTP error). The
session of a snapshot is the one its download time belongs to (``calendar.session_for``: before 09:30 New York, the
previous session).

Raw: ``raw/ibkr/shortable/<as-of>.txt.gz`` (as downloaded) with a ``.meta.json`` sidecar. Parquet:
``parquet/ibkr/shortable/date=<as-of>/part-0.parquet``. Served: ``ibkr_shortable``, every day's snapshot.
"""
from __future__ import annotations

import ftplib
import io
import os
import time
from datetime import date
from pathlib import Path

import duckdb

from ....core import calendar
from .. import base
from ..spine.stage import partitions
from . import KIND, common

NAME = "ibkr/shortable"
PROVIDER = "ibkr"
TABLE = "ibkr_shortable"
HOST_ENV = "RC_IBKR_SHORT_FTP_HOST"
DEFAULT_HOST = "ftp3.interactivebrokers.com"
USER = "shortstock"
FILE = "usa.txt"
FTP_TIMEOUT_S = 60.0
NEEDED = ("SYM", "CUR", "AVAILABLE")
META_SUFFIX = ".meta.json"
SCHEMA = [("snapshot_date", "DATE"), ("session_date", "DATE"), ("snapshot_at", "TEXT"), ("symbol", "TEXT"),
          ("source_symbol", "TEXT"), ("currency", "TEXT"), ("name", "TEXT"), ("conid", "TEXT"), ("isin", "TEXT"),
          ("figi", "TEXT"), ("rebate_rate", "DOUBLE"), ("fee_rate", "DOUBLE"), ("available", "BIGINT"),
          ("available_capped", "BOOLEAN"), ("fetched_at", "DOUBLE"), ("run_id", "TEXT")]
SERVED = SCHEMA[:4] + [("ticker", "TEXT"), ("ticker_source", "TEXT")] + SCHEMA[4:] + [("source", "TEXT")]

# Seams: the FTP client class and the clock of a download (tests replace both).
_ftp_factory = ftplib.FTP
_now = time.time


def host() -> str:
    return (os.getenv(HOST_ENV) or DEFAULT_HOST).strip()


def raw_dir() -> Path:
    return base.raw_root() / "ibkr" / "shortable"


def parquet_dir() -> Path:
    return base.parquet_root() / "ibkr" / "shortable"


def raw_path(day: date) -> Path:
    return raw_dir() / f"{day.isoformat()}.txt.gz"


def _meta(raw: Path) -> Path:
    return raw.with_name(raw.name + META_SUFFIX)


def download(ctx: base.RunContext) -> bytes:
    """The file, through ``base.HTTP_ATTEMPTS`` FTP sessions at most, each one counted; ``SourceError`` after."""
    last = ""
    for attempt in range(1, base.HTTP_ATTEMPTS + 1):
        ctx.check_cap(PROVIDER)
        ctx.count(PROVIDER, calls=1)
        buf = io.BytesIO()
        try:
            ftp = _ftp_factory(host(), timeout=FTP_TIMEOUT_S)
            try:
                ftp.login(USER, "")
                ftp.retrbinary(f"RETR {FILE}", buf.write)
            finally:
                try:
                    ftp.quit()
                except (ftplib.all_errors + (EOFError,)):
                    ftp.close()
            return buf.getvalue()
        except (ftplib.all_errors + (EOFError,)) as e:
            last = f"{type(e).__name__}: {e}"
        if attempt < base.HTTP_ATTEMPTS:
            ctx.sleep(base.HTTP_BACKOFF_S * 2 ** (attempt - 1))
    ctx.count(PROVIDER, failures=1)
    raise base.SourceError(f"ibkr ftp {host()}/{FILE}: {last}")


def _rate(v: str):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse(body: bytes) -> tuple[str, list[dict]]:
    """``(snapshot time, rows)``; ``SourceError`` for anything but a whole file."""
    lines = [ln.rstrip("\r") for ln in body.decode("utf-8", errors="replace").splitlines() if ln.strip()]
    if not lines or not lines[0].startswith("#BOF|"):
        raise base.SourceError(f"ibkr {FILE}: no #BOF line ({(lines or [''])[0][:40]!r})")
    stamp = " ".join(p for p in lines[0].split("|")[1:3] if p)
    if len(lines) < 2 or not lines[1].startswith("#SYM|"):
        raise base.SourceError(f"ibkr {FILE}: no #SYM header")
    header = [h.strip().lstrip("#") for h in lines[1].split("|")]
    if any(n not in header for n in NEEDED):
        raise base.SourceError(f"ibkr {FILE}: the header lacks {[n for n in NEEDED if n not in header]}")
    rows, eof = [], None
    for ln in lines[2:]:
        if ln.startswith("#EOF"):
            parts = ln.split("|")
            eof = int(parts[1]) if len(parts) > 1 and parts[1].strip().isdigit() else -1
            break
        rec = dict(zip(header, (v.strip() for v in ln.split("|"))))
        symbol = common.norm_symbol(rec.get("SYM"))
        if not symbol:
            continue
        avail = rec.get("AVAILABLE") or ""
        capped = avail.startswith(">")
        digits = avail.lstrip(">").strip()
        rows.append({"symbol": symbol, "source_symbol": rec.get("SYM"), "currency": rec.get("CUR") or None,
                     "name": rec.get("NAME") or None, "conid": rec.get("CON") or None,
                     "isin": rec.get("ISIN") or None, "figi": rec.get("FIGI") or None,
                     "rebate_rate": _rate(rec.get("REBATERATE")), "fee_rate": _rate(rec.get("FEERATE")),
                     "available": int(digits) if digits.isdigit() else None, "available_capped": capped})
    if eof is None:
        raise base.SourceError(f"ibkr {FILE}: no #EOF line (cut short at {len(rows)} rows)")
    if eof != len(rows):
        raise base.SourceError(f"ibkr {FILE}: #EOF says {eof} rows, {len(rows)} read")
    if not rows:
        raise base.SourceError(f"ibkr {FILE}: no rows")
    return stamp, rows


def _session(epoch: float) -> date | None:
    try:
        return calendar.session_for(epoch).session_date
    except ValueError:
        return None


class IbkrShortable:
    name = NAME
    kind = KIND
    providers = (PROVIDER,)
    tables = (TABLE,)
    cadence = "daily"
    backfill_needs_from = False
    starts_by_hand = True             # the first run is the server thread's, after a backup

    def default_as_of(self) -> date:
        return calendar.today_ny()

    def fetch(self, ctx: base.RunContext, as_of: date) -> list[Path]:
        raw = raw_path(as_of)
        if raw.exists():
            return [raw]
        body = download(ctx)
        parse(body)                                     # refuse a cut or foreign file before it is kept
        base.write_raw_json(_meta(raw), {"as_of": as_of.isoformat(), "fetched_at": _now(), "run_id": ctx.run_id,
                                         "host": host(), "file": FILE})
        return [base.write_raw(raw, body)]

    def backfill(self, ctx: base.RunContext, start: date | None, end: date) -> None:
        # IBKR publishes the current file only: a backfill is one download.
        self.to_parquet_all(ctx, self.fetch(ctx, end))

    def to_parquet(self, ctx: base.RunContext, raw: Path) -> list[Path]:
        return self.to_parquet_all(ctx, [raw])

    def to_parquet_all(self, ctx: base.RunContext, raws: list[Path]) -> list[Path]:
        out = []
        for raw in raws:
            meta = base.read_raw_json(_meta(raw))
            stamp, parsed = parse(base.read_raw(raw))
            day = date.fromisoformat(meta["as_of"])
            rows = [{**r, "snapshot_date": day, "session_date": _session(meta["fetched_at"]), "snapshot_at": stamp,
                     "fetched_at": meta["fetched_at"], "run_id": meta["run_id"]} for r in parsed]
            con = duckdb.connect()
            try:
                base.rows_table(con, "rows", SCHEMA, rows)
                out.append(base.write_parquet(con, "SELECT * FROM rows ORDER BY symbol",
                                              parquet_dir() / f"date={day.isoformat()}" / "part-0.parquet"))
            finally:
                con.close()
        return out

    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        files = [f for _, f in partitions(parquet_dir(), "date")]
        if not files:
            return {TABLE: common.create_table(con, TABLE, SERVED)}
        src = common.files_sql(files)
        common.symbol_map(con, f"SELECT symbol FROM {src}", ctx.notes, NAME)
        select = (f"SELECT p.*, m.ticker, m.ticker_source, '{NAME}' AS source FROM {src} p "
                  f"LEFT JOIN _symbol_map m USING (symbol) ORDER BY p.snapshot_date, p.symbol")
        return {TABLE: common.create_table(con, TABLE, SERVED, select)}

    def check(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict:
        added, removed = base.compare_with_live(con, KIND, TABLE, ["snapshot_date", "symbol", "available", "fee_rate"])
        newest = con.execute(f"SELECT max(snapshot_date) FROM {TABLE}").fetchone()[0]
        return {"dataset": NAME, "result": "changed" if added or removed else "unchanged",
                "source_as_of": (newest or ctx.as_of or self.default_as_of()).isoformat(),
                "rows": common.count(con, TABLE), "added": added, "removed": removed}


DATASET = IbkrShortable()
