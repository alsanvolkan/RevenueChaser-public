"""The internal data API: a board asks for a **need** by name and gets the store's rows in an envelope.

One front for the dashboards (the goal of 3 Oct 2026): a board names what it needs, not which table or provider it
comes from, and the answer says how fresh it is, how much of what was asked the store holds, where each row came
from, and — from PR C — whether a fill was queued.

Two hard rules, both under test:

- **The API runs no SQL and owns no store.** Every need points at an existing ``rcdata.read`` function; the envelope
  wraps the rows, never the query (``test_every_need_points_at_a_read_function``, ``test_the_api_package_writes_no_sql``).
- **The API calls no provider and writes to no serving file.** When something is missing or stale it will (PR C)
  queue the fill through ``ops.enqueue_once`` and let the builder do it, so the single-writer rule (D1) holds. In
  this PR only ``fill="never"`` exists: the answer serves what the store has and says how old it is.

Freshness lives in one place, the catalog's ``max_lag``, and the catalog is its single owner (avo, 29 Oct 2026):
the constants spread over the boards today (a calendar's 1 day, short interest's 3, estimates' 14, prices' 14) fold
into it as the boards migrate, and a board's model version reads the number from here rather than keeping a copy.
Each need carries one number and the unit it is counted in — sessions, or calendar days for a build that has no
session of its own.
"""
from __future__ import annotations

from . import boards, catalog, serve
from .catalog import NEEDS, Need
from .serve import Answer, FillNotSupported, UnknownNeed, get, lag_in, lag_sessions, summary

__all__ = ["Answer", "FillNotSupported", "NEEDS", "Need", "UnknownNeed", "boards", "catalog", "get",
           "lag_in", "lag_sessions", "serve", "summary"]
