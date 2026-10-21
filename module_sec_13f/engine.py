"""SEC Form 13F bulk data — authoritative total institutional ownership.

The Unusual Whales ``/institution/{ticker}/ownership`` endpoint omits the
largest passive managers (BlackRock, Vanguard Group, State Street, ...). The
SEC 13F data sets (https://www.sec.gov/dera/data/form-13f) contain *every*
13F filer's holdings, keyed by CUSIP. The Fetch runs in three stages:

1. **download** (network): ``download_missing_zips`` pulls the data-set zips the
   SEC lists and the store has not ingested yet;
2. **ingest** (zip -> store): ``ingest_zips`` streams each zip into
   ``sec13f_holdings`` / ``sec13f_submissions`` / ``sec13f_cusip_names``;
3. **derive** (store only): ``recompute_totals`` maps CUSIP -> ticker through the
   CUSIP map and rebuilds ``sec13f_totals``. It never touches the network, so a fix to
   ``compute_totals`` shows up without a download.

The CUSIP map (#207, D19) comes from OpenFIGI: the Fetch's map step (``app/runners``)
asks about the CUSIPs ``map_candidates`` lists, ``core/identity_cusip`` keeps the
answers, and ``cusip_ticker`` is rebuilt from them when a derive with the new map
passes the guards. A legacy entry OpenFIGI does not map stays only when OpenFIGI
confirms its issuer prefix for the same ticker and its issue number is not 9x
(``legacy_fallback``, option B minus 9x). The old issuer-name match is retired: a
map is never guessed.

Data set layout (per ZIP):
  SUBMISSION.tsv    ACCESSION_NUMBER, FILING_DATE, SUBMISSIONTYPE, CIK, PERIODOFREPORT
  COVERPAGE.tsv     ACCESSION_NUMBER, REPORTCALENDARORQUARTER, ISAMENDMENT,
                    AMENDMENTNO, AMENDMENTTYPE, REPORTTYPE, ...
  SUMMARYPAGE.tsv   ACCESSION_NUMBER, OTHERINCLUDEDMANAGERSCOUNT, TABLEENTRYTOTAL,
                    TABLEVALUETOTAL, ISCONFIDENTIALOMITTED
  INFOTABLE.tsv     ACCESSION_NUMBER, INFOTABLE_SK, NAMEOFISSUER, CUSIP, VALUE,
                    SSHPRNAMT, SSHPRNAMTTYPE (SH/PRN), PUTCALL, TITLEOFCLASS,
                    INVESTMENTDISCRETION, OTHERMANAGER, VOTING_AUTH_*, ...

Every INFOTABLE row is stored (non-lossy), including puts, PRNs and non-equity
rows. The derive filters long equity (``SSHPRNAMTTYPE == "SH"`` and no
``PUTCALL``) at read time. Amendments are resolved by walking filings per
(CIK, PERIODOFREPORT): a ``13F-HR/A`` of type ``RESTATEMENT`` replaces the active
set, one of type ``NEW HOLDINGS`` unions onto it, and any other ``/A`` (or a
missing/unknown ``AMENDMENTTYPE``) replaces conservatively.

N-1: a zip republished by the SEC keeps the old rows because the (accession,
line_no) key uses ``ON CONFLICT DO NOTHING``. The v12 migration script is the
intended way to rebuild the table from the zips on disk.
"""
import csv
import hashlib
import io
import os
import logging
import zipfile
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional


from RevenueChaser.core import prices, rawstore
from RevenueChaser.core.config import OUTPUT_DIR
# Sector classification lives in one place now (core.gics_map); re-exported here
# because older import sites still read these names from this module.
from RevenueChaser.core.gics_map import SECTORS_11, canonical_sector

logger = logging.getLogger(__name__)

# The downloaded *_form13f.zip archives are regenerable SOURCE MATERIAL (they
# can be re-downloaded from SEC and are deletable after ingest) — they live in
# the clearable .cache/, not in the durable .store/. All parsed data goes to
# .store/raw.duckdb.
SEC13F_DIR = OUTPUT_DIR / "sec_13f"
OWNERSHIP_DIR = SEC13F_DIR

# The SEC's canonical Form 13F data-set page. Its links are site-relative
# (``/files/structureddata/data/form-13f-data-sets/<name>``); each zip is
# downloaded from the link's own path resolved against this page — the old
# ``/dera/data/form-13f/<name>`` guess returns 404. The SEC form-13f zips are the
# only trusted 13F source (see .hermes/plans_claude/archive/2026-09-30_ownership.md).
FORM13F_INDEX_URL = (os.getenv("RC_FORM13F_INDEX_URL")
                     or "https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets")

# Quarters at or after this date are considered "complete" (older periods in the
# data sets are stray amendments and are ignored).
START_QUARTER = "2025-03-31"

FILING_LAG_DAYS = 45  # 13F filings are due 45 days after quarter-end

_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def parse_zip_index(html: str, index_url: str = None) -> dict[str, str]:
    """``{zip name: absolute download URL}`` from the data-set page's links.

    The URL is the link's own href resolved against the page (site-relative
    ``/files/...`` links included), never a guessed directory.
    """
    import re
    from urllib.parse import urljoin
    base = index_url or FORM13F_INDEX_URL
    out: dict[str, str] = {}
    for href in re.findall(r"""href\s*=\s*["']([^"']*?_form13f\.zip)["']""", html, flags=re.I):
        name = href.rsplit("/", 1)[-1]
        out.setdefault(name, urljoin(base, href))
    return dict(sorted(out.items()))


def available_zips() -> dict[str, str]:
    """``{zip name: URL}`` currently listed by the SEC (one throttled request)."""
    from RevenueChaser.core.providers import sec_edgar
    return parse_zip_index(sec_edgar.get_text(FORM13F_INDEX_URL), FORM13F_INDEX_URL)


def list_available_zips() -> list[str]:
    """Names of the ``*_form13f.zip`` data sets the SEC lists (sorted)."""
    return list(available_zips())


def zip_window_end(name: str) -> date | None:
    """Last filing date a data-set zip can contain, from its name.

    Legacy quarterly sets are ``2024q4_form13f.zip`` (quarter-end); newer ones are
    filing windows ``01jun2026-31aug2026_form13f.zip`` (window end). None when the
    name has neither shape.
    """
    import re
    stem = name.lower().removesuffix("_form13f.zip")
    m = re.fullmatch(r"(\d{4})q([1-4])", stem)
    if m:
        year, q = int(m.group(1)), int(m.group(2))
        return date(year, 3 * q, (31, 30, 30, 31)[q - 1])
    m = re.fullmatch(r"\d{1,2}[a-z]{3}\d{4}-(\d{1,2})([a-z]{3})(\d{4})", stem)
    if m and m.group(2).upper() in _MONTHS:
        return date(int(m.group(3)), _MONTHS[m.group(2).upper()], int(m.group(1)))
    return None


