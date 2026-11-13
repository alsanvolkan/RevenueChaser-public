"""``uw/institutions``: Unusual Whales' map of every 13F institution, keyed by CIK, once a quarter (#775).

``GET /institutions`` without a name lists every institution by total value, 500 to a page: ``cik``, ``name``,
``short_name``, ``tags`` (the vocabulary of ``docs/openapi.yaml``'s ``Institution Tag``: ``activist``,
``value_investor``, ``tiger_club``, ``hedge_fund``, ``esg``, ``credit``, ``13d_activist``, ``energy``,
``small_cap``, ``event``, ``real_estate``, ``technology``, ``biotech``, ``advisor``, ``brokerage``,
``private_fund``, ``top_fund``, ``public_companies``, ``known``), ``is_hedge_fund``, the report and filing dates,
the aggregate values, and a ``succession`` object. Measured against the live account on 12 Nov 2026: 18 pages,
8,875 rows, 8,875 distinct CIKs. The key is the CIK, which our own ``sec13f_*`` tables already carry per quarter,
so one read of this list slices every 13F metric by investor type with no per-ticker provider call.

**Why quarterly.** The list only moves when 13F filings land, and a snapshot is kept per day it was read
(``snapshot_date``), so the board can say which snapshot a slice rests on (owner's decision B, 12 Nov 2026).
``build/due.py`` learned the cadence for it (``CYCLE_DAYS["quarterly"]``, 91 days).

**Paging.** Page 0 without ``page``, then ``page=1, 2, ...``, each with ``limit=500``, until a page is short or
empty; :data:`MAX_PAGES` stops an answer that never ends. UW orders the list by value, so a filing that lands
while we page can move a row from one page to the next: a CIK seen on two pages is dropped to its first row and
counted in a ``moved`` build issue, and past :data:`MAX_MOVED_SHARE` of the rows the read is refused rather than
keep a snapshot the list no longer holds. The refusal is :func:`check_whole`, in ``fetch`` **before** the raw file
is written (as ``ibkr/shortable`` parses before it keeps): a moved snapshot left on disk would be typed again by
every later run and fail for good, because a held raw file is reused at no call. So a run cut off mid-paging, and
one whose list moved, both leave no file that reads as whole.

Raw: ``raw/uw/institutions/<as-of>.json.gz``, the pages as they arrived. Parquet:
``parquet/uw/institutions/institution/date=<as-of>/part-0.parquet`` and
``parquet/uw/institutions/succession/date=<as-of>/part-0.parquet``. Served: ``uw_institution`` and
``uw_institution_succession``, every snapshot kept, keyed by ``snapshot_date``.

**The succession table** carries one row per predecessor and successor pair, built from **both** sides UW gives:
an institution's ``succession.predecessors`` (the ones it absorbed) and its own ``succession.succeeded_by`` (who
absorbed it). The two disagree in what they cover: on 12 Nov 2026 only two rows of the whole list carried a
``succeeded_by``, while HPS Investment Partners -> BlackRock (effective 2025-09-30) was in BlackRock's
``predecessors`` alone, and HPS itself is no longer listed. A pair both sides name is one row, ``origin``
``both``.
"""
from __future__ import annotations

import time
from datetime import date
from pathlib import Path

import duckdb

from ....core import calendar
from .. import base
from ..spine.stage import partitions
from . import KIND, common

NAME = "uw/institutions"
PROVIDER = "uw"
TABLE = "uw_institution"
SUCCESSION_TABLE = "uw_institution_succession"
ENDPOINT = "/institutions"
#: UW's documented maximum for this endpoint (``docs/openapi.yaml``: "Default 500 Max 500 Min 1").
PAGE_LIMIT = 500
#: Pages before the run fails instead of asking on: 60 pages are 30,000 institutions, far past the 8,875 measured
#: on 12 Nov 2026, and only stop an answer that never ends.
MAX_PAGES = 60
#: The share of rows that may repeat across pages before the answer counts as moved rather than jittered.
MAX_MOVED_SHARE = 0.01

SCHEMA = [("snapshot_date", "DATE"), ("cik", "TEXT"), ("name", "TEXT"), ("short_name", "TEXT"),
          ("tags", "VARCHAR[]"), ("is_hedge_fund", "BOOLEAN"), ("report_date", "DATE"), ("filing_date", "DATE"),
          ("total_value", "DOUBLE"), ("share_value", "DOUBLE"), ("succeeded_by_cik", "TEXT"),
          ("succeeded_by_report_date", "DATE"), ("current_holder_cik", "TEXT"), ("source_as_of", "DATE"),
          ("fetched_at", "DOUBLE"), ("run_id", "TEXT")]
