"""Corporate-event flag (#386 F5): the 8-K items that break the comparability of a filer's fundamentals.

``sec_submissions`` (edgar.duckdb) holds every filing of every universe CIK, written by the daily ``edgar_sync`` job
(``core/sec_submissions.record``). This reader turns the 8-Ks of the last ``config.CORPORATE_EVENT_WINDOW_DAYS``
into one flag per ticker: the item codes of ``config.CORPORATE_EVENT_ITEMS`` a filer reported, and the date of its
newest such filing.

**On the fundamentals layer** (``core.fundamentals_layer.reads_layer()``, review F2, G6) the same answer comes from
``fundamentals.duckdb``: the CIK from ``fundamental_company``, the 8-K items from ``filing_item`` (``sec/filings``),
and a CIK's checked day from its list's read day (``submissions_as_of``) or a later good ``sec/filings`` run
(``_layer_reads``). The old rows are not read on that path.

**Display only.** Nothing here is scored, ranked, bucketed or frozen: the flag is not in ``freeze._HASHED`` and
changes no ``ledger.model_version``.

**Unknown is not false.** A ticker with no CIK, no filings or no qualifying item is absent from the result, never a
positive "no event" answer; a board that cannot see itself in the store says nothing. The caller that wants the
blind spot counted asks for it: ``stats["no_submissions"]`` is the number of candidates whose CIK has no filing row
at all, of any form, ``stats["no_cik"]`` the number the store cannot map to a CIK in the first place, and
``stats["stale_submissions"]`` (review F2) the number whose rows the daily sync last checked more than
``config.CORPORATE_EVENT_CHECK_MAX_AGE_DAYS`` ago, or never (its ``source_checks`` watermark).

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

from datetime import date, datetime, timedelta, timezone

from RevenueChaser.core import fundamentals_layer, rawstore, sec_submissions

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


def _store_reads(wanted: list[str], since: date, *, counted: bool):
    """The old store's answer: ``(cik_of, rows, stored, checked)``. ``cik_of`` maps a ticker to its CIK
    (``security_master``), ``rows`` are the 8-Ks filed since ``since`` (``sec_submissions``), ``stored`` the CIKs with
    any filing row and ``checked`` the day the daily sync last checked each (``source_checks``); the last two only
    when ``counted``."""
    cik_of: dict[str, str] = {}
    rows: list[dict] = []
    stored: set[str] = set()
    checked: dict[str, date] = {}
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
        ciks = sorted(set(cik_of.values()))
        for chunk in _slices(ciks):
            rows += rawstore.query(
                "SELECT cik, items, filing_date FROM sec_submissions "
                f"WHERE form = '8-K' AND filing_date >= ? AND cik IN ({_placeholders(len(chunk))})",
                [since, *chunk], db="edgar")
        if counted:
            for chunk in _slices(ciks):
                for row in rawstore.query(
                        f"SELECT DISTINCT cik FROM sec_submissions WHERE cik IN ({_placeholders(len(chunk))})",
                        chunk, db="edgar"):
                    cik = _norm_cik(row.get("cik"))
                    if cik:
                        stored.add(cik)
            # Review F2: the sync's own watermark per CIK (``source_checks``, written in raw by
            # ``sec_submissions.record``). Rows that stopped being refreshed cannot show a recent 8-K.
            for chunk in _slices(sorted(stored)):
                for row in rawstore.query(
                        f"SELECT key, max(checked_at) AS at FROM {sec_submissions.CHECKS} WHERE dataset = ? "
                        f"AND source = ? AND key IN ({_placeholders(len(chunk))}) GROUP BY key",
                        [sec_submissions.SOURCE, sec_submissions.SOURCE, *chunk]):
                    cik = _norm_cik(row.get("key"))
                    if cik and row.get("at") is not None:
                        day = datetime.fromtimestamp(float(row["at"]), timezone.utc).date()
                        checked[cik] = max(checked.get(cik, day), day)
    return cik_of, rows, stored, checked


def _layer_reads(wanted: list[str], since: date, *, counted: bool):
    """The fundamentals layer's answer, in ``_store_reads``'s shape (review F2, G6): ``fundamental_company`` maps a
    ticker to its CIK, ``filing_item`` holds the 8-K items ``sec/filings`` listed, and a CIK is stored once its list
    was read (``submissions_as_of``). Its checked day is that read, or the day of the newest good ``sec/filings``
    run when later: that run read the daily index and re-read the list of every company that filed, so a quiet
    filer's list is current through it.

    shortcut: the run's check date stands in for the day its daily-index scan reached, and a backfill run (which
    reads no index) counts too; serve the scan day per run if the stale count must be exact."""
    from RevenueChaser.rcdata import ops
    from RevenueChaser.rcdata import read as rc_read
    from RevenueChaser.rcdata.build.fundamentals import filings
    cik_of: dict[str, str] = {}
    listed: dict[str, date] = {}
    for chunk in _slices(wanted):
        for row in rc_read.fundamental_companies(chunk):
            cik = _norm_cik(row.get("cik"))
            if cik and row.get("ticker"):
                cik_of[row["ticker"]] = cik
                if row.get("submissions_as_of") is not None:
                    listed[cik] = date.fromisoformat(str(row["submissions_as_of"])[:10])
    by_accession: dict[tuple, dict] = {}
    for chunk in _slices(sorted(set(cik_of.values()))):
        for row in rc_read.filing_items(chunk, "8-K", since):
            rec = by_accession.setdefault((row["cik"], row["accession"]),
                                          {"cik": row["cik"], "filing_date": row.get("filing_date"), "items": []})
            if row.get("item"):
                rec["items"].append(str(row["item"]))
    rows = [{**r, "items": ",".join(r["items"])} for r in by_accession.values()]
    checked: dict[str, date] = {}
    if counted and listed:
        good = ops.latest_good_check(filings.NAME)
        run_day = date.fromisoformat(str(good["checked_on"])[:10]) if good else None
        checked = {cik: max(day, run_day) if run_day else day for cik, day in listed.items()}
    return cik_of, rows, set(listed), checked


def corporate_events(tickers, *, today=None, stats: dict | None = None) -> dict[str, dict]:
    """``{ticker: {"items": [codes, sorted], "last_filing_date": "YYYY-MM-DD"}}`` for tickers that have one.

    A ticker with no CIK, no row, or no qualifying filing is simply absent from the result. ``stats``, when given,
    receives ``{"no_submissions": n, "no_cik": m}``: the candidates whose CIK has no ``sec_submissions`` row at all,
    and the candidates the store cannot give a CIK to.
    """
    if stats is not None:
        stats["no_submissions"] = 0
        stats["no_cik"] = 0
        stats["stale_submissions"] = 0
    today = _today() if today is None else today
    if not isinstance(today, date):
        today = date.fromisoformat(str(today)[:10])
    wanted = list(dict.fromkeys(tickers or []))
    if not wanted:
        return {}

    since = today - timedelta(days=config.CORPORATE_EVENT_WINDOW_DAYS)
    read = _layer_reads if fundamentals_layer.reads_layer() else _store_reads
    cik_of, rows, stored, checked = read(wanted, since, counted=stats is not None)
    by_cik: dict[str, list[str]] = {}
    for ticker in wanted:
        cik = cik_of.get(ticker)
        if cik:
            by_cik.setdefault(cik, []).append(ticker)
    if stats is not None:
        # Two blind spots, counted apart: no CIK at all, and a CIK the filings do not mention. A candidate of the
        # first kind was counted nowhere before, so a card that says nothing read as a clean one (#386 F5 review).
        stats["no_cik"] = sum(1 for ticker in wanted if ticker not in cik_of)
        stats["no_submissions"] = sum(len(names) for cik, names in by_cik.items() if cik not in stored)
        # Review F2: a CIK with rows whose newest check is older than the line, or that was never checked.
        line = today - timedelta(days=config.CORPORATE_EVENT_CHECK_MAX_AGE_DAYS)
        stats["stale_submissions"] = sum(
            len(names) for cik, names in by_cik.items()
            if cik in stored and (cik not in checked or checked[cik] < line))

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
