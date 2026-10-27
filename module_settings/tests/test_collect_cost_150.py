"""#150: one Settings collect asks each store question once.

The row builders and each dataset's ``Health`` ask the same health summaries, and the row builder and
the Fresh cell ask the same newest date. ``test_identity_coverage_124`` compares one collect with another,
so a question asked twice in both passes there; these tests count the runs inside ONE collect.

Store only: the seeded store is a temp DuckDB per store file, no provider is asked.
"""
from __future__ import annotations

import sys
from collections import Counter

import pytest

from RevenueChaser.core import datahealth, rawstore
from RevenueChaser.module_settings import inventory
from RevenueChaser.module_settings.tests.test_identity_coverage_124 import FIXED, _seed

# Every summary a dataset row and a registry coverage both ask (core/datasets.py ``_summary_health``).
SUMMARIES = (
    "core.prices", "core.quotes", "core.vendor_data", "core.index_levels", "core.sec_files",
    "core.sec_submissions", "module_sector_rotation.etf_holdings",
)


def _module(dotted: str):
    __import__(f"RevenueChaser.{dotted}")
    return sys.modules[f"RevenueChaser.{dotted}"]


def test_each_health_summary_runs_once_in_one_collect(rawstore_tmp_db, monkeypatch):
    """The body of every summary runs once per collect, whoever asks it and with whatever arguments.

    The count sits under the memo (``datahealth.scoped`` wraps the counter), so a cache hit is not
    counted and a second run is.
    """
    _seed()
    runs: Counter = Counter()
    for dotted in SUMMARIES:
        module = _module(dotted)
        body = module.health_summary.__wrapped__

        def counted(*a, _body=body, _name=dotted, **k):
            runs[_name] += 1
            return _body(*a, **k)

        counted.__module__, counted.__qualname__ = body.__module__, body.__qualname__
        counted.__wrapped__ = body
        monkeypatch.setattr(module, "health_summary", datahealth.scoped(counted))

    inventory.collect(now=FIXED)

    assert {name: runs[name] for name in SUMMARIES} == {name: 1 for name in SUMMARIES}


def test_scoped_treats_a_default_argument_and_an_omitted_one_as_one_call():
    """``f()``, ``f(None)`` and ``f(now=None)`` are one question: the key is the bound arguments."""
    runs = []

    @datahealth.scoped
    def summary(now=None):
        runs.append(now)
        return {"n": len(runs)}

    with datahealth.memo_scope():
        assert summary() == summary(None) == summary(now=None) == {"n": 1}
    assert runs == [None]


def test_each_newest_date_is_read_once_in_one_collect(rawstore_tmp_db, monkeypatch):
    """The row's ``last_date`` and the Fresh cell are one read of ``MAX(column)``, not two."""
    _seed()
    asked: Counter = Counter()
    real = rawstore.query

    def query(sql, *a, **k):
        if sys._getframe(1).f_code.co_filename == inventory.__file__ and sql.startswith("SELECT MAX("):
            asked[sql] += 1
        return real(sql, *a, **k)

    monkeypatch.setattr(rawstore, "query", query)
    inventory.collect(now=FIXED)

    assert asked, "the collect read some newest dates"
    assert {sql: n for sql, n in asked.items() if n > 1} == {}


def test_a_failed_newest_date_read_stays_failed_in_the_fresh_cell(rawstore_tmp_db, monkeypatch):
    """Reading once must not turn a failure into a date: the row's dash and the Fresh cell's
    "query failed" both come from the one failed read."""
    _seed()
    real = rawstore.query

    def query(sql, *a, **k):
        if sql == "SELECT MAX(session_date) AS m FROM quote_snapshot":
            raise RuntimeError("locked")
        return real(sql, *a, **k)

    monkeypatch.setattr(rawstore, "query", query)
    payload = inventory.collect(now=FIXED)
    row = next(r for r in payload["rows"] if r["key"] == "quotes")
    assert row["last_date"] == inventory.DASH
    assert row["fresh"]["state"] == inventory.FRESH_FAILED


def test_the_collect_clock_reaches_the_registry_coverage(rawstore_tmp_db, monkeypatch):
    """The coverage of a dated summary is read at the collect's own clock, not the wall clock."""
    _seed()
    from RevenueChaser.core import prices
    seen = []
    body = prices.health_summary.__wrapped__

    def counted(now=None):
        seen.append(now)
        return body(now)

    counted.__module__, counted.__qualname__ = body.__module__, body.__qualname__
    monkeypatch.setattr(prices, "health_summary", datahealth.scoped(counted))
    inventory.collect(now=FIXED)
    assert seen == [FIXED]


def test_a_failed_newest_date_read_is_recorded_once(rawstore_tmp_db, monkeypatch):
    """#205 review: the row's date and the Fresh cell both caught the one cached failure and each recorded it, so
    the board listed one broken read twice."""
    _seed()
    real = rawstore.query

    def query(sql, *a, **k):
        if sql == "SELECT MAX(session_date) AS m FROM quote_snapshot":
            raise RuntimeError("locked")
        return real(sql, *a, **k)

    monkeypatch.setattr(rawstore, "query", query)
    # The two catchers of the cached read; quotes.health_summary is a read of its own and records its own failure.
    errors = [e for e in inventory.collect(now=FIXED)["errors"]
              if e["table"] == "quote_snapshot" and e["where"] in ("_max_value", "_fresh")]
    assert len(errors) == 1, errors


def test_the_date_cache_raises_a_fresh_error_wrapping_the_first(rawstore_tmp_db, monkeypatch):
    """Re-raising the stored exception object grows its traceback on every hit; a new error keeps the first as its
    cause."""
    real = rawstore.query

    def query(sql, *a, **k):
        if sql.startswith("SELECT MAX(session_date) AS m FROM quote_snapshot"):
            raise RuntimeError("locked")
        return real(sql, *a, **k)

    monkeypatch.setattr(rawstore, "query", query)
    monkeypatch.setattr(inventory, "_table_exists", lambda name: True)
    inventory._begin_call()
    try:
        with pytest.raises(RuntimeError) as first:
            inventory._max_value_or_raise("quote_snapshot", "session_date")
        with pytest.raises(Exception) as again:
            inventory._max_value_or_raise("quote_snapshot", "session_date")
    finally:
        inventory._end_call()
    assert again.value is not first.value
    assert again.value.__cause__ is first.value
    assert str(again.value) == "locked"
