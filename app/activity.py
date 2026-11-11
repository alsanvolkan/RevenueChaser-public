"""Which board each scheduled job feeds (``GET /api/activity/boards``), read by the Grafana boards ``rc-activity`` and
``rc-logs`` (home-server homeops), which group RevenueChaser's jobs and their run logs by board (avo, 10 Nov 2026:
"RC activity yi dashboardlara gore ayirsan daha kolay olur"). Settings' Fetch/Health tab frames both.

The Activity page that used to frame them (``/view/activity``) was closed on 11 Nov 2026 (avo: "activity yi kapatmak
istiyorum"): its Add box went to Settings' watch-list cards, and the address redirects to Settings.

``job_boards`` is worked out from the code on each call, so a new board or job lands in place with no list to keep:
a scheduled job feeds a board when a table it writes (``AutomationEntry.tables_written``) is named in that board's
code. Settings reads every table, so it is left out; a job that feeds no board, or writes no table, is ``System``
unless ``BY_NAME`` places it.
"""
from __future__ import annotations

import functools
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SYSTEM = "System"
#: A job that writes no table but plainly serves one board.
BY_NAME = {"RC-SPX-Gamma-Fetch": "module_spx_gamma"}
#: The builder, the backup and the checks: they serve every board, so they are System, whatever they write.
SYSTEM_JOBS = frozenset({"RC-Build", "RC-Backup-Weekly", "RC-IBKR-Login-Check"})
#: Settings reads every table, so naming it would put every job under it.
NOT_A_FEED = frozenset({"module_settings"})


@functools.lru_cache(maxsize=1)
def _board_code() -> dict[str, str]:
    """Each board's own code (tests left out), keyed by its tab key. Read once a process: the code does not change
    under a running dashboard."""
    from RevenueChaser.app import dashboard
    out = {}
    for t in dashboard.TABS:
        key = t.get("key") or ""
        folder = ROOT / key
        if t.get("type") == "tab" and key.startswith("module_") and key not in NOT_A_FEED and folder.is_dir():
            out[key] = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in sorted(folder.rglob("*.py"))
                                 if "tests" not in p.parts)
    return out


def job_boards() -> dict:
    """``{"boards": [{"key", "label", "jobs": [registry names]}], "jobs": {name: {"unit", "label", "boards"}}}``
    in menu order, ``System`` last. A job is listed under every board it feeds."""
    from RevenueChaser.app import dashboard
    from RevenueChaser.core import automations
    code = _board_code()
    labels = {t["key"]: t["label"] for t in dashboard.TABS if t.get("type") == "tab"}
    jobs, by_board = {}, {k: [] for k in code}
    by_board[SYSTEM] = []
    for e in automations.REGISTRY:
        if e.name in SYSTEM_JOBS:
            feeds = []
        elif e.name in BY_NAME:
            feeds = [BY_NAME[e.name]]
        else:
            names = [re.compile(rf"""['"\s.]{re.escape(t)}['"\s]""") for t in e.tables_written]
            feeds = [k for k, src in code.items() if any(n.search(src) for n in names)]
        for k in feeds or [SYSTEM]:
            by_board[k].append(e.name)
        jobs[e.name] = {"unit": e.unit, "label": e.label, "boards": feeds or [SYSTEM]}
    boards = [{"key": k, "label": labels.get(k, k), "jobs": v} for k, v in by_board.items() if v and k != SYSTEM]
    boards.append({"key": SYSTEM, "label": SYSTEM, "jobs": by_board[SYSTEM]})
    return {"boards": boards, "jobs": jobs}


def pressed_by_board(rows: list[dict]) -> dict[str, dict]:
    """The dashboard's own job runs (``runview.read``'s rows), per board key: how many, the newest start and how
    many failed or were lost. A run whose module is not a board is ``System``."""
    from RevenueChaser.app import dashboard
    keys = {t["key"] for t in dashboard.TABS if t.get("type") == "tab"}
    out: dict[str, dict] = {}
    for r in rows:
        key = r.get("module") if r.get("module") in keys else SYSTEM
        s = out.setdefault(key, {"runs": 0, "last": None, "bad": 0})
        s["runs"] += 1
        s["last"] = max(filter(None, (s["last"], r.get("started_at"))), default=None)
        s["bad"] += r.get("status") in ("failed", "lost")
    return out
