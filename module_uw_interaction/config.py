"""UW Interaction (lab) — config.

The board is a live-only explorer: each run queries one Unusual Whales tool and
writes a regenerable JSON payload (``UW_STATE_JSON``). There is no ``.store/``
write and no typed-column promotion: the payload is a *last-run snapshot* for a
lab, never a scan/join target, so the hot-path rule does not apply. Paths come
from ``core.config.OUTPUT_DIR`` so the ``RC_CACHE_DIR`` test redirect works.
"""
from RevenueChaser.core.config import OUTPUT_DIR

# Durable per-module artifacts do not belong here; this JSON is regenerable and
# lives under .cache/ like every other *_latest payload (financials, v2, …).
UW_STATE_DIR = OUTPUT_DIR / "uw_interaction"
UW_STATE_JSON = UW_STATE_DIR / "uw_interaction_latest.json"

import json
from datetime import datetime, timezone


def load_state() -> dict | None:
    """The last-run payload (None when no run has happened yet)."""
    if not UW_STATE_JSON.exists():
        return None
    try:
        data = json.loads(UW_STATE_JSON.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def save_state(payload: dict) -> None:
    """Persist a run payload under .cache/ (regenerable, never .store/).

    Written atomically (#258): a concurrent reader sees either the old payload or
    the new one, never a half-written file that ``load_state`` reads as ``None``.
    """
    from RevenueChaser.core.reporting_common import write_artifact
    write_artifact(UW_STATE_JSON, json.dumps(payload, ensure_ascii=False, default=str))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
