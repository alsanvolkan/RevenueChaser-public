"""``sec/companyfacts``: each Tier A company's XBRL facts, re-read on the day it files a statement (#43 C14).

The old store read a company's facts once and never again (``edgar_financials`` has no expiry, the root of the
Fundamentals review's F3). Here the trigger is the SEC daily index (``sec_index.py``): a run reads the indexes of the
last ``sec_index.LOOKBACK_DAYS`` days, or back to the day after its last whole run when that is longer ago (a builder
that was down still reads what was filed meanwhile: ``sec_index.lookback``), and re-reads
``GET <RC_SEC_DATA_BASE>/api/xbrl/companyfacts/CIK<10 digits>.json`` for every Tier A company that filed a statement
form (``facts.STATEMENT_FORMS``) whose accession its newest stored facts do not hold yet. The SEC's XBRL API can lag a
filing by hours: an accession still missing after the re-read is ``pending`` (the run's note) and asked again by the
next day's run, while the filing is inside the last ``sec_index.LOOKBACK_DAYS`` days. A Tier A company with no stored
facts at all is read too, up to ``NEW_PER_RUN`` a run (a new name; the first fill is the backfill). Requests are
spaced under 5 a second (``common.SEC_INTERVAL_S``) and counted against the ``sec`` cap.

Backfill (``backfill sec/companyfacts``, no ``--from``): every Tier A company with no stored facts, resumable (a
company read is not read again).

Raw: ``raw/sec/companyfacts/<cik>/<as-of>.json.gz`` with a ``.meta.json`` sidecar (as-of, fetch time, run, URL, the
accessions the facts hold, the ones the run wanted and the ones still missing); one re-read per company and as-of day
(the raw file is the resume marker). A 404 (no XBRL facts at the SEC) writes ``<as-of>.absent.json`` instead, and
the company is not asked again for ``ABSENT_DAYS`` unless it files a statement. Parquet:
``parquet/sec/companyfacts/cik=<cik>/part-0.parquet`` (the fact rows of the newest re-read, replaced whole: the
facts answer holds the whole history) and ``.../company.parquet`` (name, filer kind, the newest read).

Staging: ``stg_sec_facts``, ``stg_sec_company``.
"""
from __future__ import annotations

import time
from datetime import date, timedelta
from pathlib import Path

import duckdb

from ....core import calendar
from .. import base
from . import KIND, common, facts, sec_index

NAME = "sec/companyfacts"
NEW_PER_RUN = 500
ABSENT_DAYS = 30
STALE_DAYS = 120
STAGING = {"facts": "stg_sec_facts", "company": "stg_sec_company"}


def url(cik: str) -> str:
    return f"{common.sec_data()}/api/xbrl/companyfacts/CIK{common.cik10(cik)}.json"


def cik_dir(cik: str) -> Path:
    return common.raw_dir(NAME) / str(cik)


def _raws(cik: str) -> list[Path]:
    folder = cik_dir(cik)
    return sorted(folder.glob("*.json.gz")) if folder.is_dir() else []


def _newest_read(cik: str) -> date | None:
    raws = _raws(cik)
    return date.fromisoformat(raws[-1].name.split(".", 1)[0]) if raws else None


def _absent_since(cik: str) -> date | None:
    folder = cik_dir(cik)
    marks = sorted(folder.glob("*.absent.json")) if folder.is_dir() else []
    return date.fromisoformat(marks[-1].name.split(".", 1)[0]) if marks else None


def _held(cik: str) -> set[str]:
    """The accessions the newest stored facts of ``cik`` hold."""
    raws = _raws(cik)
    if not raws:
        return set()
    return set(base.read_raw_json(common.meta_path(raws[-1])).get("accessions") or [])


def _accessions(body: dict) -> set[str]:
    out = set()
    for taxonomy in (body.get("facts") or {}).values():
        for concept in (taxonomy or {}).values():
            for units in ((concept or {}).get("units") or {}).values():
                out.update(f["accn"] for f in units or [] if f.get("accn"))
    return out


