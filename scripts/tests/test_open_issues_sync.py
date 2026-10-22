"""``scripts/open_issues_sync.py``: the registry's only writer (#383), tested offline.

The script is the reason the panel is never stale: it reads the open issues of the public repo from the GitHub API, merges
them with the curated registry (the API owns title/state/labels/updated_at, a human owns the mapping and the dense
text), and writes the file back deterministically. Every test here injects a fake fetch — the root ``conftest.py``
blocks real sockets, and a live comparison is the script's own job, not the suite's.
"""
from __future__ import annotations

import json

import pytest

from RevenueChaser.core import open_issues
from RevenueChaser.scripts import open_issues_sync as sync

# The only repo the sync reads.
PUB = "RevenueChaser-public"
# The private repo, which it must never read again: used here to prove an unread repo keeps its entries.
PRIV = "RevenueChaser"


def _api(number, title, *, body="", labels=(), updated_at="2026-10-23T10:00:00Z", repo=PUB):
    return {"number": number, "title": title, "state": "open",
            "labels": [{"name": n} for n in labels],
            "updated_at": updated_at, "body": body,
            "html_url": f"{open_issues.URL_PREFIX}{repo}/issues/{number}"}


def _entry(number, *, repo=PUB, **extra):
    base = {
        "repo": repo, "number": number, "url": f"{open_issues.URL_PREFIX}{repo}/issues/{number}",
        "title": "curated title", "state": "open", "labels": [], "boards": ["module_sec_13f"],
        "scope": "board", "kind": "known-problem", "status": "deferred",
        "deferred_until": "after 2026-10-25",
        "summary": "A curated one-sentence summary that is comfortably over the sixty character minimum.",
        "detail": {"whats_wrong": "curated", "evidence": "curated", "example": "", "tried_or_decided": "",
                   "direction": "curated", "next_step": "curated"},
        "refs": [{"label": "measurement comment",
                  "url": f"{open_issues.URL_PREFIX}{repo}/issues/{number}#issuecomment-1"}],
        "issue_updated_at": "2026-10-22T10:03:48Z", "reviewed_at": "2026-10-22",
    }
    base.update(extra)
    return base


def _registry(tmp_path, entries):
    path = tmp_path / "open_issues.json"
    path.write_text(sync.dumps({"generated_at": "2026-10-20T00:00:00Z", "generated_from": "3670e76",
                                "issues": entries}), encoding="utf-8")
    return path


def _fetch(payload):
    def _fetch(repo, token=""):
        return list(payload.get(repo, []))
    return _fetch


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_write_updates_the_api_fields_and_preserves_the_curated_ones(tmp_path):
    path = _registry(tmp_path, [_entry(11)])
    api = {PUB: [_api(11, "the title has changed", labels=("bug", "13f"))]}
    assert sync.main(["--write", "--registry", str(path)], fetch=_fetch(api), head_sha="abc1234") == 0
    out = _read(path)
    issue = out["issues"][0]
    assert out["generated_from"] == "abc1234"
    assert issue["title"] == "the title has changed"
    assert issue["labels"] == ["bug", "13f"]
    assert issue["issue_updated_at"] == "2026-10-23T10:00:00Z"
    for kept in ("boards", "scope", "kind", "status", "deferred_until", "summary", "detail", "refs", "reviewed_at"):
        assert issue[kept] == _entry(11)[kept], kept


def test_a_closed_issue_is_dropped(tmp_path):
    path = _registry(tmp_path, [_entry(11), _entry(99)])
    assert sync.main(["--write", "--registry", str(path)], fetch=_fetch({PUB: [_api(11, "kept")]}),
                     head_sha="abc1234") == 0
    assert [i["number"] for i in _read(path)["issues"]] == [11]


