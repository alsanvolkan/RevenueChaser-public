"""Refresh ``docs/open_issues.json`` from the GitHub API (#383) — the registry's only writer.

The dashboard's "Open issues" group reads a checked-in file, never the network
(``core/open_issues.py``). This script keeps that file honest:

- ``--write`` reads the **open** issues of both repos (paginated, pull requests filtered out), merges them with the
  registry and writes it back. The **API owns** ``title``, ``state``, ``labels`` and ``issue_updated_at``; a human
  owns ``boards``, ``scope``, ``kind``, ``status``, ``deferred_until``, ``summary``, ``detail``, ``refs`` and
  ``reviewed_at``, and those are never overwritten. A new issue is added (``scope="board"``, no boards, kind from its
  labels) with ``summary``/``detail`` pre-filled from its body. An issue that is no longer open is dropped — but only
  for a repo this run actually read.
- ``--check`` writes nothing and exits 1 when the registry differs from the API.
- ``--report-thin`` lists the entries below the density bar (``core.open_issues.thin_reasons``) and exits 1.

Output is deterministic (sorted by ``(repo, number)``, 2-space indent, trailing newline) and a run that changes
nothing does not touch the file, so two runs produce an empty diff. The header's ``generated_at`` / ``generated_from``
move only when the content does.

Run it after each merge round:  ``python scripts/open_issues_sync.py --write``
The private repo needs ``RC_GITHUB_TOKEN`` (a token with ``repo`` read); the public one reads anonymously.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from RevenueChaser.core import open_issues

OWNER = "alsanvolkan"
REPOS = ("RevenueChaser-public", "RevenueChaser")
API_ROOT = "https://api.github.com"
PER_PAGE = 100

# The registry's key order, so a rewrite diffs only where a value changed.
ENTRY_KEYS = ("repo", "number", "url", "title", "state", "labels", "boards", "scope", "kind", "status",
              "deferred_until", "summary", "detail", "refs", "issue_updated_at", "reviewed_at")
DETAIL_KEYS = ("whats_wrong", "evidence", "example", "tried_or_decided", "direction", "next_step")
CURATED_KEYS = ("boards", "scope", "kind", "status", "deferred_until", "summary", "detail", "refs", "reviewed_at")

SUMMARY_LIMIT, FIELD_LIMIT = 200, 900
KIND_LABELS = {"bug": "bug", "enhancement": "enhancement", "documentation": "docs", "epic": "epic", "idea": "idea"}

# A "## STATUS …" block is the shape this project's issues use for the paragraph a reader needs.
_STATUS_HEADING = re.compile(r"^##\s+STATUS\b[^\n]*$", re.M)
_LEADER = re.compile(r"\*\*([^*\n]{2,60})\*\*")
_LEADER_FIELDS = (("short description", "whats_wrong"), ("concrete example", "example"),
                  ("why it matters", "evidence"), ("solution direction", "direction"))
_SECTION = re.compile(r"^(#{2,3})\s+(.+?)\s*$", re.M)
_SECTION_RULES = ((r"observed|the seam|what is wrong", "whats_wrong"),
                  (r"mechanism|evidence|why it is worth fixing|why it matters", "evidence"),
                  (r"example", "example"),
                  (r"proposed approach|shape of the fix|direction", "direction"),
                  (r"how to verify|next", "next_step"))


class SyncError(Exception):
    """The API could not be read (a message the operator can act on)."""


# ── the API ──────────────────────────────────────────────────────────────────

def github_open_issues(repo: str, token: str = "") -> list[dict]:
    """Every open issue of ``OWNER/repo`` (pull requests filtered out), paginated."""
    import urllib.error
    import urllib.request

    headers = {"Accept": "application/vnd.github+json", "User-Agent": "RevenueChaser-open-issues-sync"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    found: list[dict] = []
    page = 1
    while True:
        url = (f"{API_ROOT}/repos/{OWNER}/{repo}/issues"
               f"?state=open&per_page={PER_PAGE}&page={page}")
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                batch = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            hint = " (set RC_GITHUB_TOKEN for the private repo)" if exc.code in (401, 404) else ""
            raise SyncError(f"{repo}: HTTP {exc.code}{hint}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SyncError(f"{repo}: {exc}") from exc
        if not isinstance(batch, list):
            raise SyncError(f"{repo}: the API did not return a list")
        found += [issue for issue in batch if "pull_request" not in issue]
        if len(batch) < PER_PAGE:
            return found
        page += 1


# ── the body → summary/detail pre-fill ───────────────────────────────────────

def _clean(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _clip(text: str, limit: int = FIELD_LIMIT) -> str:
    """Cut at a word boundary, marker included, because the full text stays on GitHub behind the link."""
    if len(text) <= limit:
        return text
    marker = " […]"
    cut = text[:limit - len(marker)]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip() + marker


def _status_section(body: str) -> tuple[str, str] | None:
    heads = list(_STATUS_HEADING.finditer(body))
    if not heads:
        return None
    head = heads[-1]
    rest = body[head.end():]
    nxt = re.search(r"^#{1,2}\s+", rest, re.M)
    return head.group(0), rest[:nxt.start()] if nxt else rest


def _status_fields(head: str, section: str) -> dict:
    detail = {}
    heading = head[2:].strip()
    if ":" in heading:
        detail["tried_or_decided"] = heading.split(":", 1)[1].strip()
    leaders = list(_LEADER.finditer(section))
    for i, leader in enumerate(leaders):
        end = leaders[i + 1].start() if i + 1 < len(leaders) else len(section)
        text = _clean(section[leader.end():end])
        label = leader.group(1).strip().rstrip(".").lower()
        for prefix, name in _LEADER_FIELDS:
            if label.startswith(prefix) and text:
                detail.setdefault(name, text)
                break
    return detail


def _sections(body: str) -> dict:
    found = list(_SECTION.finditer(body))
    detail: dict[str, str] = {}
    for i, head in enumerate(found):
        end = found[i + 1].start() if i + 1 < len(found) else len(body)
        heading = head.group(2).lower()
        text = _clean(body[head.end():end])
        if not text:
            continue
        for pattern, name in _SECTION_RULES:
            if name not in detail and re.search(pattern, heading):
                detail[name] = text
                break
    return detail


def _summary_from(whats_wrong: str) -> str:
    line = " ".join((whats_wrong or "").split())
    sentence = re.split(r"(?<=[.!?])\s+", line, maxsplit=1)[0] if line else ""
    if len(sentence) > SUMMARY_LIMIT:
        cut = sentence[:SUMMARY_LIMIT]
        sentence = (cut.rsplit(" ", 1)[0] if " " in cut else cut).rstrip()
    return sentence if len(sentence) >= open_issues.SUMMARY_MIN else ""


def prefill(body: str) -> dict:
    """``summary`` + ``detail`` from an issue body: the last STATUS block, else the leading sections."""
    body = _clean(body)
    detail = {}
    status = _status_section(body)
    if status:
        detail = _status_fields(*status)
    for name, text in _sections(body).items():
        detail.setdefault(name, text)
    detail = {name: _clip(str(detail.get(name) or "")) for name in DETAIL_KEYS}
    return {"summary": _summary_from(detail.get("whats_wrong", "")), "detail": detail}


def guess_kind(labels) -> str:
    names = [str(label).lower() for label in labels]
    for label, kind in KIND_LABELS.items():
        if label in names:
            return kind
    return "bug"


# ── merge / write ────────────────────────────────────────────────────────────

def _canonical(entry: dict) -> dict:
    """One entry in the registry's key order, with every field present."""
    detail = entry.get("detail") or {}
    return {
        "repo": str(entry.get("repo") or ""),
        "number": int(entry.get("number") or 0),
        "url": str(entry.get("url") or ""),
        "title": str(entry.get("title") or ""),
        "state": str(entry.get("state") or "open"),
        "labels": [str(label) for label in (entry.get("labels") or [])],
        "boards": [str(board) for board in (entry.get("boards") or [])],
        "scope": str(entry.get("scope") or "board"),
        "kind": str(entry.get("kind") or "bug"),
        "status": str(entry.get("status") or "open"),
        "deferred_until": str(entry.get("deferred_until") or ""),
        "summary": str(entry.get("summary") or ""),
        "detail": {name: str(detail.get(name) or "") for name in DETAIL_KEYS},
        "refs": [{"label": str(ref.get("label") or ""), "url": str(ref.get("url") or "")}
                 for ref in (entry.get("refs") or [])],
        "issue_updated_at": str(entry.get("issue_updated_at") or ""),
        "reviewed_at": str(entry.get("reviewed_at") or ""),
    }


