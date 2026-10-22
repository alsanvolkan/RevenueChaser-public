"""``docs/open_issues.json`` is a valid, complete map of this repo's open issues (#383).

The registry is the panel's only data source (``core/open_issues.py``), so it is held to the same bar as the code
that reads it: known board keys, known scopes/kinds/statuses, unique ``(repo, number)``, links only to this owner,
and the density bar of the build brief (a summary a reader can act on, and the three fields that make an entry
resumable months later).

The registry is seeded from the open issues of both repositories (48 entries on 2026-10-22, public numbers); these tests
check its structure, not a hand-copied list of issue numbers, so a re-seed or a migration of issues does not make them stale.
"""
from __future__ import annotations

import re
from datetime import datetime

import pytest

from RevenueChaser.core import open_issues

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


def test_every_issue_appears_once(registry):
    """Each ``(repo, number)`` is in the registry once, and the entry's url points at that issue."""
    keys = [(e.repo, e.number) for e in registry.issues]
    assert len(keys) == len(set(keys)), sorted(k for k in set(keys) if keys.count(k) > 1)
    for e in registry.issues:
        assert e.url.lower().endswith(f"/{e.repo}/issues/{e.number}".lower()), (e.repo, e.number, e.url)


def test_every_board_named_is_a_real_dashboard_tab(registry):
    """A ``boards`` value that is not a tab key would never show: a curation slip shows up here."""
    from RevenueChaser.app import dashboard

    tabs = {t["key"] for t in dashboard.TABS if t["type"] == "tab"}
    for e in registry.issues:
        assert set(e.boards) <= tabs, (e.repo, e.number, sorted(set(e.boards) - tabs))
        assert (e.scope == "board") == bool(e.boards), (e.repo, e.number, e.scope, e.boards)


def test_approved_is_a_status_and_sorts_between_in_flight_and_open():
    """The seeded registry carries issues whose build is approved but not started (status ``approved``)."""
    from RevenueChaser.core import open_issues

    assert "approved" in open_issues.STATUSES
    order = open_issues.STATUS_ORDER
    assert order["in-flight"] < order["approved"] < order["open"]