def test_a_new_issue_is_added_from_a_status_block(tmp_path):
    body = (
        "Lead-in paragraph that is not part of the status block.\n\n"
        "## STATUS 2026-10-22: logged as a known problem, not built before Sunday\n\n"
        "**Short description.** The derive leaves out large well-known issuers because an empty answer is stored "
        "without the reason it was empty, so the totals are understated.\n\n"
        "**Concrete example.** Exxon Mobil, CUSIP 30231G102: $354.43B held, empty ticker.\n\n"
        "**Why it matters.** 99.7% of the missing value came back with no ticker.\n\n"
        "**Solution direction.** Record why an answer was empty, then retry those answers.\n"
    )
    path = _registry(tmp_path, [])
    api = {PUB: [_api(11, "13F: large issuers stay unmatched", body=body, labels=("bug",))]}
    assert sync.main(["--write", "--registry", str(path)], fetch=_fetch(api), head_sha="abc1234") == 0
    issue = _read(path)["issues"][0]
    assert issue["scope"] == "board" and issue["boards"] == [] and issue["status"] == "open"
    assert issue["kind"] == "bug"
    assert issue["detail"]["whats_wrong"].startswith("The derive leaves out large well-known issuers")
    assert issue["detail"]["example"].startswith("Exxon Mobil")
    assert issue["detail"]["evidence"].startswith("99.7% of the missing value")
    assert issue["detail"]["direction"].startswith("Record why an answer was empty")
    assert issue["detail"]["tried_or_decided"] == "logged as a known problem, not built before Sunday"
    assert 60 <= len(issue["summary"]) <= 200 and "\n" not in issue["summary"]
    assert issue["summary"].startswith("The derive leaves out large well-known issuers")


def test_a_new_issue_is_added_from_plain_sections(tmp_path):
    body = (
        "## Observed\n\nThe board reads the revenue concept that most filers do not tag.\r\n\r\n\r\n"
        "## Mechanism\n\n339 filers carry the value under a different concept.\n\n"
        "## Proposed approach\n\nFall back to the second concept when the first is empty.\n\n"
        "## How to verify\n\nRe-run the derive and compare the totals for AKAM, BALL and ARMK.\n"
    )
    path = _registry(tmp_path, [])
    api = {PUB: [_api(435, "EDGAR revenue concept gap", body=body, labels=("bug",), repo=PUB)]}
    assert sync.main(["--write", "--registry", str(path)], fetch=_fetch(api), head_sha="abc1234") == 0
    issue = _read(path)["issues"][0]
    assert issue["detail"]["whats_wrong"].startswith("The board reads the revenue concept")
    assert "339 filers" in issue["detail"]["evidence"]
    assert issue["detail"]["direction"].startswith("Fall back to the second concept")
    assert issue["detail"]["next_step"].startswith("Re-run the derive")
    assert "\\r" not in json.dumps(issue)


def test_long_text_is_clipped_at_a_word_boundary(tmp_path):
    body = "## What is wrong\n\n" + ("word " * 400)
    path = _registry(tmp_path, [])
    api = {PUB: [_api(11, "a long one", body=body)]}
    assert sync.main(["--write", "--registry", str(path)], fetch=_fetch(api), head_sha="abc1234") == 0
    text = _read(path)["issues"][0]["detail"]["whats_wrong"]
    assert text.endswith(" […]")
    assert len(text) <= 900


def test_check_reports_drift_without_writing(tmp_path):
    path = _registry(tmp_path, [_entry(11)])
    before = path.read_text(encoding="utf-8")
    api = {PUB: [_api(11, "changed title")]}
    assert sync.main(["--check", "--registry", str(path)], fetch=_fetch(api), head_sha="abc1234") == 1
    assert path.read_text(encoding="utf-8") == before


def test_check_exits_zero_when_nothing_differs(tmp_path):
    path = _registry(tmp_path, [])
    api = {PUB: [_api(11, "one issue", body="## What is wrong\n\nshort")]}
    assert sync.main(["--write", "--registry", str(path)], fetch=_fetch(api), head_sha="abc1234") == 0
    assert sync.main(["--check", "--registry", str(path)], fetch=_fetch(api), head_sha="abc1234") == 0


