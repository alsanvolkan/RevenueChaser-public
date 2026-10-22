"""Open GitHub issues that affect a dashboard board (#383, ``docs/open_issues.json``).

The dashboard's "Open issues" group is parent chrome (``templates/dashboard.html``), so it must not read the store or
call the network while a page renders. It reads one checked-in registry instead: ``docs/open_issues.json``, written
only by ``scripts/open_issues_sync.py --write`` and curated by hand.

Three scopes keep a board's own problems at the top of its group:

- ``board``  — listed under "This board", for the boards named in ``boards``;
- ``shared`` — listed under "Affects every board", on every data board;
- ``repo``   — listed under "Repository-wide", on the Settings / Data health board only.

``load`` validates the shape and raises ``RegistryError`` with a one-line reason; a caller renders that as a message.
A registry that cannot be read must never turn a dashboard page into a 500 — the same convention as
``reporting_common.recorded_sessions_html``.

``for_board`` answers one board. The shell needs all of them, so ``panel_payload`` ships the entries once and lets each
board name its own by key: sending ``for_board``'s answer per board repeats the shared entries 16 times.

Links are restricted to ``URL_PREFIX`` at two points: the registry test, and ``for_board``, which drops (and counts) an
entry or a ref whose URL points anywhere else. The panel renders every string through ``html_safe``/``rcEsc``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

REGISTRY_PATH = Path(__file__).resolve().parent.parent / "docs" / "open_issues.json"
URL_PREFIX = "https://github.com/alsanvolkan/"

# Not data boards (``core/tests/test_help_every_tab.py``): no report, so no issue panel.
NOT_A_DATA_BOARD = frozenset({"logs", "dealer_hedge"})
# Repository-wide entries are listed on the Ops board only.
REPO_SCOPE_BOARD = "module_settings"

SCOPES = ("board", "shared", "repo")
KINDS = ("bug", "known-problem", "investigate", "seam", "enhancement", "docs", "epic", "idea")
STATUSES = ("needs-decision", "in-flight", "approved", "open", "needs-review", "deferred")

# The panel's order inside each section: what needs a decision first, a parked entry last.
STATUS_ORDER = {s: i for i, s in enumerate(("needs-decision", "in-flight", "approved", "open", "needs-review", "deferred"))}
KIND_ORDER = {k: i for i, k in enumerate(KINDS)}

DETAIL_FIELDS = ("whats_wrong", "evidence", "example", "tried_or_decided", "direction", "next_step")
# The two bars a curated entry must clear (build brief §5). ``thin_reasons`` is the one implementation: the
# registry test and ``open_issues_sync.py --report-thin`` both call it.
SUMMARY_MIN, SUMMARY_MAX, FIELD_MAX = 60, 200, 900
DENSE_KINDS = frozenset({"bug", "known-problem", "investigate"})


class RegistryError(Exception):
    """The registry is missing or does not have the shape this module promises."""


@dataclass(frozen=True)
class Entry:
    repo: str
    number: int
    url: str
    title: str
    state: str
    labels: list
    boards: list
    scope: str
    kind: str
    status: str
    deferred_until: str
    summary: str
    detail: dict
    refs: list
    issue_updated_at: str
    reviewed_at: str
    stored_status: str = ""

    @property
    def key(self) -> tuple[str, int]:
        return self.repo, self.number

    def as_dict(self) -> dict:
        """The panel's JSON shape (plain dicts/lists/strings only)."""
        return {
            "repo": self.repo, "number": self.number, "url": self.url, "title": self.title,
            "state": self.state, "labels": list(self.labels), "boards": list(self.boards),
            "scope": self.scope, "kind": self.kind, "status": self.status,
            "stored_status": self.stored_status or self.status,
            "deferred_until": self.deferred_until, "summary": self.summary,
            "detail": dict(self.detail), "refs": [dict(r) for r in self.refs],
            "issue_updated_at": self.issue_updated_at, "reviewed_at": self.reviewed_at,
        }


@dataclass(frozen=True)
class Registry:
    generated_at: str
    generated_from: str
    issues: tuple