def _new_entry(repo: str, issue: dict, today: str) -> dict:
    labels = [str(label.get("name") or "") for label in (issue.get("labels") or [])
              if isinstance(label, dict)]
    number = int(issue["number"])
    pre = prefill(issue.get("body") or "")
    return _canonical({
        "repo": repo, "number": number,
        "url": f"{open_issues.URL_PREFIX}{repo}/issues/{number}",
        "title": issue.get("title") or "", "state": "open", "labels": labels,
        "boards": [], "scope": "board", "kind": guess_kind(labels), "status": "open",
        "summary": pre["summary"], "detail": pre["detail"], "refs": [],
        "issue_updated_at": issue.get("updated_at") or "", "reviewed_at": today,
    })


def _api_fields(issue: dict) -> dict:
    return {
        "title": issue.get("title") or "",
        "state": "open",
        "labels": [str(label.get("name") or "") for label in (issue.get("labels") or [])
                   if isinstance(label, dict)],
        "issue_updated_at": issue.get("updated_at") or "",
    }


def merge(existing: list[dict], fetched: dict[str, list[dict]], today: str) -> tuple[list[dict], list, list, list]:
    """``(entries, added, removed, changed)``; the API's fields win, the curated ones are kept verbatim."""
    current = {(e["repo"], e["number"]): _canonical(e) for e in existing}
    merged: dict[tuple[str, int], dict] = {}
    added, changed = [], []
    for repo, issues in fetched.items():
        for issue in issues:
            key = (repo, int(issue["number"]))
            fields = _api_fields(issue)
            old = current.get(key)
            if old is None:
                merged[key] = _new_entry(repo, issue, today)
                added.append(key)
                continue
            entry = dict(old)
            if any(old[name] != fields[name] for name in fields):
                changed.append(key)
            entry.update(fields)
            entry["url"] = f"{open_issues.URL_PREFIX}{repo}/issues/{key[1]}"
            merged[key] = _canonical(entry)
    removed = [key for key in current if key[0] in fetched and key not in merged]
    # A repo this run did not read keeps its entries: a failed fetch must not delete anything.
    for key, entry in current.items():
        if key[0] not in fetched:
            merged[key] = entry
    entries = [merged[key] for key in sorted(merged)]
    return entries, sorted(added), sorted(removed), sorted(changed)


