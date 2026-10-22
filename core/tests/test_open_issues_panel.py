"""The "Open issues" group in the dashboard shell (#383): splitting, ordering, counts and escaping.

The panel is parent chrome, so the shell renders it from one embedded JSON payload
(``reporting_common.open_issues_payload``) and re-renders it in ``showTab`` without a page load. This tier checks what
Python owns — the split by scope, the order, the counts the payload implies, the link allowlist and the failure
convention. What the browser makes of the strings is ``module_testing/e2e/test_open_issues_panel.py``.
"""
from __future__ import annotations

import json

import pytest

from RevenueChaser.core import open_issues, reporting_common

BOARD = "module_sec_13f"
SHARED = 9
REPO = 4


def _entry(repo, number, scope, *, boards=(), kind="bug", status="open", deferred_until="",
           title=None, summary=None, refs=(), detail=None, issue_updated_at="2026-10-20T00:00:00Z",
           reviewed_at="2026-10-22"):
    detail = detail if detail is not None else {
        "whats_wrong": f"What is wrong in {repo}#{number}.",
        "evidence": f"Evidence for {repo}#{number}.",
        "example": f"Example for {repo}#{number}.",
        "tried_or_decided": "", "direction": f"Direction for {repo}#{number}.",
        "next_step": f"Next step for {repo}#{number}.",
    }
    return {
        "repo": repo, "number": number,
        "url": f"{open_issues.URL_PREFIX}{repo}/issues/{number}",
        "title": title or f"Issue {number} of {repo}",
        "state": "open", "labels": [], "boards": list(boards), "scope": scope, "kind": kind,
        "status": status, "deferred_until": deferred_until,
        "summary": summary or f"A one-sentence summary for {repo} issue number {number} that is long enough.",
        "detail": detail, "refs": list(refs),
        "issue_updated_at": issue_updated_at, "reviewed_at": reviewed_at,
    }


def _registry(entries, generated_at="2026-10-22T10:30:00Z", generated_from="3670e76"):
    return {"generated_at": generated_at, "generated_from": generated_from, "issues": entries}


def _write(tmp_path, payload, name="open_issues.json"):
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _full_registry():
    """6 board entries for SEC 13F, 9 shared, 4 repo — the counts of the build brief §3.2/§3.3."""
    board = [
        _entry("RevenueChaser-public", 1, "board", boards=[BOARD]),                       # bug, open
        _entry("RevenueChaser", 2, "board", boards=[BOARD], kind="epic", status="deferred"),
        _entry("RevenueChaser", 3, "board", boards=[BOARD], status="needs-decision"),
        _entry("RevenueChaser", 4, "board", boards=[BOARD], kind="known-problem", status="in-flight"),
        _entry("RevenueChaser", 5, "board", boards=[BOARD], status="open",
               issue_updated_at="2026-10-23T00:00:00Z"),                                   # curated before the change
        _entry("RevenueChaser-public", 6, "board", boards=[BOARD], kind="seam", status="open"),
    ]
    shared = [_entry("RevenueChaser", 100 + i, "shared", kind=k)
              for i, k in enumerate(("bug", "known-problem", "seam", "enhancement", "docs",
                                     "epic", "idea", "investigate", "bug"))]
    repo = [_entry("RevenueChaser", 200 + i, "repo", kind=k, status=s)
            for i, (k, s) in enumerate((("docs", "open"), ("bug", "open"),
                                        ("epic", "open"), ("idea", "deferred")))]
    return _registry(board + shared + repo)


@pytest.fixture
def registry(tmp_path):
    return open_issues.load(_write(tmp_path, _full_registry()))


def test_the_payload_splits_by_scope(registry):
    got = open_issues.for_board(BOARD, registry)
    assert [len(got[s]) for s in ("board", "shared", "repo")] == [6, SHARED, 0]
    assert {e["scope"] for e in got["board"]} == {"board"}
    assert {e["scope"] for e in got["shared"]} == {"shared"}
    # Settings / Data health is the one board that also shows the repository-wide entries.
    settings = open_issues.for_board("module_settings", registry)
    assert (len(settings["board"]), len(settings["shared"]), len(settings["repo"])) == (0, SHARED, REPO)


def test_a_board_with_no_board_issue_still_gets_the_shared_ones(registry):
    got = open_issues.for_board("module_oipd", registry)
    assert len(got["board"]) == 0 and len(got["shared"]) == SHARED and got["repo"] == []


@pytest.mark.parametrize("key", ["logs", "dealer_hedge"])
def test_not_a_data_tab_gets_nothing(registry, key):
    assert not open_issues.for_board(key, registry)


def test_the_order_is_status_then_kind_then_number(registry):
    got = open_issues.for_board(BOARD, registry)
    assert [e["number"] for e in got["board"]] == [3, 4, 1, 6, 5, 2]
    assert [open_issues.effective_status(_as_entry(e)) for e in got["board"]] == [
        "needs-decision", "in-flight", "open", "open", "needs-review", "deferred"]


def test_an_issue_changed_after_curation_reads_needs_review(registry):
    changed = next(e for e in open_issues.for_board(BOARD, registry)["board"] if e["number"] == 5)
    assert changed["status"] == "needs-review"
    assert changed["stored_status"] == "open"