def read_company(ctx: base.RunContext, cik: str, as_of: date, wanted: set[str] = frozenset()) -> Path | None:
    """Re-read one company's facts for ``as_of`` (resumable); the raw file, or None when the SEC has none (404)."""
    raw = cik_dir(cik) / f"{as_of.isoformat()}.json.gz"
    if raw.exists():
        return raw
    r = common.sec_get(ctx, url(cik), missing=(404,))
    if r is None:
        base.write_raw_json(cik_dir(cik) / f"{as_of.isoformat()}.absent.json",
                            {"cik": cik, "as_of": as_of.isoformat(), "fetched_at": time.time(), "run_id": ctx.run_id})
        return None
    try:
        body = r.json()
    except ValueError as e:
        raise base.SourceError(f"sec companyfacts {cik}: the answer is not JSON") from e
    if not isinstance(body, dict) or not isinstance(body.get("facts"), dict):
        raise base.SourceError(f"sec companyfacts {cik}: not the documented shape")
    held = _accessions(body)
    meta = {"cik": cik, "as_of": as_of.isoformat(), "fetched_at": time.time(), "run_id": ctx.run_id, "url": url(cik),
            "accessions": sorted(held), "wanted": sorted(wanted), "missing": sorted(set(wanted) - held)}
    base.write_raw_json(common.meta_path(raw), meta)
    return base.write_raw_json(raw, body)