def missing_zips(listed: dict[str, str], ownership_dir: Path = None) -> dict[str, str]:
    """Listed zips worth downloading: not ingested, not on disk, and recent enough.

    A zip is skipped when ``sec13f_ingested_zips`` already records it (the file
    may have been deleted after ingest — re-downloading every set would pull ~4 GB),
    when it is already on disk, or when its filing window ends on or before
    ``START_QUARTER`` (nothing in it can be published).
    """
    ownership_dir = ownership_dir or OWNERSHIP_DIR
    ingested = {r["filename"] for r in rawstore.query("SELECT filename FROM sec13f_ingested_zips")}
    on_disk = {p.name for p in ownership_dir.glob("*_form13f.zip")} if ownership_dir.exists() else set()
    start = date.fromisoformat(START_QUARTER)
    out = {}
    for name, url in listed.items():
        if name in ingested or name in on_disk:
            continue
        end = zip_window_end(name)
        if end is not None and end <= start:
            continue
        out[name] = url
    return out


def check_new_zips(ownership_dir: Path = None) -> dict:
    """What the SEC lists vs what is ingested — downloads nothing.

    ``{"latest": newest listed name or None, "new": [names that a Fetch would
    download], "listed": count}``.
    """
    listed = available_zips()
    new = missing_zips(listed, ownership_dir)
    latest = max(listed, key=lambda n: (zip_window_end(n) or date.min, n)) if listed else None
    return {"latest": latest, "new": sorted(new, key=lambda n: (zip_window_end(n) or date.min, n)),
            "listed": len(listed)}


def download_missing_zips(ownership_dir: Path = None) -> list[Path]:
    """Download the listed zips that ``missing_zips`` selects; return their paths.

    Goes through the shared SEC client (throttle, configured User-Agent,
    retries). Each file is written as ``<name>.part`` and renamed only after
    it reads as a valid zip, so an interrupted download never leaves a truncated
    ``*_form13f.zip`` that every later build would choke on.
    """
    import zipfile
    from RevenueChaser.core.providers import sec_edgar
    ownership_dir = ownership_dir or OWNERSHIP_DIR
    ownership_dir.mkdir(parents=True, exist_ok=True)
    downloaded = []
    for name, url in missing_zips(available_zips(), ownership_dir).items():
        logger.info("Downloading 13F zip: %s", name)
        target = ownership_dir / name
        part = target.with_name(name + ".part")
        part.write_bytes(sec_edgar.get_bytes(url))
        if not zipfile.is_zipfile(part):
            part.unlink(missing_ok=True)
            raise RuntimeError(f"13F download {name} is not a valid zip")
        os.replace(part, target)
        downloaded.append(target)
    return downloaded


def _quarter_end_before(d) -> date:
    """Latest quarter-end strictly before ``d``."""
    latest = date(d.year - 1, 12, 31)
    for m, day in ((3, 31), (6, 30), (9, 30), (12, 31)):
        qe = date(d.year, m, day)
        if qe < d:
            latest = qe
    return latest


def _add_quarter(qe: date) -> date:
    m = qe.month + 3
    y = qe.year
    if m > 12:
        m -= 12
        y += 1
    nxt = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    return nxt - timedelta(days=1)


def quarter_ends(start: str = START_QUARTER, today: date = None) -> list:
    """Quarter-end ISO dates from ``start`` through the latest completed quarter."""
    start_d = date.fromisoformat(start)
    end_d = _quarter_end_before(today or date.today())
    out = []
    d = start_d
    while d <= end_d:
        out.append(d.isoformat())
        d = _add_quarter(d)
    return out


def quarter_label(qe: str) -> str:
    d = date.fromisoformat(qe)
    return f"{d.year} Q{(d.month - 1) // 3 + 1}"


def expected_report_date(today: date = None) -> str:
    """Latest quarter-end whose 13F filings are due by now (45-day lag)."""
    d = today or date.today()
    due = d - timedelta(days=FILING_LAG_DAYS)
    return _quarter_end_before(due + timedelta(days=1)).isoformat()


def release_schedule(n: int = 8, today: date = None) -> list:
    """Upcoming 13F release dates (45-day filing deadline) for the next ``n`` quarters."""
    d = today or date.today()
    qe = _add_quarter(_quarter_end_before(d))
    out = []
    for _ in range(n):
        due = qe + timedelta(days=FILING_LAG_DAYS)
        out.append({
            "report_period": quarter_label(qe.isoformat()),
            "quarter_end": qe.isoformat(),
            "filing_due": due.isoformat(),
            "weekday": due.strftime("%A"),
        })
        qe = _add_quarter(qe)
    return out


def load_sec13f_meta() -> dict:
    """Cached ``{ticker: {"name": str, "sector": str, "type": str}}`` for the full 13F universe."""
    rows = rawstore.get_all("sec13f_security_meta")
    return {r["ticker"]: (r.get("entry") or {}) for r in rows}


def save_sec13f_meta(meta: dict):
    rawstore.write_rows("sec13f_security_meta", [
        {"ticker": t, "entry": v} for t, v in meta.items()
    ])


