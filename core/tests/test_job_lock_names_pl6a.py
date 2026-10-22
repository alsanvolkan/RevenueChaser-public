"""PL6-a (#26): the ``job_lock`` docstring lists every lock name in use, so the list cannot rot again.

#26's body rotted exactly this way: it described a single ``"edgar"`` lock that no longer existed. The module
docstring is the one place that says which names exist and who takes them, so it is asserted here — the thirteen
names by contract, plus every literal name the sources hand to ``acquire()``. A new literal lock name that is not in
the docstring fails this test.
"""
from __future__ import annotations

import re
from pathlib import Path

from RevenueChaser.core import job_lock

NAMES = (
    "edgar_sync",              # module_value_chain/sync.py (+ reslice)
    "relatedness",             # module_relatedness/engine.py (+ reslice)
    "universe",                # core/universe_fetch.py: the universe writer lock
    "universe_run",            # core/universe_fetch.py: the refresh-run marker beside it
    "board_fetch_<board>",     # core/universe_fetch.py: one single-board Fetch
    "daily_bars",              # core/prices.py
    "index_lists",             # core/index_registry.py
    "index_levels",            # core/index_levels.py
    "identity",                # core/identity/refresh.py: the dashboard job and its CLI (#26, PL6-a)
    "sec_13f",                 # app/runners.py: the three 13F jobs (#26, PL6-a)
    "discord",                 # module_discord/engine.py: update + discover (#26, PL6-a)
    "forecast_outcomes",       # scripts/forecast_outcomes.py (#26, PL6-a)
    "nightly_inputs_<step>",   # scripts/nightly_inputs.py, one per step (#26, PL6-a)
)


def test_the_docstring_names_every_lock():
    doc = job_lock.__doc__ or ""
    assert [n for n in NAMES if n not in doc] == [], "a lock name in use is missing from the job_lock docstring"
    assert '"edgar"' not in doc, "the docstring still describes the retired single 'edgar' lock"


def test_every_literal_lock_name_in_the_sources_is_documented():
    """The rot guard: a new ``acquire("name")`` nobody documented fails here, whatever NAMES says."""
    root = Path(job_lock.__file__).resolve().parents[1]
    assert (root / "core" / "job_lock.py").exists(), "the scan is looking at the wrong tree"
    found: set[str] = set()
    for path in root.rglob("*.py"):
        if "tests" in path.parts or path.name == "job_lock.py":
            continue
        found |= set(re.findall(r'acquire\(\s*"([a-z0-9_]+)"', path.read_text(encoding="utf-8")))
    assert found, "the scan found no literal lock name at all"
    assert found <= set(NAMES), f"undocumented lock name(s): {sorted(found - set(NAMES))}"
