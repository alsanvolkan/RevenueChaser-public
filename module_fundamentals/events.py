"""Corporate-event flag (#386 F5): the 8-K items that break the comparability of a filer's fundamentals.

``sec_submissions`` (edgar.duckdb) holds every filing of every universe CIK, written by the daily ``edgar_sync`` job
(``core/sec_submissions.record``). This reader turns the 8-Ks of the last ``config.CORPORATE_EVENT_WINDOW_DAYS``
into one flag per ticker: the item codes of ``config.CORPORATE_EVENT_ITEMS`` a filer reported, and the date of its
newest such filing.

**Display only.** Nothing here is scored, ranked, bucketed or frozen: the flag is not in ``freeze._HASHED`` and
changes no ``ledger.model_version``.

**Unknown is not false.** A ticker with no CIK, no filings or no qualifying item is absent from the result, never a
positive "no event" answer; a board that cannot see itself in the store says nothing. The caller that wants the
blind spot counted asks for it: ``stats["no_submissions"]`` is the number of candidates whose CIK has no filing row
at all, of any form, and ``stats["no_cik"]`` the number the store cannot map to a CIK in the first place.

**Bounded reads.** Production passes no ``top_n``, so the candidate list is unbounded and grows with the screen
(``pipeline.build_payload``). Every store read therefore walks its keys in slices of ``CHUNK``: the statement and its
``IN (...)`` list stay the same size whatever the board's universe.

**One session.** All of those reads run inside one ``rawstore.session()``: a job's worker and the scheduled CLI
Fetch have no outer request session, so without it the 500 MB file is opened once per statement (the review priced
that at 9.5 s of connects on a 4,000-candidate screen). ``session()`` is re-entrant, so the dashboard's per-request
session is shared rather than nested.

Read-only store reads, no write and no network call; a missing table read answers empty, like an empty store.
"""
from __future__ import annotations

from datetime import date, timedelta

from RevenueChaser.core import rawstore, sec_submissions

from . import config

# Keys per store read (#386 F5 review): one constant for all three reads, so the number of candidates a Fetch or a
# Refresh has to cover changes the number of statements, never the size of one. 2000 keeps the ``IN (...)`` list
# small; with one session around the reads, the slice count is the only thing that grows.
CHUNK = 2000


def _today():
    """The module's one calendar seam: the pipeline's ``_today``, which a test pins for the whole board."""
    from . import pipeline
    return pipeline._today()


def _norm_cik(v) -> str | None:
    """``core.sec_submissions.norm_cik``, or None for a value that is not a CIK. ``'0001234'`` and ``'1234'`` both
    become ``'1234'``; a non-numeric or zero value is no CIK, so the ticker stays unknown rather than clean."""
    try:
        cik = sec_submissions.norm_cik(v)
    except (TypeError, ValueError):
        return None
    return cik if cik != "0" else None


def _slices(keys: list[str]):
    """``keys`` in slices of at most ``CHUNK``: one bounded store read per slice."""
    for i in range(0, len(keys), CHUNK):
        yield keys[i:i + CHUNK]


def _placeholders(n: int) -> str:
    return ", ".join(["?"] * n)


def _items(raw) -> set[str]:
    """The item codes of a filing's ``items`` text column: ``'2.02,9.01'`` -> ``{'2.02', '9.01'}``."""
    return {part.strip() for part in str(raw or "").split(",") if part.strip()}


def corporate_events(tickers, *, today=None, stats: dict | None = None) -> dict[str, dict]:
    """``{ticker: {"items": [codes, sorted], "last_filing_date": "YYYY-MM-DD"}}`` for tickers that have one.

    A ticker with no CIK, no row, or no qualifying filing is simply absent from the result. ``stats``, when given,
    receives ``{"no_submissions": n, "no_cik": m}``: the candidates whose CIK has no ``sec_submissions`` row at all,
    and the candidates the store cannot give a CIK to.
    """
    if stats is not None:
        stats["no_submissions"] = 0
        stats["no_cik"] = 0
    today = _today() if today is None else today
    if not isinstance(today, date):
        today = date.fromisoformat(str(today)[:10])
    wanted = list(dict.fromkeys(tickers or []))
    if not wanted:
        return {}

    since = today - timedelta(days=config.CORPORATE_EVENT_WINDOW_DAYS)
    cik_of = {}
    by_cik = {}
    rows = []
    stored: set[str] = set()
    # One handle per store file for all of it (#386 F5 review): no outer session exists in a job worker or the
    # scheduled CLI Fetch, and a per-statement connect would open the 500 MB file once per slice.
    with rawstore.session():
        for chunk in _slices(wanted):
            for row in rawstore.query(
                    f"SELECT ticker, cik FROM security_master WHERE ticker IN ({_placeholders(len(chunk))}) "
                    "AND cik IS NOT NULL AND cik <> ''", chunk, db="raw"):
                cik = _norm_cik(row.get("cik"))
                if cik and row.get("ticker"):
                    cik_of[row["ticker"]] = cik
        for ticker in wanted:
            cik = cik_of.get(ticker)
            if cik:
                by_cik.setdefault(cik, []).append(ticker)
        ciks = list(by_cik)
        for chunk in _slices(ciks):
            rows += rawstore.query(
                "SELECT cik, items, filing_date FROM sec_submissions "
                f"WHERE form = '8-K' AND filing_date >= ? AND cik IN ({_placeholders(len(chunk))})",
                [since, *chunk], db="edgar")
        if stats is not None:
            for chunk in _slices(ciks):
                for row in rawstore.query(
                        f"SELECT DISTINCT cik FROM sec_submissions WHERE cik IN ({_placeholders(len(chunk))})",
                        chunk, db="edgar"):
                    cik = _norm_cik(row.get("cik"))
                    if cik:
                        stored.add(cik)
    if stats is not None:
        # Two blind spots, counted apart: no CIK at all, and a CIK the filings do not mention. A candidate of the
        # first kind was counted nowhere before, so a card that says nothing read as a clean one (#386 F5 review).
        stats["no_cik"] = sum(1 for ticker in wanted if ticker not in cik_of)
        stats["no_submissions"] = sum(len(names) for cik, names in by_cik.items() if cik not in stored)

    wanted_items = set(config.CORPORATE_EVENT_ITEMS)
    found: dict[str, dict] = {}
    for row in rows:
        hit = _items(row.get("items")) & wanted_items
        filed = str(row.get("filing_date") or "")[:10]
        if not hit or not filed:
            continue
        for ticker in by_cik.get(_norm_cik(row.get("cik")), ()):
            ev = found.setdefault(ticker, {"items": set(), "last_filing_date": ""})
            ev["items"] |= hit
            ev["last_filing_date"] = max(ev["last_filing_date"], filed)
    return {t: {"items": sorted(ev["items"]), "last_filing_date": ev["last_filing_date"]}
            for t, ev in found.items()}
