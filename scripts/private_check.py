"""Private-material check: no secret, personal home path, personal mailbox, account id or private folder in git.

More than one developer reads this repo, so anything committed is visible to all of them. Owner-only notes live in
``.private/`` (gitignored). This module is the one checker, stdlib only so a plain ``python`` can run it:

- ``python scripts/private_check.py --staged`` checks what the next commit would carry (the index, not the working
  tree). ``scripts/hooks/pre-commit`` runs it; enable it once per clone with ``git config core.hooksPath scripts/hooks``.
- ``python scripts/private_check.py --all`` checks every tracked file. ``scripts/tests/test_private_check.py`` runs the
  same check in the unit suite, so a slip is caught before a merge whoever pushed it.

A finding prints ``path:line: kind`` and never the matched value.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

_SECRETS: dict[str, re.Pattern[str]] = {
    "anthropic_key": re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
    "openai_style_key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}"),
    "xai_key": re.compile(r"\bxai-[A-Za-z0-9]{40,}"),
    "github_token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{60,})"),
    "aws_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "google_api_key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "slack_token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    "discord_token": re.compile(r"\b[MNO][A-Za-z\d_-]{23,27}\.[\w-]{6,7}\.[\w-]{27,40}\b"),
    "discord_webhook": re.compile(r"discord(?:app)?\.com/api/webhooks/\d+/[\w-]+"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA |PGP |ENCRYPTED )?PRIVATE KEY-----"),
}

# A key or token variable given a literal value on one line (``NAME=value``, ``NAME = "value"``, ``$env:NAME = ...``).
_KEY_ASSIGNMENT = re.compile(
    r"\b[A-Z][A-Z0-9_]*(?:API_KEY|_TOKEN|_SECRET|PASSWORD)\b[ \t]*[:=][ \t]*['\"]?([A-Za-z0-9_\-.]{16,})")
_NOT_A_VALUE = re.compile(r"(?i)sentinel|example|dummy|fake|placeholder|your|changeme|redacted|xxxx|test|^os\.")

# A home folder with a real user name in it. Placeholders (``<user>``, ``%USERNAME%``) and generic names pass.
_HOME_PATHS = (
    re.compile(r"\b[A-Za-z]:[\\/]+Users[\\/]+([^\\/\s\"'`|:*?<>%$]+)"),
    re.compile(r"(?<![\w.])/(?:Users|home)/([A-Za-z0-9._-]+)"),
)
_GENERIC_USERS = frozenset({"user", "users", "runner", "runneradmin", "x", "you", "me", "name", "username",
                            "public", "default", "shared", "all", "admin", "root", "someone", "example"})

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+)\.[A-Za-z.]{2,}\b")
_PERSONAL_MAIL_DOMAINS = frozenset({"gmail", "googlemail", "hotmail", "outlook", "live", "msn", "icloud", "me",
                                    "mac", "yahoo", "ymail", "proton", "protonmail", "pm", "gmx", "aol", "yandex",
                                    "zoho", "mail"})
_PUBLIC_ADDRESSES = frozenset({"loughranmcdonald@gmail.com"})   # the Loughran-McDonald dictionary's own contact

_ACCOUNT_ID = re.compile(r"\b(?:DU|U)\d{7,8}\b")                 # brokerage (IBKR-style) account ids

# Paths that never belong in the shared repo, whatever they hold.
_PRIVATE_DIRS = (".private/", ".store/", ".cache/", "discord_media/", "edgar_docs/", ".tools/")
_PRIVATE_SUFFIXES = (".duckdb", ".duckdb.wal", ".sqlite", ".sqlite3", ".db", ".pem", ".p12", ".pfx", ".pyc")
_PRIVATE_NAMES = frozenset({"auth.json", "credentials.json", ".credentials.json", ".netrc", "id_rsa", "id_ed25519"})
_ENV_TEMPLATES = frozenset({".env.example", ".env.sample", ".env.template"})

_MAX_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str


def format_finding(f: Finding) -> str:
    return f"{f.path}:{f.line}: {f.kind}"


def check_path(path: str) -> Finding | None:
    """A finding when ``path`` (repo-relative, ``/``-separated) may never be committed, else ``None``."""
    p = path.replace("\\", "/")
    name = p.rsplit("/", 1)[-1]
    lowered = p.lower()
    if (name == ".env" or (name.startswith(".env.") and name not in _ENV_TEMPLATES)
            or name in _PRIVATE_NAMES
            or any(lowered.startswith(d) or f"/{d}" in lowered for d in _PRIVATE_DIRS)
            or lowered.endswith(_PRIVATE_SUFFIXES)):
        return Finding(path, 0, "private_path")
    return None


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def check_text(path: str, text: str) -> list[Finding]:
    """Every finding in one file's text. Binary content (a NUL byte near the start) is skipped."""
    if "\x00" in text[:8000]:
        return []
    found: list[Finding] = []
    for kind, rx in _SECRETS.items():
        found += [Finding(path, _line_of(text, m.start()), kind) for m in rx.finditer(text)]
    for m in _KEY_ASSIGNMENT.finditer(text):
        if not _NOT_A_VALUE.search(m.group(1)):
            found.append(Finding(path, _line_of(text, m.start()), "key_assignment"))
    for rx in _HOME_PATHS:
        for m in rx.finditer(text):
            if m.group(1).lower() not in _GENERIC_USERS:
                found.append(Finding(path, _line_of(text, m.start()), "personal_path"))
    for m in _EMAIL.finditer(text):
        if m.group(1).lower() in _PERSONAL_MAIL_DOMAINS and m.group(0).lower() not in _PUBLIC_ADDRESSES:
            found.append(Finding(path, _line_of(text, m.start()), "personal_email"))
    found += [Finding(path, _line_of(text, m.start()), "account_id") for m in _ACCOUNT_ID.finditer(text)]
    return sorted(set(found), key=lambda f: (f.line, f.kind))


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True).stdout


