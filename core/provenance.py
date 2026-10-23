"""What date is each board actually computed against?

"Last run" only says when a scan executed. It says nothing about how old the
downloaded data underneath is — and a re-render that reads the cache without
touching the network stamps "now" over data that may be days old. That is how
the Swing board came to read "Last run: today" on Aug 31 bars.

So every board backed by downloaded data reports the newest date present in the
data it actually reads. One resolver per board, all reading the store, so a new
board gets a data date by adding one entry rather than by patching the UI.

Returns an ISO ``YYYY-MM-DD`` string, or ``None`` for a board that holds no
downloaded data (a live-only view, or an empty store).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from RevenueChaser.core import rawstore
from RevenueChaser.core.calendar import to_session_date

logger = logging.getLogger(__name__)


def _day(value) -> str | None:
    """Normalise whatever a column holds into a YYYY-MM-DD day."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):  # epoch seconds (ticker_payload_cache.fetched_at)
        return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%d")
    text = str(value)
    return text[:10] if len(text) >= 10 else None


def session_of(value) -> str | None:
    """ISO New York session date of an old run-time value (epoch, ISO text or date), else ``None``.

    The read path for old tables until each migrates to ``session_date`` (#283, ADR-3). Unlike
    ``_day`` it does not take a text prefix or the UTC date, so a Saturday fetch reads as
    Friday. Never raises: a value outside the calendar or unparseable gives ``None``.
    """
    try:
        d = to_session_date(value)
    except Exception:  # noqa: BLE001 - a label is never worth an error
        return None
    return d.isoformat() if d else None


def _max(sql: str, db: str = "raw") -> str | None:
    rows = rawstore.query(sql, db=db)
    return _day(rows[0]["v"]) if rows else None


# ── per-board resolvers ─────────────────────────────────────────────────────

def _comovement_day() -> str | None:
    """Newest D01 session over the names Co-movement clusters (the classification universe): it reads D01 since
    the #379 D01 switch, so its date is its own names' price date (DF101, as K7 does for Swing and Fundamentals)."""
    from RevenueChaser.module_comovement import ohlc
    return d01_day(ohlc.universe_tickers())


def d01_day(tickers) -> str | None:
    """Newest D01 session (``price_bars_daily``) over ``tickers``; ``None`` when none has a bar."""
    from RevenueChaser.core import ledger
    days = [day for day, _ in ledger.d01_last_bars(tickers).values()]
    return max(days) if days else None


def _scored_d01_day(scores: str):
    """A resolver: the newest D01 session over the names in the board's score table ``scores`` (K7, #383 R3).

    Swing and Fundamentals score every name from D01 since C12, so their date is the price date of their own names
    (DF101), not the newest bar of any ticker in a shared cache. Asymmetric Growth reads its momentum from D01 since
    #380 S4, so its date is its scored names' too. No scored name: no date.
    """
    def resolve() -> str | None:
        return d01_day(r["ticker"] for r in rawstore.query(f"SELECT ticker FROM {scores}"))
    return resolve


def _ticker_cache_day() -> str | None:
    """When the per-ticker screener payloads were last fetched."""
    return _max("SELECT max(fetched_at) AS v FROM ticker_payload_cache")


def _rotation_day() -> str | None:
    return _max("SELECT max(as_of_date) AS v FROM rotation_snapshots")


def _sec13f_day() -> str | None:
    """Newest 13F reporting period in ``sec13f_totals`` — what the board renders.

    Quarterly, so months behind by design. It reads the totals (``{period: metrics}``
    per ticker), not the submissions: a period that was ingested but not derived yet
    is not on the board.
    """
    return _max("SELECT max(k) AS v FROM "
                "(SELECT unnest(json_keys(entry)) AS k FROM sec13f_totals)")


# Newest print times read for the LEAPS chip: text order is near the time order (offsets can move a value a few
# hours), so the session is taken over this many newest values, not only the first.
_LEAPS_CANDIDATES = 50


def _leaps_day() -> str | None:
    """NY session of the newest flow print (its ``executed_at``, else when the store found it), falling back to the
    newest cached snapshot. ``discovered_at`` alone dated a print by its discovery: a Saturday scan read Saturday."""
    rows = rawstore.query(
        "SELECT v FROM (SELECT DISTINCT COALESCE(NULLIF(executed_at, ''), discovered_at) AS v "
        "FROM leaps_flow_prints) WHERE v IS NOT NULL AND v <> '' ORDER BY v DESC LIMIT ?", [_LEAPS_CANDIDATES])
    sessions = [d for d in (session_of(r["v"]) for r in rows) if d]
    if sessions:
        return max(sessions)
    return _max("SELECT max(json_extract_string(payload, '$.latest.as_of')) AS v FROM leaps_cache")