def test_two_writes_with_no_change_produce_an_empty_diff(tmp_path):
    path = _registry(tmp_path, [])
    api = {PUB: [_api(11, "one issue", body="## What is wrong\n\nshort")]}
    assert sync.main(["--write", "--registry", str(path)], fetch=_fetch(api), head_sha="abc1234") == 0
    first = path.read_bytes()
    assert sync.main(["--write", "--registry", str(path)], fetch=_fetch(api), head_sha="abc1234") == 0
    assert path.read_bytes() == first


def test_report_thin_names_every_thin_entry(tmp_path, capsys):
    thin = _entry(11, summary="too short", detail={"whats_wrong": "", "evidence": "", "next_step": ""})
    ok = _entry(12)
    path = _registry(tmp_path, [thin, ok])
    assert sync.main(["--report-thin", "--registry", str(path)]) == 1
    out = capsys.readouterr().out
    assert f"{PUB}#11" in out
    assert f"{PUB}#12" not in out


def test_report_thin_is_zero_when_every_entry_passes(tmp_path):
    path = _registry(tmp_path, [_entry(11)])
    assert sync.main(["--report-thin", "--registry", str(path)]) == 0


def test_a_missing_registry_is_refused(tmp_path, capsys):
    assert sync.main(["--report-thin", "--registry", str(tmp_path / "absent.json")]) == 2
    assert "registry" in capsys.readouterr().err.lower()


def test_the_written_file_is_sorted_and_indented(tmp_path):
    path = _registry(tmp_path, [_entry(9, repo=PRIV)])   # a repo this run does not read keeps its entry
    api = {PUB: [_api(11, "public eleven")]}
    assert sync.main(["--write", "--registry", str(path)], fetch=_fetch(api), head_sha="abc1234") == 0
    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n") and '\n  "issues"' in text
    assert [i["repo"] for i in _read(path)["issues"]] == [PRIV, PUB]    # (repo, number) ascending


def test_the_sync_reads_the_public_repo_only(tmp_path):
    """The private repo's issues were migrated to the public one and closed (2026-10-22).

    A run that still read the private repo would fetch its migrated, half-closed set and re-add entries the panel
    must not show; the registry holds public issues only.
    """
    asked = []

    def fetch(repo, token=""):
        asked.append(repo)
        return []

    path = _registry(tmp_path, [_entry(11)])
    sync.main(["--write", "--registry", str(path)], fetch=fetch, head_sha="abc1234")
    assert asked == [PUB], f"the sync asked GitHub for {asked}, not for the public repo alone"
    assert not any(PRIV == repo for repo in asked)


def test_check_names_an_open_issue_missing_from_the_registry_and_a_closed_one_still_in_it(tmp_path, capsys):
    """The comparison both ways: one open issue the registry lacks, one closed issue it still carries.

    ``--check`` is what an operator runs before a merge round; a one-directional comparison would let a stale entry
    (an issue closed since the last write) stay on the panel for good.
    """
    path = _registry(tmp_path, [_entry(11), _entry(22)])
    api = {PUB: [_api(11, "curated title", updated_at="2026-10-22T10:03:48Z"),
                 _api(33, "opened since the last sync")]}
    assert sync.main(["--check", "--registry", str(path)], fetch=_fetch(api), head_sha="abc1234") == 1
    out = capsys.readouterr()
    assert f"added: {PUB}#33" in out.out, "the open issue the registry lacks is reported"
    assert f"removed: {PUB}#22" in out.out, "the closed issue still in the registry is reported"
    assert f"{PUB}#11" not in out.out, "an unchanged entry is not reported"
    assert "1 added, 1 removed" in out.err


