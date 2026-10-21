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
  SUBMISSION.tsv   ACCESSION_NUMBER, FILING_DATE, SUBMISSIONTYPE, CIK, PERIODOFREPORT
  INFOTABLE.tsv    ACCESSION_NUMBER, NAMEOFISSUER, CUSIP, VALUE, SSHPRNAMT,
                   SSHPRNAMTTYPE (SH/PRN), PUTCALL, INVESTMENTDISCRETION, ...

Only long equity holdings (``SSHPRNAMTTYPE == "SH"`` and no ``PUTCALL``) are
counted. Amendments are deduped by keeping the latest 13F-HR filing per
(CIK, PERIODOFREPORT).
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


from RevenueChaser.core import rawstore
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
    """Return the archive member name ending in ``name`` (ZIPs may nest files)."""
    for m in z.namelist():
        if m.endswith(name):
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


# Holdings are inserted in chunks so a multi-GB quarter never holds the single
# writer lock for minutes at a stretch (a dashboard read would time out).
_HOLDINGS_CHUNK = 100_000


def _clear_holdings_for(accessions: list[str]) -> None:
    """Drop any holdings already stored for these accessions.

    ``sec13f_holdings`` is append-only with no key (one filing legitimately
    lists the same CUSIP more than once), so re-running after an interrupted
    ingest would otherwise DOUBLE the rows — silently inflating every
    shares/value/holder figure downstream. Deleting the zip's own accessions
    first makes the chunked insert idempotent.
    """
    for i in range(0, len(accessions), 1000):
        chunk = accessions[i:i + 1000]
        ph = ", ".join(["?"] * len(chunk))
        rawstore.execute(f"DELETE FROM sec13f_holdings WHERE accession IN ({ph})", chunk)


def _clear_submissions_for(accessions: list[str]) -> None:
    """Drop the stored submission rows of these accessions (see ``ingest_zips``)."""
    for i in range(0, len(accessions), 1000):
        chunk = accessions[i:i + 1000]
        ph = ", ".join(["?"] * len(chunk))
        rawstore.execute(f"DELETE FROM sec13f_submissions WHERE accession IN ({ph})", chunk)


def ingest_zips(zip_paths: Iterable[str | Path]) -> int:
    """Stream any not-yet-ingested ZIPs into raw.duckdb (returns count).

    Only long equity holdings (``SSHPRNAMTTYPE == "SH"`` and no ``PUTCALL``)
    are stored. Each zip is marked ingested by filename, so re-running skips
    already-loaded zips — making the store incremental and the zips deletable.

    A zip is streamed in ``_HOLDINGS_CHUNK`` row batches, so it is never held in
    memory and the writer lock is never held for a whole quarter. The batches go
    first; the zip's ``sec13f_submissions`` rows, its issuer names and the
    ``ingested_zips`` marker follow in ONE transaction. The derive joins every
    holding through its submission row, so holdings of a zip that died half-way
    are invisible to it, and a rerun clears them before re-inserting.

    The clear covers the zip's existing submission rows too: on a re-ingest (marker
    removed) the old rows would otherwise keep a half-rewritten accession visible
    to the derive. ``recompute_totals`` refuses while holdings without a
    submission row remain.
    """
    _init_db()
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

            # submissions (accession -> cik) for this zip; written last
            acc_cik = {}
            sub_rows = []
            with z.open(sub_member) as f:
                for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8"), delimiter="\t"):
                    acc = row["ACCESSION_NUMBER"]
                    acc_cik[acc] = row.get("CIK", "")
                    sub_rows.append({"accession": acc, "cik": row.get("CIK", ""),
                                     "period": normalize_period(row.get("PERIODOFREPORT", "")),
                                     "filing_date": row.get("FILING_DATE", ""),
                                     "type": row.get("SUBMISSIONTYPE", "")})

            # holdings, streamed in chunks, + per-CUSIP name counts
            _clear_holdings_for(sorted(acc_cik))
            _clear_submissions_for(sorted(acc_cik))
            name_counter = defaultdict(Counter)
            name_raw = {}
            batch = []
            with z.open(hold_member) as f:
                for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8"), delimiter="\t"):
                    if row.get("SSHPRNAMTTYPE") != "SH" or row.get("PUTCALL"):
                        continue
                    cusip = normalize_cusip(row.get("CUSIP", ""))
                    if not cusip:
                        continue
                    nm = normalize_issuer_name(row.get("NAMEOFISSUER", ""))
                    name_counter[cusip][nm] += 1
                    name_raw.setdefault(cusip, row.get("NAMEOFISSUER", ""))
                    batch.append({"accession": row["ACCESSION_NUMBER"], "cusip": cusip,
                                  "cik": acc_cik.get(row["ACCESSION_NUMBER"], ""),
                                  "shares": float(row.get("SSHPRNAMT") or 0),
                                  "value": float(row.get("VALUE") or 0)})
                    if len(batch) >= _HOLDINGS_CHUNK:
                        rawstore.write_rows("sec13f_holdings", batch)
                        batch = []
            if batch:
                rawstore.write_rows("sec13f_holdings", batch)

            # canonical issuer name per CUSIP (most common)
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
    """Latest 13F-HR accession per (cik, period) across the whole store."""
    latest = {}
    for row in rawstore.query(
            "SELECT accession, cik, period, filing_date, type FROM sec13f_submissions "
            "WHERE type LIKE '13F-HR%'"):
        period = row["period"]
        if not period:
            continue
        key = (row["cik"], period)
        cand = filing_rank(row["filing_date"], row["type"], row["accession"])
        if key not in latest or cand > latest[key]:
            latest[key] = cand
    return {cand[2] for cand in latest.values()}


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


