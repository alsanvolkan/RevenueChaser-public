"""Value Chain board -> result history (#387, A3; avo's card 12: the Value Chain edges kept per run).

``record`` runs after every write of ``chain_mentions`` (``engine.record_history``): a Value Chain Fetch
(``engine.run``, the dashboard) and an EDGAR sync (``sync.sync_filings``: the dashboard's EDGAR sync job and the
scheduled ``RC-ValueChain-Sync``), since both rebuild the edges the board shows. It hands ``core/history.record_run``
the edges ``chain_mentions`` holds after the write (``value_chain_edges_history`` is its mirror, numbered by
``row_no`` because ``chain_mentions`` has no PRIMARY KEY); a Fetch also stores the board payload
(``result_runs.payload``, compressed). The mapping lives here, in the module, not in ``core/``. A Refresh never calls
it (06 rule 9).

Value Chain is a run board: every run keeps its rows, and a degraded run is kept too, marked 'partial' with the reason
in ``quality_note`` (design C rule E). The reasons, all run-level: no edge came out of the corpus, so the stored
edges were kept (#284); some tickers' filings could not be fetched or synced, so their edges come from the stored
filing; or SEC stopped answering and the sync left tickers pending.
"""
from __future__ import annotations

from RevenueChaser.core import history, ledger
from RevenueChaser.module_value_chain import config, precision

BOARD, TABLE = "module_value_chain", "value_chain_edges_history"
# Bump when the extraction changes in a way the config hash does not see (``mentions``' relation patterns,
# ``concentration``, ``aliases``, the splitter's patterns), not a setting.
# 2: a concentration edge keeps the share its sentence states (``share_pct``, #387 D7).
# 3: the annual report's XBRL major customers (``customer_share``) are edges too, on the fundamentals layer (G6).
SELECTION_VERSION = "3"


def effective_config() -> dict:
    """The settings the filings are sliced and the edges extracted and gated with."""
    return {
        "MIN_ITEM_CHARS": config.MIN_ITEM_CHARS, "MAX_ITEM_CHARS": config.MAX_ITEM_CHARS,
        "MENTION_WINDOW": config.MENTION_WINDOW, "MISS_RETRY_DAYS": config.MISS_RETRY_DAYS,
        "USE_SHARED_UNIVERSE": config.USE_SHARED_UNIVERSE, "MIN_PRECISION": precision.MIN_PRECISION,
        "MIN_LABELED_N": precision.MIN_LABELED_N, "MAX_EDGES_PER_TICKER": precision.MAX_EDGES_PER_TICKER,
    }


def model_version() -> str:
    return ledger.model_version(SELECTION_VERSION, effective_config())


def _problems(edges: list[dict], kept: bool, filings: int | None, counts: dict) -> list[str]:
    """Why this run's edges are not whole (empty when they are)."""
    out = []
    if kept:
        out.append(f"no edge came out of {filings or 0} filings, so the {len(edges)} stored edges were kept")
    if counts.get("failed"):
        out.append(f"{counts['failed']} tickers' filings could not be fetched or synced; their edges come from "
                   "the stored filing")
    if counts.get("sec_unavailable"):
        out.append(f"SEC stopped answering; {counts.get('pending', 0)} tickers were left pending")
    return out


def record(edges: list[dict], *, run_id: str, run_at: float, kept: bool = False, counts: dict | None = None,
           filings: int | None = None, payload: dict | None = None):
    """Record a ``chain_mentions`` write: a header for every run, with the edges the table holds after it; a degraded
    run is partial. ``counts`` is the Fetch's ``fetch_corpus`` result or the sync's counts. Returns rows written."""
    counts = dict(counts or {})
    return history.record_run(
        BOARD, run_id=run_id, run_at=run_at, model_version=model_version(), rows={TABLE: list(edges)},
        params=effective_config(),
        context={"trigger": "fetch" if payload is not None else "sync", "filings": filings, "edges": len(edges),
                 "kept": kept, "counts": counts},
        payload=_without_catalog(payload), partial="; ".join(_problems(edges, kept, filings, counts)) or None)


def _without_catalog(payload: dict | None) -> dict | None:
    """The payload as history keeps it: without ``theme_catalog``, the Theme facet's vocabulary, which the board reads
    from the pinned theme run at render time (#383 C1b) and is not part of the Fetch's result."""
    if payload is None:
        return None
    return {k: v for k, v in payload.items() if k != "theme_catalog"}