def test_a_foreign_link_is_dropped_and_counted(tmp_path):
    good = {"label": "issue", "url": f"{open_issues.URL_PREFIX}RevenueChaser/issues/7"}
    bad = {"label": "elsewhere", "url": "https://example.com/not-ours"}
    payload = _registry([
        _entry("RevenueChaser", 7, "board", boards=[BOARD], refs=[good, bad]),
        dict(_entry("RevenueChaser", 8, "board", boards=[BOARD]), url="https://example.com/8"),
    ])
    got = open_issues.for_board(BOARD, open_issues.load(_write(tmp_path, payload)))
    assert [e["number"] for e in got["board"]] == [7]
    assert [r["url"] for r in got["board"][0]["refs"]] == [good["url"]]
    assert got["dropped_links"] == 2


def test_a_missing_or_malformed_registry_raises_a_readable_error(tmp_path):
    with pytest.raises(open_issues.RegistryError, match="no issue registry"):
        open_issues.load(tmp_path / "absent.json")
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(open_issues.RegistryError, match="unreadable"):
        open_issues.load(broken)
    wrong = _write(tmp_path, {"generated_at": "2026-10-22T00:00:00Z", "issues": [{"repo": "x"}]},
                   name="wrong.json")
    with pytest.raises(open_issues.RegistryError, match="generated_from"):
        open_issues.load(wrong)
    unknown = _write(tmp_path, _registry([
        dict(_entry("RevenueChaser", 9, "board", boards=[BOARD]), scope="galaxy")]), name="scope.json")
    with pytest.raises(open_issues.RegistryError, match="unknown scope"):
        open_issues.load(unknown)


# ── the shell ────────────────────────────────────────────────────────────────

@pytest.fixture
def shell(tmp_path, monkeypatch):
    """The dashboard page rendered with a controlled registry."""
    from RevenueChaser.app import dashboard

    def _render(payload):
        monkeypatch.setattr(open_issues, "REGISTRY_PATH", _write(tmp_path, payload))
        dashboard.app.config["TESTING"] = True
        r = dashboard.app.test_client().get("/")
        assert r.status_code == 200
        return r.get_data(as_text=True)
    return _render


def test_the_shell_embeds_one_payload_with_the_boards_counts(shell):
    html = shell(_full_registry())
    assert html.count('id="rc-open-issues-data"') == 1
    blob = html.split('id="rc-open-issues-data">', 1)[1].split("</script>", 1)[0]
    data = json.loads(blob)
    from RevenueChaser.app import dashboard
    data_boards = [t["key"] for t in dashboard.TABS
                   if t["type"] == "tab" and t["key"] not in open_issues.NOT_A_DATA_BOARD]
    assert sorted(data) == sorted(data_boards)
    assert len(data[BOARD]["board"]) == 6 and len(data[BOARD]["shared"]) == SHARED
    assert len(data["module_settings"]["repo"]) == REPO
    assert not data["module_oipd"]["board"]
    assert "logs" not in data and "dealer_hedge" not in data

    # The counts in the summary line come from exactly these lists.
    assert "on this board" in html and "as of " in html and "' shared'" in html
    assert "This board (" in html and "Affects every board (" in html and "Repository-wide (" in html
    assert "No board-specific open issue." in html


def test_a_hostile_title_and_summary_cannot_leave_the_script_block(shell):
    hostile_title = "</script><script>alert(1)</script>"
    hostile_line = '"><img src=x onerror=alert(1)>'
    payload = _registry([_entry("RevenueChaser", 11, "board", boards=[BOARD],
                                title=hostile_title, summary=hostile_line)])
    html = shell(payload)
    assert hostile_title not in html
    assert "<img src=x onerror=alert(1)>" not in html
    blob = html.split('id="rc-open-issues-data">', 1)[1].split("</script>", 1)[0]
    assert json.loads(blob)[BOARD]["board"][0]["title"] == hostile_title     # the text itself survives
    # and the client renderer escapes what it puts into innerHTML (#175's one escaper)
    assert "rcEsc(it.title)" in html              # the title
    assert "return rcEsc(text).replace" in html  # and every summary/detail line (rcOiText)
    assert "function rcEsc(" in html


def test_a_missing_registry_renders_a_message_not_a_500(shell):
    html = shell({})          # no generated_at, no issues: load() refuses it
    assert 'id="rc-open-issues-error"' in html
    assert "issue registry" in html
    assert open_issues.NOT_A_DATA_BOARD.isdisjoint(json.loads(
        html.split('id="rc-open-issues-data">', 1)[1].split("</script>", 1)[0]))


def test_the_payload_route_is_json_ready(registry):
    """``reporting_common.open_issues_payload`` is the shell's single entry point."""
    payload = reporting_common.open_issues_payload([BOARD, "logs"])
    assert set(payload) == {BOARD}
    assert json.loads(json.dumps(payload)) == payload
    assert payload[BOARD]["dropped_links"] == 0


def _as_entry(d: dict) -> open_issues.Entry:
    return open_issues._as_entry(d)