def compute_totals(cusip_ticker: dict) -> dict:
    """Rich per-ticker quarterly metrics from raw.duckdb.

    Returns ``{ticker: {period: {shares, value, holders, new_holders,
    exited_holders, top10_pct, hhi, buy_flow, sell_flow}}}``. Multi-CUSIP
    tickers are summed; per-holder (CIK) positions are kept so churn can be
    computed. Only periods >= ``START_QUARTER`` are returned (older periods are
    stray amendments).

    The join AND the aggregation run inside DuckDB. ``sec13f_holdings`` is
    ~16.6M rows; materializing the joined rows in Python needs ~14 GB, while
    the grouped result is a few thousand rows.
    """
    active = active_accessions_db()
    pairs = [[cusip, ticker_of(info)] for cusip, info in cusip_ticker.items() if ticker_of(info)]
    if not active or not pairs:
        return {}

    # The active set and the CUSIP->ticker pairs are handed to DuckDB as list
    # parameters and unnested in the query: one statement, no row-by-row temp
    # table inserts (O6).
    with rawstore.connect() as conn:
        rows = conn.execute(
            "WITH active AS (SELECT unnest(?::VARCHAR[]) AS accession), "
            "mapped AS (SELECT m[1] AS cusip, m[2] AS ticker "
            "           FROM (SELECT unnest(?::VARCHAR[][]) AS m)) "
            "SELECT m.ticker, s.period, h.cik, SUM(h.shares), SUM(h.value) "
            "FROM sec13f_holdings h "
            "JOIN mapped m ON m.cusip = h.cusip "
            "JOIN sec13f_submissions s ON s.accession = h.accession "
            "JOIN active a ON a.accession = h.accession "
            "WHERE s.period >= ? AND h.cik IS NOT NULL AND h.cik <> '' "
            "GROUP BY m.ticker, s.period, h.cik",
            [sorted(active), pairs, START_QUARTER]).fetchall()

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
            "AND h.cik IS NOT NULL AND h.cik <> ''", [period, sorted(active)]).fetchone()[0]
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


def recompute_totals(cusip_map: Optional[dict] = None, force: bool = False) -> Optional[dict]:
    """The derive stage: rebuild ``sec13f_totals`` from the store, no network.

    Maps CUSIPs to tickers through ``cusip_map`` (the map step's new map, from ``identity_cusip``) or, by default,
    the stored ``cusip_ticker``. Nothing is matched by issuer name (#207): a CUSIP with no map entry stays out of
    the totals and in the unmatched note. Returns the new totals, or ``None`` when the store holds nothing to derive
    from: the stored totals and map are then left alone rather than wiped.

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
        return None
    before = load_totals()
    if not force:
        guard(before, totals)
    logger.info("13F derive: %d -> %d tickers, %d -> %d ticker-periods",
                len(before), len(totals), _ticker_periods(before), _ticker_periods(totals))
    if cusip_map is not None and cusip_map != stored_map:
        save_cusip_ticker(cusip_map)
    save_totals(totals)
    return totals


def build_sec13f(zip_paths: Iterable[str | Path]) -> dict:
    """Ingest new ZIPs, then derive the totals with the stored map (ingest + ``recompute_totals``)."""
    ingest_zips(list(zip_paths))
    return recompute_totals() or {}
