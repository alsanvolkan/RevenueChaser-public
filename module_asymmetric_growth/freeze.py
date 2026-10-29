"""Asymmetric Growth board -> forecast ledger rows (#128).

``freeze_result`` is the last step of an AG Fetch (``app/runners.run_asymmetric_growth``): it applies the
shared freeze rule (``core.ledger.freeze_board``) to the scan just made and appends every shown card to
``forecast_ledger``. The mapping lives here, in the module, not in ``core/``.

AG's momentum is read from the stored bars up to the scan's session (#380 S4; the prices store's since PR 11), so
the session is complete for the board when the prices store holds that session's SPY bar, read after the close +
30 min: the anchor is ``core.ledger.prices_has_session`` (PR 10), the same store Swing and Fundamentals check — not
the universe snapshot behind the quotes (the quote only fills a card price) and not ``ohlc_gaps`` (one gap in one
name must not refuse the board). AG also has no fixture / no-data flag, so it brings its own guards
(``run_problem``): a partial universe pull and too many scoring errors. The history's "partial" rule is the same
function (review N-1), so a run the history keeps whole is the run the freeze writes.

Each pick carries its card's own price session (``period_ends["price_bar"]``, review SF-1), so a card priced from an
older session than the board's is told apart in the ledger too.
"""
from __future__ import annotations

from RevenueChaser.core import ledger
from RevenueChaser.module_asymmetric_growth import engine

SOURCE = "asymmetric_growth"
LABEL = "AG"


def effective_config() -> dict:
    """What the board scores with: the default thresholds, the default market-cap range and the C18 rules."""
    return {"THRESHOLDS": engine.THRESHOLDS, "DEFAULT_MC_RANGE": list(engine.DEFAULT_MC_RANGE),
            "RULES": dict(engine.RULES)}


def model_version() -> str:
    return ledger.model_version(engine.SELECTION_VERSION, effective_config())


def bucket(card: dict) -> str:
    """The ledger bucket: tier and points bucket, e.g. ``tier:3|pts_9`` or ``tier:none|pts_lt5`` (#380 S2)."""
    tier = card.get("tier")
    return f"tier:{tier if tier in (2, 3) else 'none'}|{engine._points_bucket(card['total_points'])}"


def picks(cards: list[dict]) -> list[dict]:
    """The board's shown cards as ledger picks. ``rank`` is the 1-based position on the board."""
    return [{
        "subject": c["ticker"],
        "rank": position,
        "score": c.get("total_points"),
        "bucket": bucket(c),
        "market_cap": c.get("market_cap"),
        "sector": c.get("sector"),
        "shown_price": c.get("price"),
        "stop": None,
        "target": None,
        "price_session": (c.get("period_ends") or {}).get("price_bar"),
    } for position, c in enumerate(cards, start=1)]


def _score_error_problem(result: dict) -> str | None:
    summary = result.get("summary") or {}
    attempted = int(result.get("universe_total") or 0) - int(summary.get("excluded_types") or 0)
    if attempted <= 0:
        return "no ticker was attempted"
    errors = int(summary.get("score_errors") or 0)
    if errors > engine.FREEZE_MAX_SCORE_ERROR_RATE * attempted:
        return (f"{errors:,} of {attempted:,} tickers failed to score (scoring errors above "
                f"{engine.FREEZE_MAX_SCORE_ERROR_RATE:.0%})")
    return None


def run_problem(result: dict) -> str | None:
    """Why a scan with cards is not a whole run, or ``None``: the universe pull was partial, or too many tickers
    failed to score. The freeze's ``payload_check`` and the history's ``_partial`` both ask this (review N-1)."""
    if result.get("universe_partial"):
        return "the universe pull was partial"
    return _score_error_problem(result)


def _make_anchor(result: dict):
    """The freeze anchor (#380 S4, decision 4): SPY's own bar for the session, in the prices store since PR 10.

    ``result`` is kept in the signature (``history.py`` calls ``_make_anchor(result)``) but the anchor does not
    read the payload: the board's momentum is the stored bars', so the board is as fresh as the session's SPY bar.
    """
    def anchor(session) -> str | None:
        if ledger.prices_has_session(session, "SPY"):
            return None
        return f"no SPY bar in the prices store for the {session} session yet, read after the close + 30 min"
    return anchor


def freeze_result(result: dict, *, run_id: str | None = None, now=None) -> ledger.Outcome:
    """Freeze a finished AG scan when the rule allows it; the outcome is also a job note."""
    return ledger.freeze_board(
        SOURCE, LABEL, result, rows_key="cards", to_rows=picks, model_version=model_version(),
        run_id=run_id, now=now, check_spy=False, payload_check=run_problem,
        anchor=_make_anchor(result))
