"""SEC Form 13F — what the board needs from the ownership serving store.

The numbers come from the builder (``rcdata/build/ownership``, PR 16): it walks the SEC's Form 13F data sets and
the EDGAR daily index, keeps every filing and holding in Parquet, names each CUSIP through the spine and derives
the per-ticker metrics into ``ownership.duckdb``. ``load_totals`` reads them.

This module used to do all of that itself, into ``raw.duckdb``: download the data-set zips, ingest them, ask
OpenFIGI for a CUSIP map and derive ``sec13f_totals``. Two writers of the same numbers meant the board showed
whichever had run last, and on a machine where only the builder ever ran it showed nothing at all. The old path is
gone (G9); what stays here is the board's own material: the quarter calendar, the UW name/sector meta the
dashboard keeps, and the totals read.
"""
import logging
import time
from datetime import date, datetime, timedelta
from typing import Optional

from RevenueChaser.core import ownership, rawstore

logger = logging.getLogger(__name__)

# Quarters at or after this date are considered "complete" (older periods are stray amendments).
START_QUARTER = "2025-03-31"

FILING_LAG_DAYS = 45  # 13F filings are due 45 days after quarter-end

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
    fetched_at = time.time()
    rawstore.write_rows("sec13f_security_meta", [
        {"ticker": t, "entry": v, "fetched_at": fetched_at} for t, v in meta.items()
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


def load_totals() -> dict:
    """The 13F per-ticker totals the board shows: its own file (``store``, #732) while it is current, else the
    builder's rows merged over the file's closed quarters. Asymmetric Growth reads the builder's rows directly
    (``core.ownership.sec13f_totals``, #16) and must not import this engine for them."""
    from RevenueChaser.module_sec_13f import store
    return store.read_totals()
