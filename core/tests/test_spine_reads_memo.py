"""Opus review S6 (PR 6): one board render no longer opens the spine file a dozen times.

On the spine default, the reads behind one render (the registry, the filter labels, the universe counts, the
classification universe, the security types, the card badges) each opened ``spine.duckdb`` and pulled the same
7.7k-row listed universe again: 16 opens and about 1 s per render on the real-data scratch spine of 28 Oct. The
spine changes only when a build swaps the file, so ``core.spine_reads`` keeps the answers until the file changes.
"""
from __future__ import annotations

import os

import pytest

from RevenueChaser.core import index_registry as ir
from RevenueChaser.core import spine_reads, universe
from RevenueChaser.rcdata import serving
from RevenueChaser.rcdata.tests.conftest import http_mock, market_roots  # noqa: F401 - market roots per test
from RevenueChaser.rcdata.tests.test_spine_security import built, run_all  # noqa: F401 - the 10 names


@pytest.fixture
def opens(monkeypatch, built):
    monkeypatch.setenv(universe.SOURCE_ENV, "spine")
    spine_reads.clear()
    seen = []
    real = serving.open_read

    def spy(kind):
        seen.append(kind)
        return real(kind)
    monkeypatch.setattr(serving, "open_read", spy)
    return seen


def _render():
    cards = [{"ticker": t} for t in ("MSFT", "HEI", "NOPE")]
    ir.load_registry()
    ir.filter_labels()
    universe.universe_counts(3e8)
    universe.classification_universe(3e8)
    universe.filter_fields(["MSFT", "HEI"])
    ir.annotate_cards(cards)
    ir.annotate_cards(cards)
    return cards


def test_a_render_reads_each_spine_answer_once(opens):
    first = _render()
    n = len(opens)
    assert n <= 4, opens                                     # universe, membership, identity (+ provenance)
    assert _render() == first and len(opens) == n           # a second render opens nothing


def test_a_rebuilt_file_is_read_again(opens):
    _render()
    n = len(opens)
    path = serving.live_path("spine")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))       # what a swap looks like to stat
    _render()
    assert len(opens) > n


def test_a_caller_cannot_change_the_kept_rows(opens):
    rows = spine_reads.universe(("A", "B"))
    rows[0]["ticker"] = "CHANGED"
    rows.append({"ticker": "EXTRA"})
    again = spine_reads.universe(("A", "B"))
    assert "CHANGED" not in {r["ticker"] for r in again} and "EXTRA" not in {r["ticker"] for r in again}
