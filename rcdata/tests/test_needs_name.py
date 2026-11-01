"""The needs layer has one name (avo, 1 Nov 2026): the package is ``rcdata.needs``, the call is ``needs.ask``, the
routes are ``/needs``. The old names (``rcdata.api``, ``needs.get``, ``/api/data``) must not creep back in."""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
OLD = re.compile(r"rcdata[./]api\b|\bapi\.get\(|/api/data(?![_a-z])")
SKIP_PARTS = {".venv", "__pycache__", ".git"}


def _sources():
    for path in ROOT.rglob("*"):
        if path.suffix not in (".py", ".md", ".html", ".js") or SKIP_PARTS & set(path.parts):
            continue
        if path == pathlib.Path(__file__):
            continue
        yield path


def test_the_package_is_called_needs():
    from RevenueChaser.rcdata import needs
    assert callable(needs.ask) and not hasattr(needs, "get")


def test_no_file_names_the_old_api():
    offenders = [str(p.relative_to(ROOT)) for p in _sources() if OLD.search(p.read_text(encoding="utf-8", errors="replace"))]
    assert offenders == [], f"the old name is back in: {offenders}"