SERVED = SCHEMA + [("source", "TEXT")]

SUCCESSION_SCHEMA = [("snapshot_date", "DATE"), ("predecessor_cik", "TEXT"), ("successor_cik", "TEXT"),
                     ("effective_report_date", "DATE"), ("predecessor_name", "TEXT"), ("successor_name", "TEXT"),
                     ("origin", "TEXT"), ("source_as_of", "DATE"), ("fetched_at", "DOUBLE"), ("run_id", "TEXT")]
SUCCESSION_SERVED = SUCCESSION_SCHEMA + [("source", "TEXT")]

#: What ``scaffold._ownership_schemas`` reads: this module serves two tables, not one.
SCHEMAS = {TABLE: SERVED, SUCCESSION_TABLE: SUCCESSION_SERVED}

#: Where a pair was named: in the successor's ``predecessors``, in the predecessor's own ``succeeded_by``, or both.
FROM_PREDECESSORS, FROM_SUCCEEDED_BY, FROM_BOTH = "predecessors", "succeeded_by", "both"

# Seam: the clock of a fetch (tests replace it).
_now = time.time


def raw_dir() -> Path:
    return base.raw_root() / "uw" / "institutions"


def raw_path(day: date) -> Path:
    return raw_dir() / f"{day.isoformat()}.json.gz"


def parquet_dir(table: str = TABLE) -> Path:
    leaf = "succession" if table == SUCCESSION_TABLE else "institution"
    return base.parquet_root() / "uw" / "institutions" / leaf


def _text(value) -> str | None:
    s = None if value is None else str(value).strip()
    return s or None


def norm_cik(value) -> str | None:
    """A CIK in the ten-digit form the ``sec13f_*`` tables use (``2012383`` -> ``0002012383``); None for none."""
    digits = "".join(c for c in str(value or "") if c.isdigit())
    return digits.zfill(10) if digits else None


def _day(value) -> date | None:
    text = _text(value)
    if text is None:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _number(value) -> float | None:
    """UW sends every value as a string (``'6729538649155'``); None for anything that is not one."""
    text = _text(value)
    if text is None:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _bool(value) -> bool | None:
    if isinstance(value, bool):
        return value
    text = _text(value)
    return None if text is None else text.lower() in ("true", "1", "yes", "t")


def _tags(value) -> list[str]:
    """The row's tags, deduped and sorted, so two snapshots of the same tags compare equal."""
    if not isinstance(value, (list, tuple)):
        return []
    return sorted({t for t in (_text(v) for v in value) if t})


def _party(value) -> tuple[str | None, str | None, date | None]:
    """``(cik, name, effective report date)`` of a ``succession`` party: UW writes it as an object
    (``{"cik": ..., "name": ..., "effective_report_date": ...}``), and a bare CIK string is read as one too."""
    if isinstance(value, dict):
        return norm_cik(value.get("cik")), _text(value.get("name")), _day(value.get("effective_report_date"))
    cik = norm_cik(value) if _text(value) else None
    return cik, None, None


def _rows_of(body) -> list | None:
    """The ``data`` list of a page body (or a bare list); None when the answer carries no list of rows."""
    items = body.get("data") if isinstance(body, dict) else body
    return items if isinstance(items, list) else None


def fetch_pages(ctx: base.RunContext) -> list[list[dict]]:
    """Every page of the institution list, in order; ``SourceError`` for a refused call or an answer with no rows."""
    pages: list[list[dict]] = []
    while len(pages) < MAX_PAGES:
        ctx.check_cap(PROVIDER)
        params = {"limit": PAGE_LIMIT} if not pages else {"limit": PAGE_LIMIT, "page": len(pages)}
        res = ctx.uw_client().get_result(ENDPOINT, params)
        if res.status != "ok":
            raise base.SourceError(f"UW {ENDPOINT} page {len(pages)}: {res.error}")
        rows = _rows_of(res.data)
        if rows is None:
            raise base.SourceError(f"UW {ENDPOINT} page {len(pages)}: no list of rows in the answer")
        if not rows:
            break                       # the empty page ends the list (a total that is a multiple of the limit)
        pages.append(rows)
        if len(rows) < PAGE_LIMIT:
            break                       # a short page is the last one
    else:
        raise base.SourceError(f"UW {ENDPOINT}: {MAX_PAGES} full pages of {PAGE_LIMIT} rows reach the page ceiling")
    if not pages:
        raise base.SourceError(f"UW {ENDPOINT}: no rows")       # the whole 13F filer list is never empty
    return pages


