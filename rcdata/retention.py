"""How many days raw option files are kept: the one rule the builder's retention hook and the Settings card both read.

``RC_RAW_RETENTION_DAYS`` (the environment) first, else the ``dataset_schedule`` row ``retention_days`` with the days
as its cadence. Unset, or not a whole number of days of at least 1, means no retention: nothing is removed and the
Settings card's warning stays on (``module_settings/newdata.py``). Which files a retention removes is the hook's
(``rcdata/build/options/raw_retention.py``).
"""
from __future__ import annotations

import os

from . import ops

ENV = "RC_RAW_RETENTION_DAYS"
ROW = "retention_days"          # a dataset_schedule row whose cadence holds the days


def _whole_days(text) -> int | None:
    text = str(text or "").strip()
    return int(text) if text.isdigit() and int(text) >= 1 else None


def days() -> tuple[int | None, str | None]:
    """``(days, where it was set)``, or ``(None, None)`` while no retention is set."""
    env = _whole_days(os.getenv(ENV))
    if env is not None:
        return env, f"{ENV}={env}"
    row = _whole_days((ops.read_schedule(ROW) or {}).get("cadence"))
    if row is not None:
        return row, f"dataset_schedule row {ROW!r}"
    return None, None