def _one_line(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()
    return text[0] if text else type(exc).__name__


def _text(raw: dict, name: str, where: str) -> str:
    value = raw.get(name, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise RegistryError(f"{where}: {name} is not a string")
    return value


def _entry(raw, index: int) -> Entry:
    where = f"issue {index}"
    if not isinstance(raw, dict):
        raise RegistryError(f"{where}: not an object")
    repo = _text(raw, "repo", where)
    if not repo:
        raise RegistryError(f"{where}: no repo")
    where = f"{repo} issue {raw.get('number')!r}"
    number = raw.get("number")
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        raise RegistryError(f"{where}: number is not a positive integer")
    url = _text(raw, "url", where)
    if not url:
        raise RegistryError(f"{where}: no url")
    scope = _text(raw, "scope", where)
    if scope not in SCOPES:
        raise RegistryError(f"{where}: unknown scope {scope!r}")
    kind = _text(raw, "kind", where)
    if kind not in KINDS:
        raise RegistryError(f"{where}: unknown kind {kind!r}")
    status = _text(raw, "status", where)
    if status not in STATUSES:
        raise RegistryError(f"{where}: unknown status {status!r}")
    boards = raw.get("boards") or []
    if not isinstance(boards, list) or not all(isinstance(b, str) for b in boards):
        raise RegistryError(f"{where}: boards is not a list of strings")
    labels = raw.get("labels") or []
    if not isinstance(labels, list) or not all(isinstance(l, str) for l in labels):
        raise RegistryError(f"{where}: labels is not a list of strings")
    detail = raw.get("detail") or {}
    if not isinstance(detail, dict):
        raise RegistryError(f"{where}: detail is not an object")
    refs = raw.get("refs") or []
    if not isinstance(refs, list) or not all(isinstance(r, dict) and "url" in r for r in refs):
        raise RegistryError(f"{where}: refs is not a list of {{label, url}}")
    return Entry(
        repo=repo, number=number, url=url, title=_text(raw, "title", where),
        state=_text(raw, "state", where) or "open", labels=list(labels), boards=list(boards),
        scope=scope, kind=kind, status=status, deferred_until=_text(raw, "deferred_until", where),
        summary=_text(raw, "summary", where),
        detail={k: str(v) for k, v in detail.items() if isinstance(v, str)},
        refs=[{"label": str(r.get("label") or ""), "url": str(r["url"])} for r in refs],
        issue_updated_at=_text(raw, "issue_updated_at", where),
        reviewed_at=_text(raw, "reviewed_at", where),
    )


def load(path: Path | str | None = None) -> Registry:
    """Read and validate the registry. Raises ``RegistryError`` with a one-line reason."""
    p = Path(path) if path is not None else REGISTRY_PATH
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RegistryError(f"no issue registry at {p.name}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistryError(f"issue registry unreadable: {_one_line(exc)}") from exc
    if not isinstance(raw, dict):
        raise RegistryError("issue registry is not an object")
    generated_at = raw.get("generated_at") or ""
    if not isinstance(generated_at, str) or not generated_at:
        raise RegistryError("issue registry has no generated_at")
    try:
        _parse_date(generated_at)
    except ValueError as exc:
        raise RegistryError(f"generated_at is not a timestamp: {generated_at!r}") from exc
    generated_from = raw.get("generated_from") or ""
    if not isinstance(generated_from, str) or not generated_from:
        raise RegistryError("issue registry has no generated_from")
    issues = raw.get("issues")
    if not isinstance(issues, list):
        raise RegistryError("issue registry has no issues list")
    return Registry(generated_at=generated_at, generated_from=generated_from,
                    issues=tuple(_entry(item, i) for i, item in enumerate(issues)))


def _parse_date(value: str) -> date:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()


def needs_review(entry: Entry) -> bool:
    """The issue changed after the entry was curated, so the curated text may be out of date."""
    try:
        return _parse_date(entry.issue_updated_at) > _parse_date(entry.reviewed_at)
    except (TypeError, ValueError):
        return False


def effective_status(entry: Entry) -> str:
    return "needs-review" if needs_review(entry) else entry.status


def _panel_entry(entry: Entry, refs: list[dict]) -> dict:
    out = entry.as_dict()
    status = effective_status(entry)
    out["status"] = status
    out["stored_status"] = entry.status
    out["refs"] = refs
    return out


def for_board(board_key: str, registry: Registry | None = None, *, today: date | None = None) -> dict:
    """The panel's payload for one board; ``{}`` (falsy) on a tab that is not a data board.

    ``today`` exists so a test never depends on the wall clock; the panel itself passes nothing.
    """
    if board_key in NOT_A_DATA_BOARD:
        return {}
    reg = registry if registry is not None else load()
    wanted: dict[str, list[tuple[Entry, dict]]] = {"board": [], "shared": [], "repo": []}
    dropped = 0
    for entry in reg.issues:
        if not entry.url.startswith(URL_PREFIX):
            dropped += 1
            continue
        refs = [r for r in entry.refs if r["url"].startswith(URL_PREFIX)]
        dropped += len(entry.refs) - len(refs)
        if entry.scope == "board" and board_key in entry.boards:
            wanted["board"].append((entry, _panel_entry(entry, refs)))
        elif entry.scope == "shared":
            wanted["shared"].append((entry, _panel_entry(entry, refs)))
        elif entry.scope == "repo" and board_key == REPO_SCOPE_BOARD:
            wanted["repo"].append((entry, _panel_entry(entry, refs)))
    try:
        generated = _parse_date(reg.generated_at)
        stale_days = max(0, ((today or date.today()) - generated).days)
    except (TypeError, ValueError):
        stale_days = 0
    return {
        "board": _sort_pairs(wanted["board"]),
        "shared": _sort_pairs(wanted["shared"]),
        "repo": _sort_pairs(wanted["repo"]),
        "generated_at": reg.generated_at,
        "stale_days": stale_days,
        "dropped_links": dropped,
    }


def entry_key(entry: Entry | dict) -> str:
    """The key the shell's payload uses for one entry: ``"<repo>#<number>"``."""
    repo, number = (entry.repo, entry.number) if isinstance(entry, Entry) else (entry["repo"], entry["number"])
    return f"{repo}#{number}"


def panel_payload(board_keys, registry: Registry | None = None, *, today: date | None = None) -> dict:
    """The shell's one JSON block: every entry once, and each board naming the entries it shows (#383, #21).

    ``for_board`` answers one board, so asking it for all 16 repeats the shared and repo entries 16 times: ~590 KB
    of embedded JSON for a 118 KB registry. Here an entry is shipped once under ``entries`` (keyed
    ``"<repo>#<number>"``) and a board keeps only its section keys plus the small header fields.

    ``{"entries": {...}, "boards": {board_key: {"board": [key, ...], "shared": [...], "repo": [...],
    "generated_at": ..., "stale_days": ..., "dropped_links": ...}}}``. A board that is not a data board is absent.
    """
    reg = registry if registry is not None else load()
    entries: dict[str, dict] = {}
    boards: dict[str, dict] = {}
    for key in board_keys:
        found = for_board(key, reg, today=today)
        if not found:
            continue
        for section in SCOPES:
            keys = []
            for item in found[section]:
                name = entry_key(item)
                entries.setdefault(name, item)
                keys.append(name)
            found[section] = keys
        boards[key] = found
    return {"entries": entries, "boards": boards}


def _sort_pairs(pairs: list[tuple[Entry, dict]]) -> list[dict]:
    """The one order inside a section: status, then kind, then issue number."""
    ordered = sorted(pairs, key=lambda p: (STATUS_ORDER.get(effective_status(p[0]), len(STATUS_ORDER)),
                                           KIND_ORDER.get(p[0].kind, len(KIND_ORDER)), p[0].number))
    return [d for _, d in ordered]


def _as_entry(d: dict) -> Entry:
    """Wrap a panel dict back into an ``Entry`` so ``thin_reasons`` accepts either shape."""
    return Entry(repo=d["repo"], number=d["number"], url=d["url"], title=d["title"], state=d["state"],
                 labels=d["labels"], boards=d["boards"], scope=d["scope"], kind=d["kind"],
                 status=d.get("stored_status") or d["status"], deferred_until=d["deferred_until"],
                 summary=d["summary"], detail=d["detail"], refs=d["refs"],
                 issue_updated_at=d["issue_updated_at"], reviewed_at=d["reviewed_at"],
                 stored_status=d.get("stored_status", ""))


def thin_reasons(entry: Entry | dict) -> list[str]:
    """Why an entry fails the density bar; empty when it passes (build brief §5)."""
    e = entry if isinstance(entry, Entry) else _as_entry(entry)
    reasons = []
    summary = e.summary or ""
    if not SUMMARY_MIN <= len(summary) <= SUMMARY_MAX:
        reasons.append(f"summary is {len(summary)} chars, needs {SUMMARY_MIN}-{SUMMARY_MAX}")
    if "\n" in summary or "\r" in summary:
        reasons.append("summary is not one line")
    for name in DETAIL_FIELDS:
        text = (e.detail or {}).get(name) or ""
        if len(text) > FIELD_MAX:
            reasons.append(f"{name} is {len(text)} chars, max {FIELD_MAX}")
    if e.kind in DENSE_KINDS:
        for name in ("whats_wrong", "evidence", "next_step"):
            if not ((e.detail or {}).get(name) or "").strip():
                reasons.append(f"{e.kind} needs a non-empty {name}")
    if e.kind == "known-problem" and not ((e.detail or {}).get("direction") or "").strip():
        reasons.append("known-problem needs a non-empty direction")
    if e.kind == "epic" and not ((e.detail or {}).get("whats_wrong") or "").strip():
        reasons.append("epic needs a whats_wrong that says what is still open")
    return reasons
