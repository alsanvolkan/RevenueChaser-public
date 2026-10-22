"""#10 (S-E): every command the migration's docstring documents must be one that can import the package.

Run by path (``python scripts/migrate_sec13f_v12.py``) the script puts its own directory on ``sys.path``, so the
absolute ``from RevenueChaser.core import rawstore`` at the top dies with ``ModuleNotFoundError`` before any check
runs; only the ``python -m RevenueChaser.scripts.migrate_sec13f_v12`` form finds the package. The assertion reads
``migrate.__doc__`` itself, not a copy of it, so the next edit cannot reintroduce the path form.
"""
from __future__ import annotations

from RevenueChaser.scripts import migrate_sec13f_v12 as migrate


def _command_lines(doc: str) -> list[str]:
    """The docstring lines that open a shell command for this script."""
    return [line.strip() for line in doc.splitlines()
            if "migrate_sec13f_v12" in line and line.strip().startswith("python ")]


def test_docstring_documents_only_the_module_form() -> None:
    commands = _command_lines(migrate.__doc__ or "")
    assert commands, "the docstring documents no runnable command; the assertion below would be vacuous"
    for line in commands:
        assert line.startswith("python -m RevenueChaser.scripts.migrate_sec13f_v12"), (
            f"documented command cannot import the package: {line!r}")
