"""What an options board asks of the builder, and which names it may show (G9, build plan PR 19).

* ``option_list_names``: SPX and the option watch list, the names ``uw/option_list`` fetches every evening
  (``rcdata/build/options/names.py``). Implied Probability, LEAPS and Earnings Radar show any other name as
  ``NOT_ON_LIST`` and give it no score.
* ``builder_note``: a board's Fetch puts one build request into the queue (D1) and says what happened, in one line;
  ``builder_notes`` does it for each dataset of a board that reads several (Earnings Radar: the list and the
  screener).
  Before the dataset's first run (the server thread's, after a backup: such a dataset ``starts_by_hand``) nothing is
  queued, so a board press is never the store's first write. A locked or broken ``ops.sqlite`` is a note, never a
  failed Fetch.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

OPTION_LIST = "uw/option_list"
OPTION_SCREENER = "uw/option_screener"
NOT_ON_LIST = "not on the option watch list"


def option_list_names() -> list[str]:
    """SPX, then the option watch list in ticker order; SPX alone when ``ops.sqlite`` cannot be read."""
    from RevenueChaser.rcdata.build.options import names
    try:
        return names.list_names()
    except Exception as exc:  # noqa: BLE001 - an unreadable ops.sqlite still leaves SPX
        logger.warning("option watch list not read: %s", exc)
        return list(names.ALWAYS)


def request(dataset: str, by: str) -> str:
    """Queue one build of ``dataset`` unless one is queued or running; the state in a few words."""
    from RevenueChaser.rcdata import ops
    try:
        if not ops.ever_built(dataset):
            return "not queued: it has not had its first run yet (the server thread starts it after a backup)"
        return "queued" if ops.enqueue_once(dataset, None, by=by) is not None else "already queued"
    except Exception as exc:  # noqa: BLE001 - the board renders what is stored either way
        logger.warning("builder request for %s not queued: %s", dataset, exc)
        return f"not queued ({type(exc).__name__}: {exc})"[:200]


def builder_note(dataset: str, by: str) -> str:
    """``request`` as the board's one line: ``Builder request: <dataset> <state>.``"""
    return f"Builder request: {dataset} {request(dataset, by)}."


def builder_notes(datasets, by: str) -> str:
    """``request`` for each dataset as one line: ``Builder request: <a> <state>, <b> <state>.``"""
    return "Builder request: " + ", ".join(f"{d} {request(d, by)}" for d in datasets) + "."
