"""``sec/ftd``: the SEC's fails-to-deliver data, one file per half month.

``GET <RC_SEC_WWW_BASE>/files/data/fails-deliver-data/cnsfails<YYYYMM><a|b>.zip``: half ``a`` holds the settlement
days 1 to 15, half ``b`` the 16th to the month's end. The zip holds one pipe-delimited text file, ``SETTLEMENT
DATE|CUSIP|SYMBOL|QUANTITY (FAILS)|DESCRIPTION|PRICE`` (the SEC's documented layout; sec.gov refused the cloud, so the
first live run on the server checks it), then trailer lines (``Trailer record count ...``). A half the SEC has not
published yet answers 404.

A run asks the halves after the newest one stored, oldest first, and stops at the first 404 (a later half is never
published before an earlier one); the first run asks back from the half before today's until one answers, at most
``FIRST_LOOKBACK`` halves. A backfill asks every half of its range. A 404 of a half that ended more than
``ABSENT_AFTER`` ago is marked absent (``.none``), never asked again. Each request is paced (``sec.Pacer``).

Raw: ``raw/sec/ftd/cnsfails<YYYYMM><a|b>.zip`` (as answered) with a ``.meta.json`` sidecar. Parquet:
``parquet/sec/ftd/half=<YYYY-MM><a|b>/part-0.parquet``. Served: ``sec_ftd``, one row per settlement date, CUSIP and
symbol as the SEC wrote them (a repeat is kept), the symbol named by the spine.
"""
from __future__ import annotations

import io
import time
import zipfile
from datetime import date, timedelta
from pathlib import Path

import duckdb

from ....core import calendar
from .. import base
from ..spine.stage import partitions
from . import KIND, common, sec

NAME = "sec/ftd"
TABLE = "sec_ftd"
PATH = "/files/data/fails-deliver-data/cnsfails{half}.zip"
FIRST_LOOKBACK = 3
ABSENT_AFTER = timedelta(days=90)
NEEDED = ("SETTLEMENT DATE", "CUSIP", "SYMBOL", "QUANTITY (FAILS)")
META_SUFFIX = ".meta.json"
SCHEMA = [("settlement_date", "DATE"), ("cusip", "TEXT"), ("symbol", "TEXT"), ("source_symbol", "TEXT"),
          ("quantity", "BIGINT"), ("description", "TEXT"), ("price", "DOUBLE"), ("half", "TEXT"),
          ("fetched_at", "DOUBLE"), ("run_id", "TEXT")]
SERVED = SCHEMA[:3] + [("ticker", "TEXT"), ("ticker_source", "TEXT")] + SCHEMA[3:] + [("source", "TEXT")]


def half_of(d: date) -> str:
    return f"{d:%Y%m}" + ("a" if d.day <= 15 else "b")


def next_half(h: str) -> str:
    if h.endswith("a"):
        return h[:6] + "b"
    y, m = int(h[:4]), int(h[4:6])
    return f"{y + (m == 12):04d}{m % 12 + 1:02d}a"


def prev_half(h: str) -> str:
    if h.endswith("b"):
        return h[:6] + "a"
    y, m = int(h[:4]), int(h[4:6])
    return f"{y - (m == 1):04d}{(m - 2) % 12 + 1:02d}b"


def half_end(h: str) -> date:
    y, m = int(h[:4]), int(h[4:6])
    if h.endswith("a"):
        return date(y, m, 15)
    return date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)


def label(h: str) -> str:
    return f"{h[:4]}-{h[4:6]}{h[6]}"


def raw_dir() -> Path:
    return base.raw_root() / "sec" / "ftd"


def parquet_dir() -> Path:
    return base.parquet_root() / "sec" / "ftd"


def raw_path(h: str) -> Path:
    return raw_dir() / f"cnsfails{h}.zip"


def none_path(h: str) -> Path:
    return raw_dir() / f"cnsfails{h}.none"


def _meta(raw: Path) -> Path:
    return raw.with_name(raw.name + META_SUFFIX)


def stored() -> list[str]:
    if not raw_dir().is_dir():
        return []
    return sorted(p.name[len("cnsfails"):-len(".zip")] for p in raw_dir().glob("cnsfails*.zip"))


def done(h: str) -> bool:
    return raw_path(h).exists() or none_path(h).exists()


def parse(body: bytes) -> tuple[list[dict], int | None]:
    """The half's rows, and the trailer's record count when it states one; ``SourceError`` for an answer that is
    not the zip of that file."""
    try:
        z = zipfile.ZipFile(io.BytesIO(body))
    except zipfile.BadZipFile as e:
        raise base.SourceError("sec ftd: the answer is not a zip") from e
    members = [n for n in z.namelist() if not n.endswith("/")]
    name = next((n for n in members if n.lower().endswith(".txt")), members[0] if members else None)
    if name is None:
        raise base.SourceError("sec ftd: the zip is empty")
    lines = z.read(name).decode("latin-1").splitlines()
    head = next((i for i, ln in enumerate(lines) if "SETTLEMENT DATE" in ln.upper()), None)
    cols = [c.strip().upper() for c in lines[head].split("|")] if head is not None else []
    if any(n not in cols for n in NEEDED):
        raise base.SourceError(f"sec ftd {name}: no {'|'.join(NEEDED)} header")
    rows, trailer = [], None
    for ln in lines[head + 1:]:
        if ln.upper().startswith("TRAILER RECORD COUNT"):
            digits = "".join(ch for ch in ln if ch.isdigit())
            trailer = int(digits) if digits else None
            continue
        parts = [p.strip() for p in ln.split("|")]
        if len(parts) < len(NEEDED):
            continue
        rec = dict(zip(cols, parts))
        day = rec.get("SETTLEMENT DATE", "")
        if not (len(day) == 8 and day.isdigit()):
            continue
        qty = rec.get("QUANTITY (FAILS)", "")
        try:
            price = float(rec.get("PRICE") or "")
        except ValueError:
            price = None
        rows.append({"settlement_date": date(int(day[:4]), int(day[4:6]), int(day[6:])),
                     "cusip": (rec.get("CUSIP") or "").upper() or None,
                     "symbol": common.norm_symbol(rec.get("SYMBOL")) or None, "source_symbol": rec.get("SYMBOL"),
                     "quantity": int(qty) if qty.isdigit() else None, "description": rec.get("DESCRIPTION") or None,
                     "price": price})
    if not rows:
        raise base.SourceError(f"sec ftd {name}: no rows")
    return rows, trailer


