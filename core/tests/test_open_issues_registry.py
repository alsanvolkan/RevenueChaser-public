"""``docs/open_issues.json`` is a valid, complete map of this repo's open issues (#383).

The registry is the panel's only data source (``core/open_issues.py``), so it is held to the same bar as the code
that reads it: known board keys, known scopes/kinds/statuses, unique ``(repo, number)``, links only to this owner,
and the density bar of the build brief (a summary a reader can act on, and the three fields that make an entry
resumable months later).

The coverage check uses a **fixture** list of the 44 open issues (13 public, 31 private) read 2026-10-22 and mapped
in the build plan §3 — never the network. ``OPEN_ISSUES_PENDING`` names the entries this starter registry does not
carry yet: seeding them needs a GitHub token, which the build worker does not have. The first
``scripts/open_issues_sync.py --write`` run adds them, and the test fails until that list is emptied.
"""
from __future__ import annotations

import re
from datetime import datetime

import pytest

from RevenueChaser.core import open_issues

# (repo, number, scope, boards, kind) — plan.md §3, verbatim.
OPEN_ISSUES = [
    # public #6-#18
    ("RevenueChaser-public", 6, "shared", [], "bug"),
    ("RevenueChaser-public", 7, "shared", [], "bug"),
    ("RevenueChaser-public", 8, "shared", [], "bug"),
    ("RevenueChaser-public", 9, "board", ["module_sec_13f"], "investigate"),
    ("RevenueChaser-public", 10, "board", ["module_sec_13f"], "bug"),
    ("RevenueChaser-public", 11, "board", ["module_sec_13f"], "known-problem"),
    ("RevenueChaser-public", 12, "board", ["module_sector_rotation", "module_relatedness"], "seam"),
    ("RevenueChaser-public", 13, "board", ["module_earnings_radar", "module_sector_rotation"], "seam"),
    ("RevenueChaser-public", 14, "board", ["module_spx_gamma", "module_earnings_radar"], "seam"),
    ("RevenueChaser-public", 15, "board", ["module_financials", "module_asymmetric_growth"], "seam"),
    ("RevenueChaser-public", 16, "board", ["module_asymmetric_growth", "module_sec_13f"], "seam"),
    ("RevenueChaser-public", 17, "board", ["module_earnings_radar", "module_sector_rotation"], "seam"),
    ("RevenueChaser-public", 18, "board", ["module_asymmetric_growth", "module_swing_potential",
                                           "module_fundamentals"], "seam"),
    # private — board-scoped
    ("RevenueChaser", 372, "board", ["module_earnings_radar"], "epic"),
    ("RevenueChaser", 374, "board", ["module_sector_rotation"], "epic"),
    ("RevenueChaser", 375, "board", ["module_sec_13f"], "epic"),
    ("RevenueChaser", 376, "board", ["module_spx_gamma"], "epic"),
    ("RevenueChaser", 377, "board", ["module_leaps"], "epic"),
    ("RevenueChaser", 379, "board", ["module_comovement"], "epic"),
    ("RevenueChaser", 380, "board", ["module_asymmetric_growth"], "epic"),
    ("RevenueChaser", 381, "board", ["module_settings"], "epic"),
    ("RevenueChaser", 384, "board", ["module_swing_potential"], "epic"),
    ("RevenueChaser", 385, "board", ["module_financials"], "epic"),
    ("RevenueChaser", 386, "board", ["module_fundamentals"], "epic"),
    ("RevenueChaser", 387, "board", ["module_value_chain"], "epic"),
    ("RevenueChaser", 388, "board", ["module_relatedness"], "epic"),
    ("RevenueChaser", 431, "board", ["module_settings", "module_value_chain"], "bug"),
    ("RevenueChaser", 435, "board", ["module_financials", "module_fundamentals"], "bug"),
    ("RevenueChaser", 450, "board", ["module_financials", "module_fundamentals"], "bug"),
    ("RevenueChaser", 289, "board", ["module_settings"], "docs"),
    ("RevenueChaser", 309, "board", ["module_settings"], "enhancement"),
    ("RevenueChaser", 342, "board", ["module_settings"], "bug"),
    ("RevenueChaser", 62, "board", ["module_asymmetric_growth", "module_swing_potential",
                                    "module_fundamentals"], "enhancement"),
    ("RevenueChaser", 129, "board", ["module_asymmetric_growth", "module_swing_potential",
                                     "module_fundamentals"], "enhancement"),
    # private — shared
    ("RevenueChaser", 371, "shared", [], "epic"),
    ("RevenueChaser", 383, "shared", [], "epic"),
    ("RevenueChaser", 433, "shared", [], "bug"),
    ("RevenueChaser", 364, "shared", [], "bug"),
    ("RevenueChaser", 365, "shared", [], "bug"),
    ("RevenueChaser", 124, "shared", [], "enhancement"),
    # private — repo-wide
    ("RevenueChaser", 45, "repo", [], "docs"),
    ("RevenueChaser", 257, "repo", [], "bug"),
    ("RevenueChaser", 402, "repo", [], "epic"),
    ("RevenueChaser", 449, "repo", [], "idea"),
]