def tracked_files(repo: Path) -> list[str]:
    return [p for p in _git(repo, "ls-files", "-z").decode("utf-8").split("\0") if p]


def staged_files(repo: Path) -> list[str]:
    out = _git(repo, "diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR")
    return [p for p in out.decode("utf-8").split("\0") if p]


def _scan(paths: list[str], read) -> list[Finding]:
    findings: list[Finding] = []
    for path in paths:
        bad = check_path(path)
        if bad:
            findings.append(bad)
            continue
        data = read(path)
        if data is None or len(data) > _MAX_BYTES:
            continue
        findings += check_text(path, data.decode("utf-8", "replace"))
    return findings


def scan_files(repo: Path, paths: list[str]) -> list[Finding]:
    """Check tracked files as they are in the working tree (a file deleted but not yet staged is skipped)."""
    def read(path: str) -> bytes | None:
        f = repo / path
        return f.read_bytes() if f.is_file() else None
    return _scan(paths, read)


def scan_staged(repo: Path) -> list[Finding]:
    """Check what the next commit would carry: each staged file's content in the index."""
    return _scan(staged_files(repo), lambda path: _git(repo, "show", f":{path}"))


def main(argv: list[str] | None = None, repo: Path | None = None) -> int:
    ap = argparse.ArgumentParser(prog="private_check", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--staged", action="store_true", help="check the staged content (the pre-commit hook)")
    mode.add_argument("--all", action="store_true", help="check every tracked file")
    args = ap.parse_args(argv)

    root = repo if repo is not None else Path(_git(Path.cwd(), "rev-parse", "--show-toplevel").decode().strip())
    findings = scan_staged(root) if args.staged else scan_files(root, tracked_files(root))
    if not findings:
        return 0
    print("Private material found (move it to .private/ or remove it; values are not shown):")
    for f in findings:
        print("  " + format_finding(f))
    return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
