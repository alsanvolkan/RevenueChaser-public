"""PL6-a (#26): the ``job_lock`` docstring lists every lock name in use, so the list cannot rot again.

#26's body rotted exactly this way: it described a single ``"edgar"`` lock that no longer existed. The module
docstring is the one place that says which names exist and who takes them, so it is asserted here — the thirteen
names by contract, plus every name the sources hand to ``acquire()``, whether it is a literal or a module-level
constant. A new lock name that is not in the docstring fails this test.
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
    "forecast_outcomes",       # scripts/forecast_outcomes.py (#26, PL6-a)
    "forecast_baselines",     # scripts/forecast_baselines.py (#25 HR-1)
    "nightly_inputs_<step>",   # scripts/nightly_inputs.py, one per step (#26, PL6-a)
)


def test_the_docstring_names_every_lock():
    doc = job_lock.__doc__ or ""
    assert [n for n in NAMES if n not in doc] == [], "a lock name in use is missing from the job_lock docstring"
    assert '"edgar"' not in doc, "the docstring still describes the retired single 'edgar' lock"


def _sources() -> list[Path]:
    """Every source file the scan reads: no test module, and not ``job_lock`` itself (its docstring is the list)."""
    root = Path(job_lock.__file__).resolve().parents[1]
    assert (root / "core" / "job_lock.py").exists(), "the scan is looking at the wrong tree"
    return [p for p in root.rglob("*.py") if "tests" not in p.parts and p.name != "job_lock.py"]


def _literal_names() -> set[str]:
    """The names handed to ``acquire()`` as a literal, e.g. ``acquire("daily_bars", wait=0)``."""
    return {name for path in _sources()
            for name in re.findall(r'acquire\(\s*"([a-z0-9_]+)"', path.read_text(encoding="utf-8"))}


def _constant_names() -> set[str]:
    """The names held in a module-level lock constant, e.g. ``_LOCK = "identity"`` or ``_RUN_LOCK_NAME = "x"``.

    ``acquire(_LOCK)`` passes a name the literal scan cannot see, so every constant of that shape is collected too.
    """
    return {name for path in _sources()
            for name in re.findall(r'^(?:[A-Z_]*_)?LOCK(?:_NAME)?\s*=\s*"([a-z0-9_]+)"',
                                   path.read_text(encoding="utf-8"), re.MULTILINE)}


def _scanned_names() -> set[str]:
    """Every lock name the guard judges: the literals plus the constants.

    The guard's own assertion is a subset test, so dropping one half of this union only shrinks the set and
    still passes. Both halves therefore live here, and the constant test below pins that this function really
    carries them — otherwise "simplifying" the guard back to literals would silently stop guarding five names.
    """
    return _literal_names() | _constant_names()


def test_every_lock_name_in_the_sources_is_documented():
    """The rot guard: a new lock name nobody documented fails here, whatever NAMES says."""
    found = _scanned_names()
    assert found, "the scan found no lock name at all"
    assert found <= set(NAMES), f"undocumented lock name(s): {sorted(found - set(NAMES))}"


def test_a_lock_name_passed_as_a_constant_is_found():
    """A name only a constant carries — no literal anywhere — still has to reach the scan, or the guard misses it."""
    literals, constants = _literal_names(), _constant_names()
    assert {"identity", "universe", "universe_run", "index_levels"} <= constants
    assert "universe_run" not in literals, "core/universe_fetch._RUN_LOCK_NAME is the only source of this name"
    assert constants <= set(NAMES)
    # What the guard actually judges has to contain them: this is the assertion that keeps the union in place.
    assert constants <= _scanned_names(), "the guard stopped scanning constant lock names"
    # And the mirror of it, so neither half of the union can be dropped: a literal-only name has to be judged too.
    assert "sec_13f" in literals and "sec_13f" not in constants, "app/runners.py passes this one as a literal"
    assert literals <= _scanned_names(), "the guard stopped scanning literal lock names"
