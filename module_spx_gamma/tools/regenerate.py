"""Offline regeneration of module_spx_gamma artifacts from stored raw days.

A thin wrapper over :func:`run.refresh` (the dashboard's Refresh path): the same loader, the same
``normalize_snapshot`` and the same render, with no network and no backfill. Raw files are never
written.

Usage: python -m RevenueChaser.module_spx_gamma.tools.regenerate [<day>...]
       (no day = the newest stored raw day)
"""
from __future__ import annotations

import sys

from RevenueChaser.module_spx_gamma.run import refresh


def regenerate(day: str | None = None) -> None:
    out = refresh(day)
    if out is None:
        raise FileNotFoundError(f"no stored raw day{' for ' + day if day else ''}")
    snap = out["snapshot"] if "snapshot" in out else {}
    spot = snap.get("current_level")
    pin = (snap.get("prediction") or {}).get("active_pin")
    print(f"{day or out['latest']['asof_date']}: spot={spot} pin={pin}")


if __name__ == "__main__":
    for d in sys.argv[1:] or [None]:
        regenerate(d)
