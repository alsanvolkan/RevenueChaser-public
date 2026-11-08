"""Run a PR's new tests against its merge-base: a new test that already passes there proves nothing about the change.

    python scripts/red_on_base.py <base ref> [--repo DIR] [--work DIR] [--python PY]

"Seen red first" was a sentence in a commit message; this makes it a CI result (plan
home-server/2026-11-08_review-llm-coder-llm-plan.md step 3, avo 8 Nov 2026). The 6 Nov coder-push retro
(claude-coordinator retros/2026-11-06-coder-push.md) found a commit that said "all red first" when two of its
tests were not.

How: the test modules the PR adds or changes (`test_*.py`, outside conftest) are collected on the head and, where
the file exists there, on the merge-base; the ids the base file lacks are the PR's new tests. The merge-base is
checked out into a scratch worktree, the PR's version of those files is copied in, and only the new ids run there.
A new test that fails, errors or cannot even be collected on the base is red-first. A new test that passes there
is listed, and the exit is 1. A PR without new tests exits 0 and says so.

The worktree sits in a folder named after the repository's top folder, because the code imports itself as
`RevenueChaser.*`. It is removed afterwards; the repository's own checkout is never touched.

Exit: 0 every new test is red on the base (or there are none), 1 some pass there, 2 usage or git failure.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath

_ID = re.compile(r"^\S+\.py::\S")


def is_test_file(path: str) -> bool:
    """A pytest module by the default naming: test_*.py, never a conftest."""
    name = PurePosixPath(path).name
    return name.startswith("test_") and name.endswith(".py")


def new_ids(head: list[str], base: list[str]) -> list[str]:
    """The head ids the base version of the file did not have, in head order."""
    seen = set(base)
    return [i for i in head if i not in seen]


def parse_collected(out: str) -> list[str]:
    """Node ids from `pytest --collect-only -q` (one per line; the summary line is not one)."""
    return [ln.strip() for ln in out.splitlines() if _ID.match(ln.strip())]


def passed_in_junit(xml_text: str) -> set[tuple[str, str]]:
    """(classname, test name) of every testcase without failure, error or skipped."""
    root = ET.fromstring(xml_text)
    passed = set()
    for case in root.iter("testcase"):
        if any(case.find(tag) is not None for tag in ("failure", "error", "skipped")):
            continue
        passed.add((case.get("classname") or "", case.get("name") or ""))
    return passed


def _id_key(node_id: str) -> tuple[str, str]:
    """(classname, test name) of a node id, the way pytest's junit writes it: the module path with dots for
    slashes and no .py, then any class; the last :: part is the name."""
    parts = node_id.split("::")
    module = parts[0][:-3] if parts[0].endswith(".py") else parts[0]
    return ".".join([module.replace("/", "."), *parts[1:-1]]), parts[-1]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


# No __pycache__ in the repository's own checkout, which the check must leave as it found it.
_ENV = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}


def _collect(py: str, cwd: Path, files: list[str]) -> list[str]:
    """Node ids pytest collects from these files in cwd; a file that cannot be collected gives none."""
    if not files:
        return []
    r = subprocess.run([py, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", *files],
                       cwd=cwd, capture_output=True, text=True, env=_ENV)
    return parse_collected(r.stdout)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="red_on_base", description=__doc__.split("\n\n")[0])
    ap.add_argument("base", help="the base ref, e.g. origin/main")
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--work", default=None, help="scratch folder for the base worktree (default: a temp folder)")
    ap.add_argument("--python", default=sys.executable)
    a = ap.parse_args(argv)
    repo = Path(a.repo).resolve()
    try:
        merge_base = _git(repo, "merge-base", a.base, "HEAD").strip()
        changed = _git(repo, "diff", "--name-only", "--diff-filter=AM", f"{merge_base}..HEAD").split()
    except subprocess.CalledProcessError as exc:
        print(f"red-on-base: git failed: {(exc.stderr or '').strip()}", file=sys.stderr)
        return 2
    tests = [p for p in changed if is_test_file(p)]
    if not tests:
        print("red-on-base: no new tests (no test module added or changed)")
        return 0

    work = Path(a.work) if a.work else Path(tempfile.mkdtemp(prefix="red-on-base-"))
    work.mkdir(parents=True, exist_ok=True)
    base_tree = work / repo.name
    try:
        _git(repo, "worktree", "add", "--detach", "-q", str(base_tree), merge_base)
        # The base file's own ids, collected before the PR's version replaces it.
        base_has = [p for p in tests if (base_tree / p).exists()]
        base_ids = _collect(a.python, base_tree, base_has)
        head_ids = _collect(a.python, repo, tests)
        fresh = new_ids(head_ids, base_ids)
        if not fresh:
            print("red-on-base: no new tests (the changed test modules add no test id)")
            return 0
        for p in tests:
            dest = base_tree / p
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(repo / p, dest)
        junit = work / "red-on-base.xml"
        subprocess.run([a.python, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={junit}", *fresh],
                       cwd=base_tree, capture_output=True, text=True, env=_ENV)
        passed = passed_in_junit(junit.read_text(encoding="utf-8")) if junit.exists() else set()
    except subprocess.CalledProcessError as exc:
        print(f"red-on-base: git failed: {(exc.stderr or '').strip()}", file=sys.stderr)
        return 2
    finally:
        subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(base_tree)],
                       capture_output=True, text=True)
        subprocess.run(["git", "-C", str(repo), "worktree", "prune"], capture_output=True, text=True)
        if not a.work:
            shutil.rmtree(work, ignore_errors=True)

    green = [i for i in fresh if _id_key(i) in passed]
    red = [i for i in fresh if i not in green]
    print(f"red-on-base: {len(fresh)} new test(s) run on the merge-base {merge_base[:12]}")
    for i in red:
        print(f"  red on base (good): {i}")
    if green:
        print(f"These {len(green)} new test(s) pass on the base, so they prove nothing about this change:")
        for i in green:
            print(f"  {i}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