def check_whole(pages: list[list[dict]]) -> None:
    """``SourceError`` when the pages are not a snapshot worth keeping: no row carries a CIK, or more than
    :data:`MAX_MOVED_SHARE` of them repeat across pages (the list moved while we paged)."""
    rows, repeated, _unkeyed = institution_rows(pages, date.min, {"fetched_at": 0.0, "run_id": ""})
    if not rows:
        raise base.SourceError(f"UW {ENDPOINT}: no row carries a CIK")
    total = repeated + len(rows)
    if repeated > total * MAX_MOVED_SHARE:
        raise base.SourceError(f"UW {ENDPOINT}: {repeated} of {total} rows repeated across pages "
                               f"(over {MAX_MOVED_SHARE:.0%}); the list moved while paging")


def institution_rows(pages: list[list[dict]], day: date, meta: dict) -> tuple[list[dict], int, int]:
    """``(rows, repeated CIKs, rows with no CIK)``: one row per institution, the first page a CIK is on winning."""
    rows, seen, repeated, unkeyed = [], set(), 0, 0
    for page in pages:
        for item in page if isinstance(page, list) else []:
            if not isinstance(item, dict):
                unkeyed += 1
                continue
            cik = norm_cik(item.get("cik"))
            if cik is None:
                unkeyed += 1
                continue
            if cik in seen:
                repeated += 1
                continue
            seen.add(cik)
            succession = item.get("succession") if isinstance(item.get("succession"), dict) else {}
            by_cik, _by_name, by_date = _party(succession.get("succeeded_by"))
            holder_cik, _holder_name, _holder_date = _party(succession.get("current_holder"))
            rows.append({"snapshot_date": day, "cik": cik, "name": _text(item.get("name")),
                         "short_name": _text(item.get("short_name")), "tags": _tags(item.get("tags")),
                         "is_hedge_fund": _bool(item.get("is_hedge_fund")), "report_date": _day(item.get("date")),
                         "filing_date": _day(item.get("filing_date")),
                         "total_value": _number(item.get("total_value")),
                         "share_value": _number(item.get("share_value")),
                         "succeeded_by_cik": by_cik, "succeeded_by_report_date": by_date,
                         "current_holder_cik": holder_cik, "source_as_of": day,
                         "fetched_at": meta["fetched_at"], "run_id": meta["run_id"]})
    return rows, repeated, unkeyed


def succession_rows(pages: list[list[dict]], day: date, meta: dict) -> list[dict]:
    """One row per predecessor and successor pair, from both sides UW names, deduped on the pair and its date."""
    out: dict[tuple[str, str, date | None], dict] = {}

    def add(pred_cik, succ_cik, when, pred_name, succ_name, origin) -> None:
        if not pred_cik or not succ_cik or pred_cik == succ_cik:
            return
        key = (pred_cik, succ_cik, when)
        held = out.get(key)
        if held is None:
            out[key] = {"snapshot_date": day, "predecessor_cik": pred_cik, "successor_cik": succ_cik,
                        "effective_report_date": when, "predecessor_name": pred_name,
                        "successor_name": succ_name, "origin": origin, "source_as_of": day,
                        "fetched_at": meta["fetched_at"], "run_id": meta["run_id"]}
            return
        if held["origin"] != origin:
            held["origin"] = FROM_BOTH
        held["predecessor_name"] = held["predecessor_name"] or pred_name
        held["successor_name"] = held["successor_name"] or succ_name

    for page in pages:
        for item in page if isinstance(page, list) else []:
            if not isinstance(item, dict):
                continue
            cik, name = norm_cik(item.get("cik")), _text(item.get("name"))
            succession = item.get("succession") if isinstance(item.get("succession"), dict) else {}
            for entry in succession.get("predecessors") or []:
                pred_cik, pred_name, when = _party(entry)
                add(pred_cik, cik, when, pred_name, name, FROM_PREDECESSORS)
            succ_cik, succ_name, when = _party(succession.get("succeeded_by"))
            add(cik, succ_cik, when, name, succ_name, FROM_SUCCEEDED_BY)
    return [out[k] for k in sorted(out, key=lambda k: (k[0], k[1], k[2] or date.min))]