def dumps(registry: dict) -> str:
    """Deterministic bytes: sorted entries, 2-space indent, no ASCII escaping, trailing newline."""
    body = {
        "generated_at": registry["generated_at"],
        "generated_from": registry["generated_from"],
        "issues": [_canonical(entry) for entry in sorted(registry["issues"],
                                                         key=lambda e: (e["repo"], e["number"]))],
    }
    return json.dumps(body, indent=2, ensure_ascii=False) + "\n"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git_short_sha() -> str:
    root = Path(__file__).resolve().parents[1]
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root,
                             capture_output=True, text=True, timeout=10, check=True)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip()


def _describe(key: tuple[str, int]) -> str:
    return f"{key[0]}#{key[1]}"


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None, *, fetch=None, head_sha: str | None = None) -> int:
    parser = argparse.ArgumentParser(prog="open_issues_sync", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="merge the API into the registry and write it")
    mode.add_argument("--check", action="store_true", help="exit 1 when the registry differs; write nothing")
    mode.add_argument("--report-thin", action="store_true", help="list the entries below the density bar")
    parser.add_argument("--token-env", default="RC_GITHUB_TOKEN", help="variable holding the GitHub token")
    parser.add_argument("--registry", default=str(open_issues.REGISTRY_PATH), help="the registry file")
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2

    path = Path(args.registry)
    try:
        registry = open_issues.load(path)
    except open_issues.RegistryError as exc:
        print(f"open issues registry: {exc}", file=sys.stderr)
        return 2

    if args.report_thin:
        thin = 0
        for entry in registry.issues:
            reasons = open_issues.thin_reasons(entry)
            if reasons:
                thin += 1
                print(f"{_describe(entry.key)}: {'; '.join(reasons)}")
        print(f"{thin} of {len(registry.issues)} entries below the density bar", file=sys.stderr)
        return 1 if thin else 0

    fetched: dict[str, list[dict]] = {}
    token = os.environ.get(args.token_env, "")
    fetcher = fetch or github_open_issues
    try:
        for repo in REPOS:
            fetched[repo] = list(fetcher(repo, token))
    except SyncError as exc:
        print(f"open issues sync: {exc}", file=sys.stderr)
        return 2

    today = date.today().isoformat()
    entries, added, removed, changed = merge([e.as_dict() for e in registry.issues], fetched, today)
    for label, keys in (("added", added), ("removed", removed), ("changed", changed)):
        for key in keys:
            print(f"{label}: {_describe(key)}")

    if args.check:
        if added or removed or changed:
            print(f"{len(added)} added, {len(removed)} removed, {len(changed)} changed — "
                  f"run --write to update {path.name}", file=sys.stderr)
            return 1
        print("registry is current", file=sys.stderr)
        return 0

    if not (added or removed or changed):
        print("no change", file=sys.stderr)
        return 0
    payload = {
        "generated_at": _now(),
        "generated_from": head_sha or _git_short_sha() or registry.generated_from,
        "issues": entries,
    }
    path.write_text(dumps(payload), encoding="utf-8")
    print(f"wrote {len(entries)} issues to {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
