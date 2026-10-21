"""Runtime theme SSoT (``.store/raw.duckdb`` -> ``ticker_themes``).

One row per ``(ticker, theme_id, theme_run_id)``. ``theme_id`` is a stable slug
(the join key); ``theme`` is the display label. Phase 1 materializes the legacy
keyword themes from ``ticker_scores.themes`` under
``theme_run_id='legacy-keyword'``, ``level='legacy'`` so the vocabulary can be
replaced by the NMF runs (Phase 8) without losing the old assignments.

    python -m RevenueChaser.core.ticker_themes --backfill
"""
from __future__ import annotations

import json
import logging
import re

from . import rawstore

logger = logging.getLogger(__name__)

LEGACY_RUN_ID = "legacy-keyword"
LEGACY_LEVEL = "legacy"

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slug(label: str) -> str:
    """Stable theme id from a display label (``"Biotechnology"`` -> ``"biotechnology"``)."""
    return _SLUG_RE.sub("-", (label or "").strip().lower()).strip("-")


def parse_themes(raw) -> list[str]:
    """Display labels from a ``ticker_scores.themes`` cell.

    Accepts the stored JSON text, a list, or ``None``. Returns ``[]`` for
    anything that is not a non-empty list of non-blank strings.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
    if not isinstance(raw, list):
        return []
    return [str(x).strip() for x in raw if str(x or "").strip()]


def _rows_for(ticker: str, raw, run_id: str = LEGACY_RUN_ID,
              level: str = LEGACY_LEVEL) -> list[dict]:
    """One row per distinct theme slug for ``ticker`` (drops duplicates)."""
    seen: set[str] = set()
    rows: list[dict] = []
    for label in parse_themes(raw):
        tid = slug(label)
        if not tid or tid in seen:
            continue
        seen.add(tid)
        rows.append({"ticker": ticker, "theme_id": tid, "theme": label,
                     "level": level, "theme_run_id": run_id})
    return rows


def backfill_legacy() -> dict:
    """Materialize ``ticker_scores.themes`` into ``ticker_themes``.

    Idempotent: the legacy run's rows are deleted and rewritten in one
    transaction, so a re-run never duplicates. Empty ``[]`` writes no row.
    """
    rawstore.ensure_table("ticker_themes")
    src = rawstore.query(
        "SELECT ticker, themes FROM ticker_scores "
        "WHERE themes IS NOT NULL AND themes <> ''")
    rows: list[dict] = []
    for r in src:
        rows.extend(_rows_for(r["ticker"], r["themes"]))
    with rawstore.transaction() as tx:
        tx.execute("DELETE FROM ticker_themes WHERE theme_run_id = ?", [LEGACY_RUN_ID])
        if rows:
            tx.write_rows("ticker_themes", rows)
    return {"tickers": len({r["ticker"] for r in rows}), "rows": len(rows),
            "run_id": LEGACY_RUN_ID}


def main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="ticker_themes runtime theme store")
    p.add_argument("--backfill", action="store_true",
                   help="materialize legacy keyword themes from ticker_scores")
    args = p.parse_args(argv)
    if args.backfill:
        print(backfill_legacy())
    else:
        print(backfill_legacy.__doc__ or "ticker_themes")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