# Entries the hand-written starter registry cannot carry yet: seeding them needs a GitHub token (the
# private repo) and the issues' own bodies, which this offline build worker has neither of. `--write`
# fills them from the API; empty this set in the same commit that lands the real seed.
OPEN_ISSUES_PENDING = {
    ("RevenueChaser", 372), ("RevenueChaser", 374), ("RevenueChaser", 375), ("RevenueChaser", 376),
    ("RevenueChaser", 377), ("RevenueChaser", 379), ("RevenueChaser", 380), ("RevenueChaser", 381),
    ("RevenueChaser", 384), ("RevenueChaser", 385), ("RevenueChaser", 386), ("RevenueChaser", 387),
    ("RevenueChaser", 388), ("RevenueChaser", 431), ("RevenueChaser", 435), ("RevenueChaser", 450),
    ("RevenueChaser", 289), ("RevenueChaser", 309), ("RevenueChaser", 342), ("RevenueChaser", 62),
    ("RevenueChaser", 129),
}


@pytest.fixture(scope="module")
def registry() -> open_issues.Registry:
    return open_issues.load()


def test_the_registry_parses_and_has_a_header(registry):
    assert registry.issues, "the registry carries no issue"
    assert re.fullmatch(r"[0-9a-f]{7,40}", registry.generated_from), registry.generated_from
    assert datetime.fromisoformat(registry.generated_at.replace("Z", "+00:00"))


def test_every_board_key_is_a_real_board(registry):
    from RevenueChaser.app import dashboard
    tabs = {t["key"] for t in dashboard.TABS if t["type"] == "tab"}
    for e in registry.issues:
        unknown = set(e.boards) - tabs
        assert not unknown, f"#{e.number}: {sorted(unknown)} is not a dashboard tab"
        assert not set(e.boards) & open_issues.NOT_A_DATA_BOARD, f"#{e.number}: a non-data tab has no panel"


def test_no_duplicate_issue(registry):
    keys = [(e.repo, e.number) for e in registry.issues]
    assert len(keys) == len(set(keys))


def test_every_url_is_this_owner(registry):
    for e in registry.issues:
        assert e.url.startswith(open_issues.URL_PREFIX), e.url
        for ref in e.refs:
            assert ref["url"].startswith(open_issues.URL_PREFIX), ref["url"]


def test_scope_kind_and_status_are_known_values(registry):
    for e in registry.issues:
        assert e.scope in open_issues.SCOPES, (e.number, e.scope)
        assert e.kind in open_issues.KINDS, (e.number, e.kind)
        assert e.status in open_issues.STATUSES, (e.number, e.status)
        assert e.state == "open", (e.number, e.state)


def test_a_board_scoped_entry_names_at_least_one_board(registry):
    for e in registry.issues:
        if e.scope == "board":
            assert e.boards, f"#{e.number} is board-scoped but names no board"
        else:
            assert not e.boards, f"#{e.number} is {e.scope}-scoped but names boards"


def test_the_density_bar(registry):
    for e in registry.issues:
        assert not open_issues.thin_reasons(e), f"#{e.number}: {open_issues.thin_reasons(e)}"


@pytest.mark.parametrize("patch,expected", [
    ({"summary": "too short"}, "summary is"),
    ({"summary": "A summary that is long enough to pass the minimum but\ncarries a newline in it."}, "one line"),
    ({"detail": {"whats_wrong": "x" * 950, "evidence": "e", "next_step": "n"}}, "max 900"),
    ({"detail": {"whats_wrong": "w", "evidence": "e", "next_step": ""}}, "non-empty next_step"),
    ({"kind": "known-problem", "detail": {"whats_wrong": "w", "evidence": "e", "next_step": "n"}},
     "non-empty direction"),
    ({"kind": "epic", "detail": {}}, "epic needs a whats_wrong"),
])
def test_the_density_bar_catches_each_violation(registry, patch, expected):
    """The bar is code, not prose: each rule has to fail on an entry that breaks it."""
    import dataclasses
    entry = dataclasses.replace(registry.issues[0], **patch)
    reasons = open_issues.thin_reasons(entry)
    assert any(expected in r for r in reasons), reasons


def test_the_density_bar_passes_a_good_entry(registry):
    assert open_issues.thin_reasons(registry.issues[0]) == []


def test_every_open_issue_is_mapped(registry):
    """The fixture's 44 issues are either in the registry or named in ``OPEN_ISSUES_PENDING``."""
    mapped = {(e.repo, e.number) for e in registry.issues}
    fixture = {(repo, number) for repo, number, *_ in OPEN_ISSUES}
    assert len(fixture) == 44, len(fixture)
    assert not mapped & OPEN_ISSUES_PENDING, f"seeded but still listed as pending: {sorted(mapped & OPEN_ISSUES_PENDING)}"
    assert mapped | OPEN_ISSUES_PENDING == fixture, (
        f"open issues nobody mapped: {sorted(fixture - mapped - OPEN_ISSUES_PENDING)}; "
        f"mapped but no longer open: {sorted(mapped - fixture)}")


def test_the_seeded_entries_agree_with_the_plan(registry):
    """Scope, boards and kind of every seeded entry match plan.md §3 (a curation slip shows up here)."""
    plan = {(repo, number): (scope, boards, kind) for repo, number, scope, boards, kind in OPEN_ISSUES}
    for e in registry.issues:
        scope, boards, kind = plan[(e.repo, e.number)]
        assert (e.scope, e.boards, e.kind) == (scope, boards, kind), f"#{e.number}"
