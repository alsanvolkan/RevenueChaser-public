"""A served ``fundamentals.duckdb`` written straight from rows, for the tests of the boards that read it (#642 PR 6).

The builder makes the live file from the SEC's answers (``rcdata/build/fundamentals``, driven in
``fund_fixture``); a board test only needs ``fundamental_company`` and ``fundamental_fact``. ``put_edgar`` takes the
old ``edgar_financials`` row shape the board tests were written against (``ticker``, ``annual``, ``quarterly``,
``meta``, each record in the extraction's shape) and writes the layer's rows of it into the live file, so
``core/fundamentals_layer`` rebuilds the same records. It also books a good ``sec/companyfacts`` check, so the layer
reads as built (``rcdata.read.missing``).

A company's CIK is ``meta["cik"]`` when the row gives one, else one made from the ticker. An annual record with no
``period_ends`` is dated fresh (``UNDATED_NEWEST_END``). A field with no accession
or filed day gets a placeholder accession and its period end as the filed day (the served table requires both on a
non-preliminary row).
"""
from __future__ import annotations

import zlib
from datetime import date, timedelta

import duckdb

from RevenueChaser.rcdata import ops, serving
from RevenueChaser.rcdata.build.fundamentals import tables

FACTS_DATASET = "sec/companyfacts"
TABLES = ("fundamental_company", "fundamental_fact", "filing_item")
_NOT_FIELDS = {"fiscal_year", "period"}
_ACCN = "0000000000-00-000000"
# shortcut: a fixed day, before the boards' pinned test days (Oct 2026) and fresh against the real one until
# ``fiscal_year.STALE_DAYS`` after it (Oct 2027); move it forward when a test reading today's date starts to drop.
UNDATED_NEWEST_END = date(2026, 6, 30)


def _connect() -> duckdb.DuckDBPyConnection:
    path = serving.live_path("fundamentals")
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    have = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    if serving.VERSION_TABLE not in have:
        con.execute(f"CREATE TABLE {serving.VERSION_TABLE} (kind TEXT, version INTEGER, built_at DOUBLE, "
                    "build_id TEXT)")
        con.execute(f"INSERT INTO {serving.VERSION_TABLE} VALUES ('fundamentals', ?, 1, 'test')",
                    [serving.SERVING_SCHEMA_VERSION["fundamentals"]])
    for table in TABLES:
        if table not in have:
            tables.create(con, table)
    return con


def cik_of(ticker: str, meta: dict | None = None) -> str:
    given = str((meta or {}).get("cik") or "").strip()
    if given:
        return str(int(given)) if given.isdigit() else given
    return str(zlib.crc32(str(ticker).strip().upper().encode()) % 900_000_000 + 1_000_000)


def _iso(v) -> str | None:
    if v is None:
        return None
    return v.isoformat() if isinstance(v, date) else str(v)[:10]


def _unit(field: str) -> str:
    return "USD/shares" if field.startswith("eps") else "shares" if "shares" in field else "USD"


def _facts(cik: str, rec: dict, period_type: str, newest_year: int | None = None) -> list[dict]:
    ends = rec.get("period_ends") or {}
    if period_type == "quarterly":
        default_end = _iso(rec["period"])
    elif ends:
        default_end = max(ends.values())
    else:
        # An undated record (the old row could carry none, and then had no age): the newest such year ends on
        # ``UNDATED_NEWEST_END`` and each earlier one a year before it, so it reads as a fresh filer.
        years_back = newest_year - int(rec["fiscal_year"])
        default_end = (UNDATED_NEWEST_END - timedelta(days=365 * years_back)).isoformat()
    starts, filed, accn = rec.get("period_starts") or {}, rec.get("filed") or {}, rec.get("accn") or {}
    derived, prelim = set(rec.get("derived") or []), set(rec.get("preliminary") or [])
    if rec.get("gross_profit_derived"):
        derived.add("gross_profit")
    out = []
    for field, value in rec.items():
        if field in _NOT_FIELDS or isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        end = _iso(ends.get(field)) if period_type == "annual" and ends.get(field) else default_end
        p = field in prelim
        out.append({"cik": cik, "field": field, "period_type": period_type,
                    "fiscal_year": int(rec["fiscal_year"]) if rec.get("fiscal_year") is not None else None,
                    "period_start": _iso(starts.get(field)), "period_end": end, "value": float(value),
                    "unit": _unit(field), "accn": None if p else accn.get(field) or _ACCN,
                    "filed": None if p else _iso(filed.get(field)) or end, "form": None,
                    "derived": field in derived, "preliminary": p,
                    "source": FACTS_DATASET})
    return out