def normalize_period(raw: str) -> Optional[str]:
    """``31-MAR-2026`` -> ``2026-03-31`` (None if unparseable)."""
    if not raw:
        return None
    raw = raw.strip()
    for fmt in ("%d-%b-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def normalize_cusip(raw: str) -> Optional[str]:
    c = (raw or "").strip().upper()
    if not c or c in ("N/A", "NA", "NONE", "000000000"):
        return None
    return c


def _find_member(z: zipfile.ZipFile, name: str) -> Optional[str]:
    """Return the archive member name ending in ``name`` (ZIPs may nest files).

    Match is case-insensitive so a nested or upper-cased member (e.g.
    ``01JUN2025-31AUG2025_form13f/COVERPAGE.tsv``) is found the same way as a
    flat lower-case one.
    """
    up = name.upper()
    for m in z.namelist():
        if m.upper().endswith(up):
            return m
    return None


def filing_rank(filing_date: str, filing_type: str, accession: str) -> tuple:
    """Deterministic ordering key for the latest 13F-HR per (cik, period).

    A later ``filing_date`` wins. On a tie — an original and its amendment can
    share a filing date — the amendment must supersede the original, so prefer
    ``13F-HR/A`` over ``13F-HR``; the accession is the final tiebreak so the
    choice never depends on query row order.
    """
    return (filing_date or "", (filing_type or "").endswith("/A"), accession or "")


def load_cusip_ticker() -> dict:
    rows = rawstore.get_all("cusip_ticker")
    return {r["cusip"]: (r.get("entry") or {}) for r in rows}


def save_cusip_ticker(mapping: dict) -> None:
    """Make ``mapping`` the whole ``cusip_ticker`` (rows of CUSIPs not in it are deleted).

    ``cusip_ticker`` is a cache of the ``identity_cusip`` map (#207): ``recompute_totals`` replaces it once a derive
    with the new map passes the guards. An empty mapping raises (``replace_all``) rather than wipe the table.
    """
    rawstore.replace_all("cusip_ticker", [{"cusip": c, "entry": v} for c, v in sorted(mapping.items())])


def ticker_of(info) -> str:
    """The ticker of a ``cusip_ticker`` entry: a legacy string (``"RTX"``) or a dict with ``ticker``."""
    return info.upper() if isinstance(info, str) else ((info or {}).get("ticker") or "").upper()


LEGACY = "legacy"          # the ``source`` of a legacy entry the prefix fallback keeps
_PREFIX = 6                # a CUSIP's issuer prefix: its first six characters
# B-strict (#427 review): also drop a legacy ticker whose prefix OpenFIGI maps to more than one ticker. Off: avo's
# card (2026-10-15, "9x'leri at") keeps multi-ticker prefixes (GOOGL's classes) and drops only 9x issue numbers.
_FALLBACK_STRICT = False


def option_like(cusip: str) -> bool:
    """Whether a CUSIP's issue number (characters 7-8) is 90-99, the range 13F filers' call and put CUSIPs use."""
    return len(cusip or "") >= 8 and cusip[6] == "9"


def legacy_fallback(openfigi_map: dict, stored_map: dict, ever_mapped: Iterable[str] = (), *,
                    strict: bool = False) -> tuple[dict, dict]:
    """The new ``cusip_ticker``: OpenFIGI's map plus the legacy tickers its issuer prefixes confirm (avo's option B
    minus 9x, #207 decision cards 2026-10-15). Pure, no store access.

    A stored entry is a fallback candidate when OpenFIGI has no ticker for its CUSIP now and never had one
    (``ever_mapped``: a CUSIP OpenFIGI once mapped and then dropped stays out). The candidate keeps its ticker, as
    ``{"ticker", "source": "legacy"}``, only when OpenFIGI maps another CUSIP with the same first six characters to
    that same ticker and its issue number is not 90-99 (GOOGL's classes, BA, ORCL). It is dropped when OpenFIGI
    maps the prefix only to other tickers (the fund-CUSIP glue: 25434V609 legacy DFAT, prefix DEHP/DEXC/DFAE), not
    at all (G54950103 legacy HEI is Linde), or when the issue number is 9x (:func:`option_like`: 02079K909 is a
    GOOGL call, not the stock). ``strict`` (B-strict) also drops a candidate whose prefix OpenFIGI maps to more
    than one ticker. No hand overrides here. Kept entries are candidates again on the next run, so the map is
    stable.

    Returns ``(cusip_map, out)``; ``out`` lists the candidate CUSIPs, sorted, under ``kept``, ``other_ticker``,
    ``unknown_prefix``, ``option_like`` and ``strict_dropped`` (empty unless ``strict``).
    """
    known = set(ever_mapped) | set(openfigi_map)
    by_prefix: dict[str, set] = {}
    for c, info in openfigi_map.items():
        t = ticker_of(info)
        if t:
            by_prefix.setdefault(c[:_PREFIX].upper(), set()).add(t)
    cusip_map = dict(openfigi_map)
    out: dict[str, list] = {"kept": [], "other_ticker": [], "unknown_prefix": [], "option_like": [],
                            "strict_dropped": []}
    for c, info in sorted(stored_map.items()):
        t = ticker_of(info)
        if not t or c in known:
            continue
        tickers = by_prefix.get(c[:_PREFIX].upper())
        if not tickers:
            out["unknown_prefix"].append(c)
        elif t not in tickers:
            out["other_ticker"].append(c)
        elif option_like(c):
            out["option_like"].append(c)
        elif strict and len(tickers) > 1:
            out["strict_dropped"].append(c)
        else:
            cusip_map[c] = {"ticker": t, "source": LEGACY}
            out["kept"].append(c)
    return cusip_map, out


def save_totals(totals: dict):
    """Full refresh — ``compute_totals`` returns the complete map, so a ticker
    with no remaining holdings must disappear rather than keep a stale row."""
    rawstore.replace_all("sec13f_totals", [
        {"ticker": t, "entry": periods} for t, periods in totals.items()
    ])
    return True


def load_totals() -> dict:
    rows = rawstore.get_all("sec13f_totals")
    out = {}
    for r in rows:
        entry = r.get("entry")
        if isinstance(entry, dict):
            out[r["ticker"]] = entry
    return out


_NAME_DROP = {
    "INC", "CORP", "CO", "COS", "COMPANY", "COMPANIES", "CORPORATION",
    "INCORPORATED", "LTD", "LLC", "LP", "PLC", "HOLDINGS", "GROUP",
    "CLASS", "CL", "CAP", "STK", "STOCK", "COM", "COMMON", "ORD",
    "ORDINARY", "SHS", "SHARES", "US", "ADR", "A", "B", "C", "D", "E",
}


def normalize_issuer_name(raw: str) -> str:
    """Normalize a 13F issuer name to the ``sec13f_cusip_names.name_norm`` key.

    ``ROCKET COS INC-CLASS A`` -> ``ROCKET``; ``APPLE INC`` -> ``APPLE``. Class letters
    are dropped, so dual-class names (GOOGL/GOOG) collapse to one key: the reason the
    name match it once fed is retired (#207).
    """
    import re
    s = re.sub(r"[^A-Z0-9]+", " ", (raw or "").upper())
    return " ".join(t for t in s.split() if t not in _NAME_DROP)


# ── DuckDB non-lossy ingest store (.store/raw.duckdb) ─────────────────────

def _init_db() -> None:
    """Ensure the sec13f tables (and the CUSIP map's ``identity_cusip``) exist in raw.duckdb."""
    rawstore.ensure_table("sec13f_submissions")
    rawstore.ensure_table("sec13f_holdings")
    rawstore.ensure_table("sec13f_cusip_names")
    rawstore.ensure_table("sec13f_ingested_zips")
    rawstore.ensure_table("identity_cusip")


def _guard_v11_holdings_shape() -> None:
    """Refuse to ingest against the v11 ``sec13f_holdings`` shape (no key).

    Without this, the first ``ON CONFLICT (accession, line_no)`` against an
    un-indexed live table raises a bare DuckDB error. The migration script is
    the only supported path to v12.
    """
    with rawstore.connect() as conn:
        live = rawstore._live_primary_keys(conn, "sec13f_holdings")
    have = live.get("sec13f_holdings", [])
    want = ["accession", "line_no"]
    if have and have != want:
        raise RuntimeError(
            "`.store/raw.duckdb` is at the v11 13F holdings shape; "
            "run `scripts/migrate_sec13f_v12.py` (gated)")


# Holdings are inserted in chunks so a multi-GB quarter never holds the single
# writer lock for minutes at a stretch (a dashboard read would time out).
_HOLDINGS_CHUNK = 100_000

# Derive-side filter: only long equity rows count toward ownership totals.
_LONG_EQUITY_SQL = "h.sshprnamttype = 'SH' AND (h.putcall IS NULL OR h.putcall = '')"

# VALUE-units rescale thresholds (#208). A filer whose median implied price
# (value/shares) is ~1/1000 of the stored D01 close is still reporting VALUE in
# thousands; multiply value by 1000. ``_VALUE_SCALE_MIN_NAMES`` guards one-off
# mappings.
_VALUE_SCALE_BAND = (1.0 / 3000.0, 1.0 / 300.0)
_VALUE_SCALE_MIN_NAMES = 5
# How far back of a period end one prices read looks before falling back to the one bar
# ``daily_bars`` returns from before the window.
_CLOSE_LOOKBACK_DAYS = 10


def _normalize_amendment_type(raw: str) -> Optional[str]:
    """Collapse whitespace and upper-case; return None for empty/unknown."""
    if not raw:
        return None
    return " ".join(raw.upper().split()) or None


def _is_long_equity(row: dict) -> bool:
    """A holdings row counts as long equity for the derive and name counter."""
    return row.get("SSHPRNAMTTYPE") == "SH" and not row.get("PUTCALL")


def ingest_zips(zip_paths: Iterable[str | Path]) -> int:
    """Stream any not-yet-ingested ZIPs into raw.duckdb (returns count).

    Every INFOTABLE row is stored (non-lossy), including puts, PRNs and
    non-equity rows. Long-equity filtering happens at derive time. Each zip is
    marked ingested by filename, so re-running skips already-loaded zips.

    A zip is streamed in ``_HOLDINGS_CHUNK`` row batches, so it is never held in
    memory and the writer lock is never held for a whole quarter. The batches go
    first; the zip's ``sec13f_submissions`` rows, its issuer names and the
    ``ingested_zips`` marker follow in ONE transaction. The derive joins every
    holding through its submission row, so holdings of a zip that died half-way
    are invisible to it.

    Re-ingesting relies on the (accession, line_no) PRIMARY KEY and
    ``ON CONFLICT DO NOTHING``: no per-accession DELETE runs.
    """
    _init_db()
    _guard_v11_holdings_shape()
    ingested = {r["filename"] for r in rawstore.query("SELECT filename FROM sec13f_ingested_zips")}
    new = 0
    now = datetime.now().isoformat()
    for p in zip_paths:
        fname = Path(p).name
        if fname in ingested:
            continue
        logger.info("Ingesting %s", fname)
        with zipfile.ZipFile(p) as z:
            sub_member = _find_member(z, "SUBMISSION.tsv")
            if not sub_member:
                logger.warning("No SUBMISSION.tsv in %s", p)
                continue
            hold_member = _find_member(z, "INFOTABLE.tsv")
            if not hold_member:
                logger.warning("No INFOTABLE.tsv in %s", p)
                continue
            cover_member = _find_member(z, "COVERPAGE.tsv")
            summary_member = _find_member(z, "SUMMARYPAGE.tsv")
            if not cover_member:
                logger.warning("No COVERPAGE.tsv in %s", p)
            if not summary_member:
                logger.warning("No SUMMARYPAGE.tsv in %s", p)

            # submissions (accession -> cik) for this zip; written last
            acc_cik = {}
            acc_period = {}
            sub_rows = []
            with z.open(sub_member) as f:
                for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8"), delimiter="\t"):
                    acc = row["ACCESSION_NUMBER"]
                    acc_cik[acc] = row.get("CIK", "")
                    acc_period[acc] = normalize_period(row.get("PERIODOFREPORT", ""))
                    sub_rows.append({"accession": acc, "cik": row.get("CIK", ""),
                                     "period": acc_period[acc],
                                     "filing_date": row.get("FILING_DATE", ""),
                                     "type": row.get("SUBMISSIONTYPE", "")})

            # cover page: amendment metadata per accession
            cover_by_acc: dict[str, dict] = {}
            if cover_member:
                with z.open(cover_member) as f:
                    for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8"), delimiter="\t"):
                        acc = row.get("ACCESSION_NUMBER", "")
                        if acc:
                            cover_by_acc[acc] = row

            # summary page: other managers count per accession
            summary_by_acc: dict[str, dict] = {}
            if summary_member:
                with z.open(summary_member) as f:
                    for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8"), delimiter="\t"):
                        acc = row.get("ACCESSION_NUMBER", "")
                        if acc:
                            summary_by_acc[acc] = row

            # enrich submission rows with cover/summary fields
            for s in sub_rows:
                acc = s["accession"]
                cover = cover_by_acc.get(acc, {})
                summary = summary_by_acc.get(acc, {})
                s["report_type"] = cover.get("REPORTTYPE") or None
                raw_is = (cover.get("ISAMENDMENT") or "").strip().upper()
                s["is_amendment"] = raw_is == "Y" if raw_is else None
                s["amendment_type"] = _normalize_amendment_type(cover.get("AMENDMENTTYPE", ""))
                s["amendment_no"] = None
                raw_no = (cover.get("AMENDMENTNO") or "").strip()
                if raw_no:
                    try:
                        s["amendment_no"] = int(raw_no)
                    except ValueError:
                        s["amendment_no"] = None
                s["other_managers_count"] = None
                if summary.get("OTHERINCLUDEDMANAGERSCOUNT"):
                    try:
                        s["other_managers_count"] = int(summary["OTHERINCLUDEDMANAGERSCOUNT"])
                    except ValueError:
                        s["other_managers_count"] = None

            # holdings, streamed in chunks, + per-CUSIP name counts (long equity only)
            name_counter = defaultdict(Counter)
            name_raw = {}
            batch = []
            line_no_fallback: dict[str, int] = {}
            seen_keys: set[tuple[str, int]] = set()
            with z.open(hold_member) as f:
                for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8"), delimiter="\t"):
                    acc = row["ACCESSION_NUMBER"]
                    raw_sk = (row.get("INFOTABLE_SK") or "").strip()
                    try:
                        line_no = int(raw_sk) if raw_sk else None
                    except ValueError:
                        line_no = None
                    if line_no is None or (acc, line_no) in seen_keys:
                        line_no = line_no_fallback.get(acc, 0) + 1
                        while (acc, line_no) in seen_keys:
                            line_no += 1
                        line_no_fallback[acc] = line_no
                    seen_keys.add((acc, line_no))

                    cusip = normalize_cusip(row.get("CUSIP", ""))
                    if cusip and _is_long_equity(row):
                        nm = normalize_issuer_name(row.get("NAMEOFISSUER", ""))
                        name_counter[cusip][nm] += 1
                        name_raw.setdefault(cusip, row.get("NAMEOFISSUER", ""))

                    period = acc_period.get(acc)
                    batch.append({"accession": acc, "line_no": line_no,
                                  "cusip": cusip,
                                  "cik": acc_cik.get(acc, ""),
                                  "shares": float(row.get("SSHPRNAMT") or 0),
                                  "value": float(row.get("VALUE") or 0),
                                  "period": period,
                                  "putcall": row.get("PUTCALL") or None,
                                  "sshprnamttype": row.get("SSHPRNAMTTYPE") or None,
                                  "title_of_class": row.get("TITLEOFCLASS") or None,
                                  "investment_discretion": row.get("INVESTMENTDISCRETION") or None,
                                  "other_manager": row.get("OTHERMANAGER") or None,
                                  "voting_sole": float(row.get("VOTING_AUTH_SOLE") or 0) or 0.0,
                                  "voting_shared": float(row.get("VOTING_AUTH_SHARED") or 0) or 0.0,
                                  "voting_none": float(row.get("VOTING_AUTH_NONE") or 0) or 0.0})
                    if len(batch) >= _HOLDINGS_CHUNK:
                        rawstore.write_rows("sec13f_holdings", batch)
                        batch = []
            if batch:
                rawstore.write_rows("sec13f_holdings", batch)

            # canonical issuer name per CUSIP (most common long-equity name)
            name_rows = []
            for cusip, ctr in name_counter.items():
                nm, count = ctr.most_common(1)[0]
                name_rows.append({"cusip": cusip, "name_norm": nm,
                                  "name_raw": name_raw.get(cusip, ""), "count": count})

        def _finish(tx, sub_rows=sub_rows, name_rows=name_rows, fname=fname):
            tx.write_rows("sec13f_submissions", sub_rows)
            if name_rows:
                tx.write_rows("sec13f_cusip_names", name_rows)
            tx.write_rows("sec13f_ingested_zips", [{"filename": fname, "ingested_at": now}])

        rawstore.atomic(_finish)
        new += 1
    return new


def active_accessions_db() -> set:
    """Amendment-aware active 13F-HR accessions per (cik, period) across the store.

    Filings are walked in order of ``(filing_date, amendment_no, accession)``:

    * ``13F-HR`` (not an amendment) resets the active set to that accession.
    * ``13F-HR/A`` + ``amendment_type == "RESTATEMENT"`` replaces the active set.
    * ``13F-HR/A`` + ``amendment_type == "NEW HOLDINGS"`` unions the accession in.
    * Any other ``13F-HR/A`` (unknown or missing type) replaces conservatively.

    ``13F-NT`` filings have no holdings and are ignored.
    """
    rows = rawstore.query(
        "SELECT accession, cik, period, filing_date, type, amendment_type, amendment_no "
        "FROM sec13f_submissions WHERE type LIKE '13F-HR%'")
    by_key: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        period = r.get("period")
        if not period:
            continue
        by_key[(r["cik"], period)].append(r)

    def _order_key(r: dict) -> tuple:
        """``(filing_date, amendment_no, amendment-before-original, accession)``.

        The date decides first; among filings of one date the higher ``AMENDMENTNO`` is later.
        When both are equal (an original and its amendment filed the same day, no number on either)
        ``filing_rank``'s ``/A`` preference still puts the amendment last, so it is the one that
        survives the walk, and the accession is the final tiebreak.
        """
        fd = r.get("filing_date") or ""
        try:
            amend_no = int(r.get("amendment_no") or 0)
        except (ValueError, TypeError):
            amend_no = 0
        rank = filing_rank(fd, r.get("type") or "", r.get("accession") or "")
        return (fd, amend_no, rank[1], rank[2])

    active: set[str] = set()
    for group in by_key.values():
        group.sort(key=_order_key)
        current: set[str] = set()
        for r in group:
            acc = r["accession"]
            typ = (r.get("type") or "")
            amend_type = (r.get("amendment_type") or "").upper()
            is_amendment = typ.endswith("/A")
            if not is_amendment:
                current = {acc}
            elif amend_type == "NEW HOLDINGS":
                current |= {acc}
            else:
                # RESTATEMENT, unknown, empty, or unrecognised: replace (conservative)
                current = {acc}
        active |= current
    return active


def map_candidates() -> list[str]:
    """The CUSIPs the map step asks OpenFIGI about (#207): held in an active filing of a period from
    ``START_QUARTER`` on, and with no ``identity_cusip`` row yet (a CUSIP asked once, matched or not, is not asked
    again). Newest period first, then the largest value held in it, so a capped run maps the biggest holdings."""
    _init_db()
    active = active_accessions_db()
    if not active:
        return []
    with rawstore.connect() as conn:
        rows = conn.execute(
            "WITH active AS (SELECT unnest(?::VARCHAR[]) AS accession), "
            "held AS (SELECT h.cusip, s.period, SUM(h.value) AS value FROM sec13f_holdings h "
            "         JOIN sec13f_submissions s ON s.accession = h.accession "
            "         JOIN active a ON a.accession = h.accession "
            "         WHERE s.period >= ? AND h.cik IS NOT NULL AND h.cik <> '' "
            f"         AND {_LONG_EQUITY_SQL} "
            "         GROUP BY h.cusip, s.period) "
            "SELECT cusip, max(period) AS period, arg_max(value, period) AS value FROM held "
            "WHERE cusip NOT IN (SELECT cusip FROM identity_cusip) "
            "GROUP BY cusip ORDER BY period DESC, value DESC NULLS LAST, cusip",
            [sorted(active), START_QUARTER]).fetchall()
    return [r[0] for r in rows]


def map_identity(cusip_ticker: dict, unmatched: Optional[dict] = None) -> dict:
    """What a derive mapped with, for the run's history (13F-5): the mapped CUSIPs, their count by ``source``
    (``legacy`` for entries written before #207: issuer-name matches and old OpenFIGI rows), a hash of the
    CUSIP -> ticker pairs, and the CUSIPs and value the map leaves out of the newest period."""
    pairs = sorted((c, ticker_of(info)) for c, info in cusip_ticker.items() if ticker_of(info))
    by_source = Counter((cusip_ticker[c].get("source") if isinstance(cusip_ticker[c], dict) else None) or "legacy"
                        for c, _ in pairs)
    digest = hashlib.sha256("\n".join(f"{c}={t}" for c, t in pairs).encode("utf-8")).hexdigest()[:16]
    return {"count": len(pairs), "by_source": dict(sorted(by_source.items())), "hash": digest,
            "unmatched_cusips": (unmatched or {}).get("cusips"), "unmatched_value": (unmatched or {}).get("value")}


def compute_period_metrics(periods: dict) -> dict:
    """Per-period metrics from ``{period: {"shares", "value", "cik": {cik: shares}}}``."""
    metrics = {}
    prev = None
    for period in sorted(periods):
        p = periods[period]
        ciks = p["cik"]
        total = p["shares"]
        top10 = sorted(ciks.values(), reverse=True)[:10]
        top10_pct = (sum(top10) / total * 100.0) if total else None
        hhi = (sum((s / total) ** 2 for s in ciks.values()) * 10000.0) if total else None
        new = exited = buy = sell = None
        if prev is not None:
            new = len(ciks.keys() - prev.keys())
            exited = len(prev.keys() - ciks.keys())
            union = ciks.keys() | prev.keys()
            buy = sum(max(0.0, ciks.get(k, 0.0) - prev.get(k, 0.0)) for k in union)
            sell = sum(max(0.0, prev.get(k, 0.0) - ciks.get(k, 0.0)) for k in union)
        metrics[period] = {
            "shares": total,
            "value": p["value"],
            "holders": len(ciks),
            "top10_pct": round(top10_pct, 2) if top10_pct is not None else None,
            "hhi": round(hhi, 1) if hhi is not None else None,
            "new_holders": new,
            "exited_holders": exited,
            "buy_flow": round(buy, 1) if buy is not None else None,
            "sell_flow": round(sell, 1) if sell is not None else None,
        }
        prev = ciks
    return metrics


def value_scale_summary() -> dict:
    """Summary of the last ``compute_totals`` VALUE-units check.

    Returns ``{"checked": n, "rescaled": n, "periods": {period: n}}``.
    When no check ran (no active filings or no stored closes), every count is 0.
    """
    factors = getattr(compute_totals, "_last_scale_factors", {})
    if not factors:
        return {"checked": 0, "rescaled": 0, "periods": {}}
    checked = len(factors)
    rescaled = sum(1 for f in factors.values() if f != 1.0)
    periods: dict[str, int] = {}
    for (_, period), f in factors.items():
        if f != 1.0:
            periods[period] = periods.get(period, 0) + 1
    return {"checked": checked, "rescaled": rescaled, "periods": periods}


def _reference_closes(tickers: Iterable[str], periods: Iterable[str]) -> dict[tuple[str, str], float]:
    """``{(ticker, period): close}`` — the last stored nominal close on or before each period end.

    One ``prices.daily_bars`` read per period for every ticker, not one read per pair:
    ``prices.duckdb`` is a separate store file, so the closes are read in Python and the triples are
    handed to the aggregation as a list parameter (the ``pairs`` pattern). A bar with no close (a
    halted name) is skipped, so the reference is the last session that has a real price.
    """
    out: dict[tuple[str, str], float] = {}
    names = sorted({t for t in tickers if t})
    if not names:
        return out
    for period in sorted(set(periods)):
        try:
            end = date.fromisoformat(period)
        except (TypeError, ValueError):
            continue
        try:
            df = prices.daily_bars(names, basis="nominal",
                                   start=end - timedelta(days=_CLOSE_LOOKBACK_DAYS), end=end)
        except Exception:  # a store without bars, or a ticker the price tables reject
            logger.warning("13F derive: no stored closes for %s; VALUE units not checked there",
                           period, exc_info=True)
            continue
        if df is None or df.empty or "close" not in df:
            continue
        usable = df[df["close"].notna()].sort_values("session_date")
        for ticker, group in usable.groupby("ticker"):
            out[(ticker, period)] = float(group["close"].iloc[-1])
    return out


def _value_scale_factors(cusip_ticker: dict, active: set[str]) -> dict[tuple[str, str], float]:
    """``{(cik, period): factor}`` for VALUE-units rescale, 1 when no rescale.

    Reference closes are the last stored session on or before each period end
    for each mapped ticker, from ``prices.daily_bars`` (prices.duckdb, nominal
    basis). A filer with at least ``_VALUE_SCALE_MIN_NAMES`` mapped holdings
    whose median ``(value/shares)/close`` sits inside ``_VALUE_SCALE_BAND`` gets
    ``factor = 1000``; otherwise ``1``.
    """
    if not active:
        return {}
    # latest period per active accession
    period_for_acc: dict[str, str] = {}
    for r in rawstore.query(
            "SELECT accession, period FROM sec13f_submissions "
            "WHERE accession IN (SELECT unnest(?::VARCHAR[])) AND period >= ?",
            [sorted(active), START_QUARTER]):
        period_for_acc[r["accession"]] = r["period"]
    if not period_for_acc:
        return {}

    # map each holding's (cusip, period) -> ticker for the price lookup
    cusip_to_ticker = {c: ticker_of(info) for c, info in cusip_ticker.items() if ticker_of(info)}
    if not cusip_to_ticker:
        return {}

    # ticker -> set of periods it is held in (for which we need a close)
    ticker_periods: dict[str, set[str]] = defaultdict(set)
    for r in rawstore.query(
            "SELECT DISTINCT h.accession, h.cusip FROM sec13f_holdings h "
            "WHERE h.accession IN (SELECT unnest(?::VARCHAR[])) "
            f"AND {_LONG_EQUITY_SQL}",
            [sorted(active)]):
        acc = r["accession"]
        cusip = r["cusip"]
        period = period_for_acc.get(acc)
        if not period or cusip not in cusip_to_ticker:
            continue
        ticker_periods[cusip_to_ticker[cusip]].add(period)
    if not ticker_periods:
        return {}

    # last close per (ticker, period), one read per period
    close_by_tp = _reference_closes(ticker_periods.keys(),
                                    {p for ps in ticker_periods.values() for p in ps})
    if not close_by_tp:
        return {}

    # per (cik, period), collect implied prices for mapped long-equity holdings
    from statistics import median
    ratios: dict[tuple[str, str], list[float]] = defaultdict(list)
    for r in rawstore.query(
            "SELECT h.cik, h.accession, h.cusip, h.shares, h.value "
            "FROM sec13f_holdings h "
            "WHERE h.accession IN (SELECT unnest(?::VARCHAR[])) "
            "AND h.cik IS NOT NULL AND h.cik <> '' "
            f"AND {_LONG_EQUITY_SQL}",
            [sorted(active)]):
        acc = r["accession"]
        cusip = r["cusip"]
        period = period_for_acc.get(acc)
        ticker = cusip_to_ticker.get(cusip)
        shares = r["shares"] or 0.0
        if not period or not ticker or not shares:
            continue
        close = close_by_tp.get((ticker, period))
        if not close:
            continue
        implied = (r["value"] or 0.0) / shares
        ratios[(r["cik"], period)].append(implied / close)

    factors: dict[tuple[str, str], float] = {}
    for key, vals in ratios.items():
        if len(vals) < _VALUE_SCALE_MIN_NAMES:
            factors[key] = 1.0
            continue
        m = median(vals)
        lo, hi = _VALUE_SCALE_BAND
        factors[key] = 1000.0 if lo < m < hi else 1.0
    return factors


def compute_totals(cusip_ticker: dict) -> dict:
    """Rich per-ticker quarterly metrics from raw.duckdb.

    Returns ``{ticker: {period: {shares, value, holders, new_holders,
    exited_holders, top10_pct, hhi, buy_flow, sell_flow}}}``. Multi-CUSIP
    tickers are summed; per-holder (CIK) positions are kept so churn can be
    computed. Only periods >= ``START_QUARTER`` are returned (older periods are
    stray amendments).

    Long-equity rows (``SSHPRNAMTTYPE == 'SH'`` and no ``PUTCALL``) are counted;
    puts and PRNs are excluded. VALUE units are checked per (cik, period)
    against stored D01 closes and rescaled by 1000 when they are still in
    thousands (#208).

    The join AND the aggregation run inside DuckDB. ``sec13f_holdings`` is
    ~20M rows; materializing the joined rows in Python needs ~14 GB, while
    the grouped result is a few thousand rows.
    """
    active = active_accessions_db()
    pairs = [[cusip, ticker_of(info)] for cusip, info in cusip_ticker.items() if ticker_of(info)]
    # A run that checks nothing must not report the previous run's counts.
    compute_totals._last_scale_factors = {}  # type: ignore[attr-defined]
    if not active or not pairs:
        return {}

    scale_factors = _value_scale_factors(cusip_ticker, active)
    # Make the factors available to callers (history writer, job note).
    compute_totals._last_scale_factors = scale_factors  # type: ignore[attr-defined]

    # Build a (cik, period) -> factor lookup table in DuckDB via a list param.
    factor_pairs = [[cik, period, factor] for (cik, period), factor in scale_factors.items()]

    # The active set, the CUSIP->ticker pairs and the scale-factor lookup are
    # handed to DuckDB as list parameters and unnested in the query: one
    # statement, no row-by-row temp table inserts (O6).
    with rawstore.connect() as conn:
        rows = conn.execute(
            "WITH active AS (SELECT unnest(?::VARCHAR[]) AS accession), "
            "mapped AS (SELECT m[1] AS cusip, m[2] AS ticker "
            "           FROM (SELECT unnest(?::VARCHAR[][]) AS m)), "
            "scale AS (SELECT f[1] AS cik, f[2] AS period, f[3]::DOUBLE AS factor "
            "          FROM (SELECT unnest(?::VARCHAR[][]) AS f)) "
            "SELECT m.ticker, s.period, h.cik, SUM(h.shares), SUM(h.value * COALESCE(sc.factor, 1.0)) "
            "FROM sec13f_holdings h "
            "JOIN mapped m ON m.cusip = h.cusip "
            "JOIN sec13f_submissions s ON s.accession = h.accession "
            "JOIN active a ON a.accession = h.accession "
            "LEFT JOIN scale sc ON sc.cik = h.cik AND sc.period = s.period "
            "WHERE s.period >= ? AND h.cik IS NOT NULL AND h.cik <> '' "
            f"AND {_LONG_EQUITY_SQL} "
            "GROUP BY m.ticker, s.period, h.cik",
            [sorted(active), pairs, factor_pairs, START_QUARTER]).fetchall()

    periods_by_ticker = defaultdict(lambda: defaultdict(
        lambda: {"shares": 0.0, "value": 0.0, "cik": {}}))
    for ticker, period, cik, shares, value in rows:
        if not period:
            continue
        p = periods_by_ticker[ticker][period]
        p["shares"] += shares or 0.0
        p["value"] += value or 0.0
        p["cik"][cik] = p["cik"].get(cik, 0.0) + (shares or 0.0)

    totals = {}
    for ticker, periods in periods_by_ticker.items():
        metrics = compute_period_metrics(dict(periods))
        if metrics:
            totals[ticker] = metrics
    return totals


def unmatched_summary(cusip_ticker: dict) -> Optional[dict]:
    """What the derive leaves out: CUSIPs of the latest period with no ticker.

    ``compute_totals`` joins holdings to ``cusip_ticker``; a CUSIP absent from it is
    dropped without a trace. Read-only. ``{"period", "cusips", "value",
    "top": [(issuer, value), ...]}`` for the newest active period, or ``None`` when there
    is nothing to look at.
    """
    active = active_accessions_db()
    if not active:
        return None
    mapped = sorted(c for c, info in cusip_ticker.items() if ticker_of(info))
    with rawstore.connect() as conn:
        period = conn.execute(
            "SELECT max(s.period) FROM sec13f_submissions s "
            "WHERE s.accession IN (SELECT unnest(?::VARCHAR[])) AND s.period >= ?",
            [sorted(active), START_QUARTER]).fetchone()[0]
        if not period:
            return None
        rows = conn.execute(
            "SELECT h.cusip, SUM(h.value) AS value FROM sec13f_holdings h "
            "JOIN sec13f_submissions s ON s.accession = h.accession "
            "WHERE s.period = ? AND s.accession IN (SELECT unnest(?::VARCHAR[])) "
            "AND h.cik IS NOT NULL AND h.cik <> '' "
            f"AND {_LONG_EQUITY_SQL} "
            "AND h.cusip NOT IN (SELECT unnest(?::VARCHAR[])) "
            "GROUP BY h.cusip ORDER BY value DESC NULLS LAST",
            [period, sorted(active), mapped]).fetchall()
        names = {r[0]: r[1] for r in conn.execute(
            "SELECT cusip, name_raw FROM sec13f_cusip_names").fetchall()}
    top = [(names.get(c) or c, float(v or 0.0)) for c, v in rows[:3]]
    return {"period": period, "cusips": len(rows),
            "value": float(sum(v or 0.0 for _, v in rows)), "top": top}


def _money(v: float) -> str:
    if abs(v) >= 1e9:
        return f"${v / 1e9:.2f}B"
    if abs(v) >= 1e6:
        return f"${v / 1e6:.2f}M"
    return f"${v:,.0f}"


def unmatched_note(summary: Optional[dict]) -> Optional[str]:
    """One line for the board / job message, or ``None`` when nothing is left out."""
    if not summary or not summary.get("cusips"):
        return None
    n = summary["cusips"]
    text = (f"{n} CUSIP{'s' if n != 1 else ''} with no ticker "
            f"({_money(summary['value'])} held in {summary['period']}) "
            "are left out of the totals")
    top = summary.get("top") or []
    if top:
        text += "; largest: " + ", ".join(f"{nm} {_money(v)}" for nm, v in top)
    return text + "."


class DeriveRefused(RuntimeError):
    """The derive would replace the stored totals with something worse; they were kept."""


# Refuse a derive that keeps under this share of the stored tickers (stores with
# fewer than ``_SHRINK_MIN_TICKERS`` tickers are not guarded).
_SHRINK_MIN_TICKERS = 10
_SHRINK_RATIO = 0.5
# The dollar guards (13F-6, #207), on the newest quarter of the stored totals: refuse a derive whose mapped value of
# that quarter falls under ``_VALUE_RATIO`` of the stored one, or whose share of the quarter's held value left
# unmapped rises by more than ``_UNMATCHED_RISE``. Defaults until the #207 dry-run card sets them.
_VALUE_RATIO = 0.9
_UNMATCHED_RISE = 0.05


def _orphan_holdings() -> int:
    """Accessions holding rows (with a CIK) but no submission row: an interrupted ingest."""
    rows = rawstore.query(
        "SELECT count(DISTINCT h.accession) AS n FROM sec13f_holdings h "
        "WHERE h.cik IS NOT NULL AND h.cik <> '' AND NOT EXISTS "
        "(SELECT 1 FROM sec13f_submissions s WHERE s.accession = h.accession)")
    return int(rows[0]["n"]) if rows else 0


def _ticker_periods(totals: dict) -> int:
    return sum(len(periods) for periods in totals.values())


def _period_value(totals: dict, period: str) -> float:
    """The mapped value of one quarter: the sum of its ``value`` over every ticker."""
    return float(sum((periods.get(period) or {}).get("value") or 0.0 for periods in totals.values()))


def held_value(period: str) -> float:
    """The value held in a quarter's active filings, mapped or not (the map does not change it)."""
    active = active_accessions_db()
    if not active:
        return 0.0
    with rawstore.connect() as conn:
        v = conn.execute(
            "SELECT SUM(h.value) FROM sec13f_holdings h "
            "JOIN sec13f_submissions s ON s.accession = h.accession "
            "WHERE s.period = ? AND s.accession IN (SELECT unnest(?::VARCHAR[])) "
            "AND h.cik IS NOT NULL AND h.cik <> '' "
            f"AND {_LONG_EQUITY_SQL}", [period, sorted(active)]).fetchone()[0]
    return float(v or 0.0)


def guard(before: dict, totals: dict) -> None:
    """Raise ``DeriveRefused`` when ``totals`` would replace ``before`` with something much smaller.

    Stores with fewer than ``_SHRINK_MIN_TICKERS`` tickers are not guarded. Otherwise, in this order: the ticker
    count falls under ``_SHRINK_RATIO``; the newest stored quarter's mapped value falls under ``_VALUE_RATIO``; the
    share of that quarter's held value left unmapped rises by more than ``_UNMATCHED_RISE`` (13F-6: a map change that
    drops a few large names keeps the ticker count).
    """
    if len(before) < _SHRINK_MIN_TICKERS:
        return
    if len(totals) < len(before) * _SHRINK_RATIO:
        raise DeriveRefused(
            f"the derive would shrink the totals from {len(before)} to {len(totals)} tickers; "
            "the stored totals were kept.")
    period = max(p for periods in before.values() for p in periods)
    was, now = _period_value(before, period), _period_value(totals, period)
    if was > 0 and now < was * _VALUE_RATIO:
        raise DeriveRefused(
            f"the derive would cut the mapped value of {period} from {_money(was)} to {_money(now)} "
            f"(under {_VALUE_RATIO:.0%}); the stored totals were kept.")
    held = max(held_value(period), was, now)
    if held > 0 and (was - now) / held > _UNMATCHED_RISE:
        raise DeriveRefused(
            f"the derive would raise the unmapped share of {period}'s held value from "
            f"{1 - was / held:.1%} to {1 - now / held:.1%} (more than {_UNMATCHED_RISE:.0%} points); "
            "the stored totals were kept.")


def recompute_totals(cusip_map: Optional[dict] = None, force: bool = False) -> dict:
    """The derive stage: rebuild ``sec13f_totals`` from the store, no network.

    Maps CUSIPs to tickers through ``cusip_map`` (the map step's new map, from ``identity_cusip``) or, by default,
    the stored ``cusip_ticker``. Nothing is matched by issuer name (#207): a CUSIP with no map entry stays out of
    the totals and in the unmatched note. Returns the new totals, or ``{}`` when the store holds nothing to derive
    from; the stored totals and map are left alone rather than wiped.

    When ``cusip_map`` is given and the derive passes, ``cusip_ticker`` is replaced by it (old rows deleted) before
    the totals are saved. A refused derive keeps the stored map and the stored totals, so the two always match.

    Raises ``DeriveRefused`` when holdings exist without a submission row (an ingest died half-way; the derive would
    see partial data) or when ``guard`` refuses the result (``force`` overrides ``guard`` only).
    """
    _init_db()
    orphans = _orphan_holdings()
    if orphans:
        raise DeriveRefused(
            f"{orphans} accession(s) have orphan holdings (an interrupted ingest); "
            "the stored totals were kept. Re-ingest the data set.")
    stored_map = load_cusip_ticker()
    use = stored_map if cusip_map is None else cusip_map
    totals = compute_totals(use)
    if not totals:
        logger.warning("13F derive: no holdings to derive totals from; kept the stored totals")
        return totals
    before = load_totals()
    if not force:
        guard(before, totals)
    logger.info("13F derive: %d -> %d tickers, %d -> %d ticker-periods",
                len(before), len(totals), _ticker_periods(before), _ticker_periods(totals))
    scale = value_scale_summary()
    if scale["checked"]:
        logger.info("13F derive: VALUE unit check checked=%d rescaled=%d periods=%s",
                    scale["checked"], scale["rescaled"], scale["periods"])
    else:
        logger.info("13F derive: VALUE unit check skipped (no stored closes)")
    if cusip_map is not None and cusip_map != stored_map:
        save_cusip_ticker(cusip_map)
    save_totals(totals)
    return totals


def build_sec13f(zip_paths: Iterable[str | Path]) -> dict:
    """Ingest new ZIPs, then derive the totals with the stored map (ingest + ``recompute_totals``)."""
    ingest_zips(list(zip_paths))
    return recompute_totals() or {}
