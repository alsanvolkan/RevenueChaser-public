"""Opus review S2 (PR 6): the identity refresh's SPDR check reads the stored S&P 500 list, whatever the boards read,
and says when it checked nothing.

After the cutover ``index_registry.load_index("sp500")`` answers from the spine's fund files, so the check (part of a
job that writes the live store) had silently taken a dependency on the spine. It is the old path's own check and
``index_lists`` keeps that list current weekly for exactly this, so it reads ``index_lists.stored_index("sp500")``.
With no list at all it used to report "0 conflicts" as if it had checked; the run now records ``spdr_members`` and
the summary says the check did not run.
"""
from __future__ import annotations

import pytest

from RevenueChaser.core import index_lists as il
from RevenueChaser.core import index_registry, universe
from RevenueChaser.core.identity import refresh as R
from RevenueChaser.core.identity.sources import SourceResult

_HOLD = SourceResult("spdr", "ok", [{"ticker": "AAPL", "fund": "XLK", "sector": "Technology"}], None,
                     "2026-10-28T00:00:00+00:00")
_MASTER = [{"ticker": "AAPL", "sector": "Technology"}]


def test_the_check_reads_the_stored_list_not_the_spine(monkeypatch, rawstore_tmp_db):
    il.save_index("sp500", {"AAPL"})
    monkeypatch.setattr(index_registry, "load_index", lambda slug: pytest.fail("the boards' reader was asked"))
    _, counts = R._spdr_check(_HOLD, _MASTER)
    assert counts == {"spdr_checked": 1, "spdr_agree": 1, "spdr_members": 1}


def test_no_stored_list_is_not_checked_not_clean(rawstore_tmp_db):
    _, counts = R._spdr_check(_HOLD, _MASTER)
    assert counts == {"spdr_checked": 0, "spdr_agree": 0, "spdr_members": 0}
    line = R.summary({"status": "ok", "counts": {**counts, "conflicts": 0},
                      "sources": {s: {"selected": False, "status": "ok"} for s in R.SOURCES + R.CHECKS}})
    assert "SPDR not checked (no stored S&P 500 list)" in line


def test_the_module_docstring_names_the_source():
    assert "index_lists.stored_index" in R.__doc__
    assert universe  # imported for the reader of this file: the check is independent of RC_UNIVERSE_SOURCE