class UWInstitutions:
    name = NAME
    kind = KIND
    providers = (PROVIDER,)
    tables = (TABLE, SUCCESSION_TABLE)
    cadence = "quarterly"
    backfill_needs_from = False
    starts_by_hand = True             # the first run is the server thread's, after a backup
    dirty_keyed = True

    def default_as_of(self) -> date:
        return calendar.today_ny()    # the snapshot's day, not a session: the list changes on any day

    def fetch(self, ctx: base.RunContext, as_of: date) -> list[Path]:
        raw = raw_path(as_of)
        if raw.exists():
            return [raw]              # the day's snapshot is held: a rerun types it again at no call
        pages = fetch_pages(ctx)
        check_whole(pages)                # refuse a moved or CIK-less list before it is kept
        return [base.write_raw_json(raw, {"request": {"endpoint": ENDPOINT, "limit": PAGE_LIMIT},
                                          "snapshot_date": as_of.isoformat(), "fetched_at": _now(),
                                          "run_id": ctx.run_id, "pages": pages})]

    def backfill(self, ctx: base.RunContext, start: date | None, end: date) -> None:
        # UW serves the list as it stands: a backfill is one read.
        self.to_parquet_all(ctx, self.fetch(ctx, end))

    def to_parquet(self, ctx: base.RunContext, raw: Path) -> list[Path]:
        doc = base.read_raw_json(raw)
        day = date.fromisoformat(doc["snapshot_date"])
        pages = doc.get("pages") or []
        meta = {"fetched_at": doc["fetched_at"], "run_id": doc["run_id"]}
        rows, repeated, unkeyed = institution_rows(pages, day, meta)
        if not rows:
            raise base.SourceError(f"UW {ENDPOINT} {day}: no usable row in {raw.name}")
        if unkeyed:
            ctx.issue("unkeyed", day.isoformat(), f"{unkeyed} rows without a CIK")
        if repeated:
            # The share is refused in ``fetch`` (``check_whole``); here the count is reported, whatever it is, so a
            # file kept by an earlier run still types into a snapshot rather than failing for good.
            ctx.issue("moved", day.isoformat(),
                      f"{repeated} of {repeated + len(rows)} rows repeated across pages while paging; the first "
                      f"row of a CIK stands")
        out = []
        con = duckdb.connect()
        try:
            base.rows_table(con, "rows", SCHEMA, rows)
            out.append(base.write_parquet(con, "SELECT * FROM rows ORDER BY cik",
                                          parquet_dir(TABLE) / f"date={day.isoformat()}" / "part-0.parquet"))
            base.rows_table(con, "succession", SUCCESSION_SCHEMA, succession_rows(pages, day, meta))
            out.append(base.write_parquet(
                con, "SELECT * FROM succession ORDER BY predecessor_cik, successor_cik, effective_report_date",
                parquet_dir(SUCCESSION_TABLE) / f"date={day.isoformat()}" / "part-0.parquet"))
        finally:
            con.close()
        return out

    def to_parquet_all(self, ctx: base.RunContext, raws: list[Path]) -> list[Path]:
        return [p for raw in sorted(raws) for p in self.to_parquet(ctx, raw)]

    def dirty(self, ctx: base.RunContext):
        return common.keyed_dirty(ctx, self, "date")

    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        return {
            TABLE: common.build_keyed(ctx, con, NAME, TABLE, SERVED, partitions(parquet_dir(TABLE), "date"),
                                      "snapshot_date", "snapshot_date, cik"),
            SUCCESSION_TABLE: common.build_keyed(
                ctx, con, NAME, SUCCESSION_TABLE, SUCCESSION_SERVED,
                partitions(parquet_dir(SUCCESSION_TABLE), "date"), "snapshot_date",
                "snapshot_date, predecessor_cik, successor_cik"),
        }

    def check(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict:
        added, removed = base.compare_with_live(con, KIND, TABLE,
                                                ["snapshot_date", "cik", "tags", "total_value", "succeeded_by_cik"])
        newest = con.execute(f"SELECT max(snapshot_date) FROM {TABLE}").fetchone()[0]
        return {"dataset": NAME, "result": "changed" if added or removed else "unchanged",
                "source_as_of": (newest or ctx.as_of or self.default_as_of()).isoformat(),
                "rows": common.count(con, TABLE), "added": added, "removed": removed}


DATASET = UWInstitutions()