class CompanyFacts:
    name = NAME
    kind = KIND
    providers = (common.SEC,)
    tables: tuple[str, ...] = ()
    cadence = "daily"
    backfill_needs_from = False
    starts_by_hand = True

    def default_as_of(self) -> date:
        return calendar.today_ny()

    def fetch(self, ctx: base.RunContext, as_of: date) -> list[Path]:
        companies = common.ciks(common.universe())
        if not companies:
            ctx.notes.append("no Tier A companies yet (no spine file)")
            return []
        tier_a = set(companies)
        wanted: dict[str, set[str]] = {}
        days = sec_index.lookback(ctx, NAME, as_of, lambda: min(filter(None, map(_newest_read, companies)),
                                                                default=None))
        for f in sec_index.filings(ctx, as_of, forms=set(facts.STATEMENT_FORMS), ciks=tier_a, lookback=days):
            wanted.setdefault(f["cik"], set()).add(f["accession"])
        due = {cik: accs - _held(cik) for cik, accs in wanted.items()}
        due = {cik: accs for cik, accs in due.items() if accs}
        new = [c for c in companies if c not in due and not _raws(c) and not self._recently_absent(c, as_of)]
        out, pending = [], []
        for cik in sorted(due, key=int):
            raw = read_company(ctx, cik, as_of, due[cik])
            if raw is not None:
                out.append(raw)
                missing = base.read_raw_json(common.meta_path(raw)).get("missing") or []
                pending += [f"{cik}:{a}" for a in missing]
        for cik in new[:NEW_PER_RUN]:
            raw = read_company(ctx, cik, as_of)
            if raw is not None:
                out.append(raw)
        ctx.notes.append(f"{len(due)} companies filed a statement, {min(len(new), NEW_PER_RUN)} new companies read"
                         + (f", {len(new) - NEW_PER_RUN} new left for the next run" if len(new) > NEW_PER_RUN else ""))
        if pending:
            ctx.notes.append(f"{len(pending)} accession(s) not in the XBRL API yet, asked again next run: "
                             + ", ".join(pending[:5]))
        sec_index.mark_scanned(ctx, NAME, as_of)
        return out

    @staticmethod
    def _recently_absent(cik: str, as_of: date) -> bool:
        since = _absent_since(cik)
        return since is not None and as_of - since < timedelta(days=ABSENT_DAYS)

    def backfill(self, ctx: base.RunContext, start: date | None, end: date) -> None:
        # The facts answer holds a company's whole history: a backfill reads every company not read yet.
        out = []
        for cik in common.ciks(common.universe()):
            if _raws(cik) or self._recently_absent(cik, end):
                continue
            raw = read_company(ctx, cik, end)
            if raw is not None:
                out.append(raw)
        ctx.notes.append(f"backfill: {len(out)} companies read")
        self.to_parquet_all(ctx, out)

    def to_parquet(self, ctx: base.RunContext, raw: Path) -> list[Path]:
        return self.to_parquet_all(ctx, [raw])

    def to_parquet_all(self, ctx: base.RunContext, raws: list[Path]) -> list[Path]:
        newest: dict[str, Path] = {}
        for raw in raws:
            cik = raw.parent.name
            if cik not in newest or raw.name > newest[cik].name:
                newest[cik] = raw
        out = []
        dropped = 0
        for cik, raw in sorted(newest.items()):
            meta = base.read_raw_json(common.meta_path(raw))
            rows, company, counts = facts.fact_rows(base.read_raw_json(raw), cik)
            dropped += counts.get("no_provenance", 0)
            stamps = {"preliminary": False, "source": facts.SOURCE, "source_as_of": date.fromisoformat(meta["as_of"]),
                      "fetched_at": float(meta["fetched_at"]), "run_id": meta["run_id"]}
            folder = common.parquet_dir(NAME) / f"cik={cik}"
            con = duckdb.connect()
            try:
                base.rows_table(con, "rows", facts.FACT, [{**r, **stamps} for r in rows])
                out.append(base.write_parquet(con, "SELECT * FROM rows ORDER BY field, period_type, period_end",
                                              folder / "part-0.parquet"))
                base.rows_table(con, "company", facts.COMPANY, [{
                    **company, "facts_as_of": stamps["source_as_of"], "fetched_at": stamps["fetched_at"],
                    "run_id": stamps["run_id"]}])
                out.append(base.write_parquet(con, "SELECT * FROM company", folder / "company.parquet"))
            finally:
                con.close()
        if dropped:
            ctx.notes.append(f"{dropped} value(s) without an accession left out")
        return out

    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        folder = common.parquet_dir(NAME)
        common.stage_glob(con, STAGING["facts"], facts.FACT, folder, "cik=*/part-0.parquet")
        common.stage_glob(con, STAGING["company"], facts.COMPANY, folder, "cik=*/company.parquet")
        return {}

    def check(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict:
        cols = ["cik", "field", "period_type", "period_end", "value", "accn"]
        added, removed = common.compare_live(con, f"SELECT {', '.join(cols)} FROM fundamental_fact "
                                                  f"WHERE source = '{facts.SOURCE}'", "fundamental_fact", cols,
                                             where=f"source = '{facts.SOURCE}'")
        as_of = con.execute(f"SELECT max(facts_as_of) FROM {STAGING['company']}").fetchone()[0]
        fresh = freshness(con, ctx.as_of or self.default_as_of())
        ctx.notes.append(f"statements <= {STALE_DAYS} days: {fresh['fresh']} of {fresh['quarterly']} quarterly "
                         f"filers; annual-only filers {fresh['annual_fresh']} of {fresh['annual']} within "
                         f"{ANNUAL_STALE_DAYS} days; {fresh['no_facts']} Tier A companies with no statement")
        return {"dataset": NAME, "result": "changed" if added or removed else "unchanged",
                "source_as_of": (as_of or ctx.as_of or self.default_as_of()).isoformat(),
                "rows": common.count(con, STAGING["facts"]), "added": added, "removed": removed,
                "coverage": fresh["coverage"]}


ANNUAL_STALE_DAYS = 400


def freshness(con: duckdb.DuckDBPyConnection, as_of: date) -> dict:
    """The "statement older than 120 days" measure over the served companies (``fundamental_company``).

    A company's statement age is the days from its newest statement filing (the newest ``filed`` of a non-
    preliminary fact) to ``as_of``. A quarterly filer (one with a quarterly fact) is fresh within ``STALE_DAYS``;
    an annual-only filer (a 20-F or 40-F filer, which files no quarterly statement) is counted on its own, within
    ``ANNUAL_STALE_DAYS``. ``coverage`` is the fresh share of the quarterly filers (the plan's target: >= 95%).
    """
    row = con.execute(f"""
        WITH per AS (
            SELECT c.cik, max(f.filed) AS newest,
                   bool_or(f.period_type = 'quarterly') AS quarterly
            FROM (SELECT DISTINCT cik FROM fundamental_company) c
            LEFT JOIN fundamental_fact f ON f.cik = c.cik AND NOT f.preliminary
            GROUP BY c.cik)
        SELECT count(*) FILTER (WHERE quarterly),
               count(*) FILTER (WHERE quarterly AND newest >= DATE '{as_of.isoformat()}' - {STALE_DAYS}),
               count(*) FILTER (WHERE NOT quarterly),
               count(*) FILTER (WHERE NOT quarterly AND newest >= DATE '{as_of.isoformat()}' - {ANNUAL_STALE_DAYS}),
               count(*) FILTER (WHERE newest IS NULL)
        FROM per""").fetchone()
    quarterly, fresh, annual, annual_fresh, no_facts = (int(x or 0) for x in row)
    return {"quarterly": quarterly, "fresh": fresh, "annual": annual, "annual_fresh": annual_fresh,
            "no_facts": no_facts, "coverage": round(fresh / quarterly, 4) if quarterly else None}


DATASET = CompanyFacts()