def test_a_title_is_unescaped_from_html_entities(tmp_path):
    """GitHub returns a title as the author typed it, but a curated one can arrive HTML-escaped (an API/HTML round trip)."""
    title = 'D28: VVIX and OVX fail with not_a_csv — Cboe serves them as a two-column DATE,&lt;SYMBOL&gt; file'
    path = _registry(tmp_path, [_entry(11)])
    api = {PUB: [_api(11, title)]}
    assert sync.main(["--write", "--registry", str(path)], fetch=_fetch(api), head_sha="abc1234") == 0
    assert _read(path)["issues"][0]["title"] == title.replace("&lt;", "<").replace("&gt;", ">")


def test_a_mode_is_required(tmp_path):
    assert sync.main(["--registry", str(tmp_path / "x.json")]) == 2


def test_a_fetch_that_would_empty_the_registry_is_refused(tmp_path, capsys):
    """Measured 2026-10-22: the REST list endpoint answered HTTP 200 with ``[]`` while 53 issues were open.

    ``merge`` reads an empty list as every issue being closed, so ``--write`` deleted the whole curated
    registry and the suite could not see it, because nothing pins the entry count. A fetcher is never
    trusted that far: the run is refused and the file left alone.
    """
    path = _registry(tmp_path, [_entry(11), _entry(12)])
    before = path.read_text(encoding="utf-8")

    assert sync.main(["--write", "--registry", str(path)], fetch=lambda repo, token="": []) == 2
    assert "refusing" in capsys.readouterr().err
    assert path.read_text(encoding="utf-8") == before, "the registry was written despite the refusal"


def test_the_real_fetcher_refuses_an_empty_api_answer(monkeypatch):
    """The same guard at the source, so ``--check`` cannot report a 53-entry removal either."""
    class _Response:
        status = 200

        def read(self):
            return b"[]"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Response())
    with pytest.raises(sync.SyncError, match="no open issues"):
        sync.github_open_issues(PUB)


def test_a_title_keeps_an_incomplete_entity_reference():
    """``html.unescape`` resolves a reference written without its semicolon, so a literal "&notch" became
    "\u00acch" — and ``--check`` unescaped the API value to the same string, so the damage never healed.
    """
    assert sync._title({"title": "RSI&notch filter"}) == "RSI&notch filter"
    assert sync._title({"title": "&times2 leverage"}) == "&times2 leverage"
    # A complete reference is still resolved, which is the whole point of the step.
    assert sync._title({"title": "Fix &amp; ship &lt;b&gt;"}) == "Fix & ship <b>"
    assert sync._title({"title": "&#38; and &#x26;"}) == "& and &"


def test_a_clip_never_ends_inside_an_inline_code_span():
    """The panel renders inline code, so half a `span` shows as a dangling backtick on the board.

    The word-boundary cut already drops a partial token, so the span that dangles is one that runs over
    several words: the cut then falls after a space inside it and the opening backtick survives alone. Three
    registry entries looked like that on 2026-10-22, e.g. ``land in `result_runs.warnings […]``.
    """
    limit, marker = 900, len(" […]")
    span = "`result_runs.warnings and other fields`"
    # Put the cut inside the span, just past its second word, so the rsplit keeps the opening backtick.
    keep = limit - marker
    filler = "w " * ((keep - span.index(" other") - 2) // 2)
    text = filler + span + " and the rest of the sentence"
    assert text[:keep].rsplit(" ", 1)[0].count("`") % 2, "the test no longer cuts inside the span"

    clipped = sync._clip(text, limit=limit)
    assert clipped.count("`") % 2 == 0, f"dangling backtick: {clipped[-60:]!r}"
    assert clipped.endswith(" […]")
    # A whole span and a text with no backtick are both left as they are.
    assert sync._clip("a `path/to.py` b", limit=limit) == "a `path/to.py` b"
    assert sync._clip("word " * 300, limit=limit).endswith(" […]")