def _spx_gamma_day() -> str | None:
    """SPX Gamma reports come from an external tree, not the store."""
    from RevenueChaser.app import runners
    path = runners.spxgamma_latest_path()
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return _day(payload.get("asof_date") or payload.get("api_snapshot_time"))


def _chain_day() -> str | None:
    """Newest filing date in the Value Chain corpus."""
    return _max("SELECT max(filed) AS v FROM chain_filings", db="edgar")


def _oipd_day() -> str | None:
    """As-of day of the cached chain of the ticker on the board (falls back to its fetch time).

    The shown ticker is the one in the last rendered payload (``.cache/oipd/oipd_latest.json``); with none
    rendered there is no shown ticker and no date. Not the newest ``as_of`` of any ticker (OIPD-7).
    """
    from RevenueChaser.core.config import OUTPUT_DIR as CACHE_ROOT
    p = CACHE_ROOT / "oipd" / "oipd_latest.json"
    if not p.exists():
        return None
    ticker = (json.loads(p.read_text(encoding="utf-8")).get("ticker") or "").strip().upper()
    if not ticker:
        return None
    rows = rawstore.query("SELECT as_of, fetched_at FROM oipd_meta WHERE ticker = ?", [ticker])
    if not rows:
        return None
    return _day(rows[0].get("as_of")) or _day(rows[0].get("fetched_at"))


def _quick_financials_day() -> str | None:
    """Date the raw payload behind the current Financials report was fetched.

    Reads the per-module state file (``.cache/financials/financials_latest.json``)
    for the last-rendered ticker, then that ticker's ``ticker_payload_cache`` fetch time.
    """
    try:
        from RevenueChaser.core.config import OUTPUT_DIR as CACHE_ROOT
        p = CACHE_ROOT / "financials" / "financials_latest.json"
        if not p.exists():
            return None
        state = json.loads(p.read_text(encoding="utf-8"))
        ticker = (state.get("ticker") or "").strip().upper()
        if not ticker:
            return None
        row = rawstore.get("ticker_payload_cache", ticker)
        if not row:
            return None
        return _day(row.get("fetched_at"))
    except Exception:  # pragma: no cover - a label is never worth an error
        return None


def _discord_day() -> str | None:
    """Newest message stored in the Discord archive."""
    return _max("SELECT max(ts) AS v FROM discord_messages", db="discord")


# module key (as used by app/dashboard.py's tabs) -> resolver.
# A board absent from this map, or mapped to None, shows no data date — say so
# explicitly rather than leaving a stale value on screen.
RESOLVERS = {
    # Swing, Fundamentals and Asymmetric Growth: the D01 date of the names they last scored (K7, #383 R3; AG #380 S4).
    "module_swing_potential": _scored_d01_day("swing_scores"),
    "module_fundamentals": _scored_d01_day("fundamentals_scores"),
    "module_asymmetric_growth": _scored_d01_day("ticker_scores"),
    # Co-movement clusters its names' D01 closes (#379 D01 switch).
    "module_comovement": _comovement_day,
    "module_earnings_radar": _ticker_cache_day,
    "module_sector_rotation": _rotation_day,
    "module_sec_13f": _sec13f_day,
    "module_leaps": _leaps_day,
    "module_spx_gamma": _spx_gamma_day,
    "module_financials": _quick_financials_day,
    "module_value_chain": _chain_day,
    # Discord archive: the newest stored message.
    "module_discord": _discord_day,
    # Relatedness profiles are derived from the same 10-K/20-F corpus.
    "module_relatedness": _chain_day,
    # Implied Probability reads its own cached UW option chain.
    "module_oipd": _oipd_day,
    # Settings / Data health is an inventory of many datasets, each reporting its
    # own date on its row — there is no single downloaded dataset to stamp here.
    "module_settings": None,
    # interactive lab over the UW API: live state, no downloaded dataset
    "module_uw_interaction": None,
    # the dashboard's own log files: no downloaded dataset
    "logs": None,
    "runs": None,
}


def data_as_of(module: str) -> str | None:
    """Newest data date backing ``module``; ``None`` when it has none.

    Never raises: a provenance label must not be able to break a board.
    """
    resolver = RESOLVERS.get(module)
    if resolver is None:
        return None
    try:
        return resolver()
    except Exception:  # noqa: BLE001 - a label is never worth an error
        logger.debug("data_as_of(%s) failed", module, exc_info=True)
        return None