class SecFtd:
    name = NAME
    kind = KIND
    providers = (sec.PROVIDER,)
    tables = (TABLE,)
    cadence = "daily"
    starts_by_hand = True             # the first run is the server thread's, after a backup

    def default_as_of(self) -> date:
        return calendar.today_ny()

    def _get(self, ctx: base.RunContext, pacer: sec.Pacer, h: str, as_of: date) -> Path | None:
        url = sec.www_base() + PATH.format(half=h)
        r = sec.get(ctx, pacer, url, accept=(404,))
        if r.status_code == 404:
            if half_end(h) < as_of - ABSENT_AFTER:
                base.write_raw_json(none_path(h), {"status": 404, "url": url, "run_id": ctx.run_id})
            return None
        parse(r.content)                                  # refuse a foreign answer before it is kept
        raw = raw_path(h)
        base.write_raw_json(_meta(raw), {"half": h, "fetched_at": time.time(), "run_id": ctx.run_id, "url": url})
        return base.write_raw(raw, r.content)

    def fetch(self, ctx: base.RunContext, as_of: date) -> list[Path]:
        current, have = half_of(as_of), stored()
        pacer = sec.Pacer(ctx)
        if not have:
            h = prev_half(current)
            for _ in range(FIRST_LOOKBACK):
                raw = self._get(ctx, pacer, h, as_of)
                if raw is not None:
                    return [raw]
                h = prev_half(h)
            return []
        out, h = [], next_half(have[-1])
        while h < current:
            if not done(h):
                raw = self._get(ctx, pacer, h, as_of)
                if raw is None:
                    break
                out.append(raw)
            h = next_half(h)
        return out

    def backfill(self, ctx: base.RunContext, start: date | None, end: date) -> None:
        last = min(half_of(end), prev_half(half_of(calendar.today_ny())))
        pacer, out, h = sec.Pacer(ctx), [], half_of(start or end)
        while h <= last:
            if not done(h):
                raw = self._get(ctx, pacer, h, end)
                if raw is not None:
                    out.append(raw)
            h = next_half(h)
        self.to_parquet_all(ctx, out)

    def to_parquet(self, ctx: base.RunContext, raw: Path) -> list[Path]:
        return self.to_parquet_all(ctx, [raw])

    def to_parquet_all(self, ctx: base.RunContext, raws: list[Path]) -> list[Path]:
        out = []
        for raw in raws:
            meta = base.read_raw_json(_meta(raw))
            rows, trailer = parse(base.read_raw(raw))
            if trailer is not None and trailer != len(rows):
                ctx.notes.append(f"cnsfails{meta['half']}: {len(rows)} rows read, the trailer says {trailer}")
            rows = [{**r, "half": label(meta["half"]), "fetched_at": meta["fetched_at"], "run_id": meta["run_id"]}
                    for r in rows]
            con = duckdb.connect()
            try:
                base.rows_table(con, "rows", SCHEMA, rows)
                out.append(base.write_parquet(con, "SELECT * FROM rows ORDER BY settlement_date, cusip",
                                              parquet_dir() / f"half={label(meta['half'])}" / "part-0.parquet"))
            finally:
                con.close()
        return out

    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        files = [f for _, f in partitions(parquet_dir(), "half")]
        if not files:
            return {TABLE: common.create_table(con, TABLE, SERVED)}
        src = common.files_sql(files)
        common.symbol_map(con, f"SELECT symbol FROM {src}", ctx.notes, NAME)
        select = (f"SELECT p.*, m.ticker, m.ticker_source, '{NAME}' AS source FROM {src} p "
                  f"LEFT JOIN _symbol_map m USING (symbol) ORDER BY p.settlement_date, p.cusip, p.symbol")
        return {TABLE: common.create_table(con, TABLE, SERVED, select)}

    def check(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict:
        added, removed = base.compare_with_live(con, KIND, TABLE, ["settlement_date", "cusip", "symbol", "quantity"])
        newest = con.execute(f"SELECT max(settlement_date) FROM {TABLE}").fetchone()[0]
        return {"dataset": NAME, "result": "changed" if added or removed else "unchanged",
                "source_as_of": (newest or ctx.as_of or self.default_as_of()).isoformat(),
                "rows": common.count(con, TABLE), "added": added, "removed": removed}


DATASET = SecFtd()
