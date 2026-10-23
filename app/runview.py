"""The run log: what ``job_runs`` holds about the jobs this store has seen (``/view/runs``, ``GET /api/runs``).

Two writers fill the table and until now nothing read it (#26 PL5-b): the dashboard's own jobs
(``app/jobs._record``, #282 J4) and the scheduled tasks (``core/automations.run_task``, #129 A1). This module
is the only reader, and it is read-only — no route here starts, retries or deletes anything.

It reports a row as stored rather than as it would like it to be:

* a dashboard-written row carries no ``trigger``, ``source``, ``registry_name`` or ``counts`` (#83), so those
  come back ``None`` instead of being inferred from the lane the row does carry;
* a row whose end write was skipped stays ``running`` for ever (#82), so it is listed as running with its age
  and not quietly re-labelled.

The page explains both in a footnote, which is the honest version until those two land.
"""
from __future__ import annotations

import json
import logging

from markupsafe import Markup

from RevenueChaser.core import rawstore, templating
from RevenueChaser.core.html_safe import script_json

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 200
MAX_LIMIT = 1000
POLL_PATH = "/api/runs"
# What the page shows, in one place: the SELECT and the payload follow it.
COLUMNS = ("run_id", "module", "lane", "trigger", "source", "registry_name", "pid", "params",
           "queued_at", "started_at", "finished_at", "status", "error", "message", "counts")
_JSON_COLUMNS = ("params", "counts")
# A run that never started sorts on the moment it was queued, so a queued or lost job stays at the top
# where it can be seen, instead of falling to the bottom under a NULL.
_ORDER = "coalesce(started_at, queued_at, 0) DESC"


def clamp_limit(value) -> int:
    """How many rows to read: a bad or missing value falls back, a silly one is capped."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(n, MAX_LIMIT))


def _shape(row: dict) -> dict:
    """Decode the JSON columns and add the duration the page shows; nothing else is derived."""
    for col in _JSON_COLUMNS:
        value = row.get(col)
        if isinstance(value, str):
            try:
                row[col] = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                pass          # keep the text as it is: the page renders it as text either way
    started, finished = row.get("started_at"), row.get("finished_at")
    row["duration_s"] = finished - started if started is not None and finished is not None else None
    return row


def read(limit=DEFAULT_LIMIT) -> dict:
    """What ``/api/runs`` returns: the newest runs, and how many rows the table holds in all.

    A missing table or store file reads as no rows (``rawstore.query`` is lenient), so the tab opens on a
    fresh machine instead of erroring.
    """
    n = clamp_limit(limit)
    rows = [_shape(r) for r in rawstore.query(
        f"SELECT {', '.join(COLUMNS)} FROM job_runs ORDER BY {_ORDER} LIMIT {n}")]
    total = rawstore.query("SELECT count(*) AS n FROM job_runs")
    return {"rows": rows, "total": total[0]["n"] if total else 0, "shown": len(rows), "limit": n}


def page_payload() -> dict:
    """What the Runs page is rendered from: the first page of rows, so the first view needs no poll."""
    return {"initial": read()}


def render_page(payload: dict) -> str:
    # script_json output is the one trusted fragment: Markup keeps autoescape from turning its quotes into
    # entities, which an inline <script> would not decode. Every value in it reaches the page as textContent.
    return templating.render("runs.html.j2",
                             initial_json=Markup(script_json(payload.get("initial") or {})))