def put_edgar(rows: list[dict], *, facts_as_of=None) -> None:
    """Old ``edgar_financials`` rows as served companies and facts; a ticker already there is replaced."""
    con = _connect()
    try:
        for row in rows:
            ticker, meta = str(row["ticker"]).strip().upper(), dict(row.get("meta") or {})
            cik = cik_of(ticker, meta)
            annual = row.get("annual") or []
            newest_year = max((int(r["fiscal_year"]) for r in annual if r.get("fiscal_year") is not None), default=None)
            facts = [f for rec in annual for f in _facts(cik, rec, "annual", newest_year)]
            facts += [f for rec in row.get("quarterly") or [] for f in _facts(cik, rec, "quarterly")]
            filed = [f["filed"] for f in facts if f["filed"]]
            ends = [f["period_end"] for f in facts if not f["preliminary"]]
            units = meta.get("non_usd_units")
            company = {"ticker": ticker, "cik": cik, "name": meta.get("entity_name"),
                       "entity_name": meta.get("entity_name") or "",
                       "is_us": True if meta.get("is_us") is None else bool(meta["is_us"]),
                       "non_usd_units": ",".join(units) if units else "", "us_gaap": True,
                       # The layer measures the currencies on every facts read; an old row that never measured them
                       # (no ``non_usd_units`` key) is a company whose facts were not read.
                       "facts_as_of": _iso(meta.get("facts_as_of") or facts_as_of)
                       or (date.today().isoformat() if "non_usd_units" in meta else None),
                       "newest_filed": _iso(meta.get("newest_filed")) or (max(filed) if filed else None),
                       "newest_period_end": _iso(meta.get("newest_period_end")) or (max(ends) if ends else None),
                       "submissions_as_of": _iso(meta.get("submissions_as_of")),
                       "source": FACTS_DATASET}
            con.execute("DELETE FROM fundamental_company WHERE ticker = ?", [ticker])
            con.execute("DELETE FROM fundamental_fact WHERE cik = ?", [cik])
            _insert(con, "fundamental_company", [company])
            _insert(con, "fundamental_fact", facts)
        con.execute("CHECKPOINT")
    finally:
        con.close()
    built()


def put_companies(rows: list[tuple[str, str]]) -> None:
    """``fundamental_company`` rows of ``(ticker, cik)``, the CIK as given (a test of a malformed one keeps it)."""
    con = _connect()
    try:
        for ticker, _cik in rows:
            con.execute("DELETE FROM fundamental_company WHERE ticker = ?", [str(ticker).upper()])
        _insert(con, "fundamental_company", [{"ticker": str(t).upper(), "cik": str(c), "source": FACTS_DATASET}
                                             for t, c in rows])
        con.execute("CHECKPOINT")
    finally:
        con.close()
    built()


def _norm(cik) -> str:
    return str(int(cik)) if str(cik).isdigit() else str(cik)


def put_filings(rows: list[dict], *, listed_on=None) -> None:
    """``sec/filings`` rows (``cik``, ``accession``, ``form``, ``filing_date``, ``items`` as ``'2.01,9.01'``) as
    ``filing_item`` rows, one per item; each company of those CIKs gets ``listed_on`` (default today) as the day its
    filing list was read (``submissions_as_of``)."""
    items = []
    for r in rows:
        codes = [c.strip() for c in str(r.get("items") or "").split(",") if c.strip()] or [None]
        items += [{"cik": _norm(r["cik"]), "accession": r["accession"], "form": r.get("form"),
                   "filing_date": _iso(r.get("filing_date")), "kind": "item", "item": code,
                   "source": "sec/filings"} for code in codes]
    listed = {i["cik"] for i in items}
    con = _connect()
    try:
        _insert(con, "filing_item", items)
        for ticker, cik in con.execute("SELECT ticker, cik FROM fundamental_company").fetchall():
            if _norm(cik) in listed:
                con.execute("UPDATE fundamental_company SET submissions_as_of = ? WHERE ticker = ?",
                            [_iso(listed_on) or date.today().isoformat(), ticker])
        con.execute("CHECKPOINT")
    finally:
        con.close()


def _insert(con: duckdb.DuckDBPyConnection, table: str, rows: list[dict]) -> None:
    if not rows:
        return
    cols = [c for c, _ in tables.SCHEMAS[table]]
    con.executemany(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                    [[r.get(c) for c in cols] for r in rows])


def built(dataset: str = FACTS_DATASET) -> None:
    """A good check of ``dataset``, so the layer reads as built."""
    if not ops.ever_built(dataset):
        ops.record_check(dataset, "changed")


def clear() -> None:
    serving.live_path("fundamentals").unlink(missing_ok=True)
