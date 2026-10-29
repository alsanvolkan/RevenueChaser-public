"""The raw option retention hook: run after each ``options`` swap (``uw/option_list.after_swap``).

With no retention set (``rcdata/retention.py``) it does nothing, which is how it ships: the value is avo's to set,
and the Settings card warns until then. With N days set, it removes the per-session raw folders of the option data,
``raw/uw/option_list/<S>/`` and ``raw/uw/er_options/<S>/``, whose session S is more than N days before the run's
session: their ``*.json.gz`` files, then the emptied folder. Never anything else: the Parquet is what the serving
file is built from, the earnings calendar and the screener answers are small, and an IBKR raw file is the marker
that the name has its year.
"""
from __future__ import annotations

from datetime import date, timedelta

from ... import retention
from .. import base

FOLDERS = (("uw", "option_list"), ("uw", "er_options"))


def apply(ctx: base.RunContext, as_of: date) -> int:
    """Remove the expired session folders; the number of files removed (0 while no retention is set)."""
    days, where = retention.days()
    if days is None:
        return 0
    cutoff = as_of - timedelta(days=days)
    files = sessions = 0
    for parts in FOLDERS:
        root = base.raw_root().joinpath(*parts)
        if not root.is_dir():
            continue
        for folder in sorted(root.iterdir()):
            try:
                session = date.fromisoformat(folder.name)
            except ValueError:
                continue                    # not a session folder: never touched
            if not folder.is_dir() or session >= cutoff:
                continue
            for f in folder.glob("*.json.gz"):
                f.unlink()
                files += 1
            sessions += 1
            if not any(folder.iterdir()):
                folder.rmdir()
    if files:
        ctx.notes.append(f"raw retention {days} days: {files} files of {sessions} sessions removed ({where})")
    return files
