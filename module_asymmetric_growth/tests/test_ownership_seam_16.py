"""#16: the Asymmetric Growth scan reads the stored 13F totals, not the 13F board's engine.

The owner's decision of 5 Nov 2026 is the "if yes" branch of the issue: institutional ownership stays part
of the Asymmetric Growth score, and the fetch is not re-run for it — the scan reads the rows that are already
in DuckDB. So the dependency that has to go is the *import*, not the input: ``engine.py`` called
``module_sec_13f.engine.load_totals()``, which meant an AG run could not be reasoned about or tested without
the 13F side present, even though the call itself only ever read one table.

``core/ownership.py`` is where the shared side lives (it already owns ``latest_institutional``, the reader
rule Asymmetric Growth uses for the institutional share), so the stored-totals read belongs there too. Since G9
the rows it returns are the builder's (``sec13f_ticker_period`` in ``ownership.duckdb``), not the retired
derive's.

Store only: the seeded serving file is a temp DuckDB per test, no provider is asked.
"""
from __future__ import annotations

import re
from pathlib import Path

from RevenueChaser.core import ownership
from RevenueChaser.rcdata.tests.ownership_fixture import serve_13f

PKG = Path(__file__).resolve().parent.parent


def _entry(shares: float) -> dict:
    """One quarter in the shape the reader returns: every metric a cell carries, the scan's among them, plus
    ``complete`` — the period's filing deadline against today (#514). The fixture writes no deadline, which counts
    as settled."""
    return {"2026-03-31": {"shares": shares, "value": shares * 10.0, "holders": 10, "top10_pct": None,
                           "hhi": None, "new_holders": 2, "exited_holders": 1, "buy_flow": 5.0,
                           "sell_flow": 1.0, "breadth": None, "breadth_chg": None, "complete": True}}


def test_the_served_13f_totals_are_read_through_the_shared_reader():
    serve_13f({"AAA": _entry(100.0), "BBB": _entry(250.0)})

    totals = ownership.sec13f_totals()

    assert totals == {"AAA": _entry(100.0), "BBB": _entry(250.0)}


def test_a_metric_the_quarter_has_no_value_for_is_none_rather_than_missing():
    """An arriving quarter leaves columns NULL; the scan must see the key with None, never a KeyError."""
    serve_13f({"AAA": {"2026-03-31": {"shares": 100.0}}})

    (cell,) = ownership.sec13f_totals()["AAA"].values()
    assert cell["shares"] == 100.0 and cell["holders"] is None and cell["buy_flow"] is None


def test_an_empty_store_is_an_empty_mapping_not_a_failure():
    assert ownership.sec13f_totals() == {}


def test_nothing_in_asymmetric_growth_imports_the_13f_board():
    """The seam itself, over the package **and its tests**: a scan reads rows, and nothing here reaches into
    another board's engine. The tests are in scope because nine of them used to patch
    ``module_sec_13f.engine.load_totals`` — a patch that intercepted nothing once the engine stopped calling
    it, so a later test seeding totals through it would silently have got ``{}`` from the store instead.

    Import lines only: a docstring that names the other board to explain where a number comes from is
    provenance, not a dependency.
    """
    offenders = [f"{path.relative_to(PKG.parent)}:{n}: {line.strip()}"
                 for path in sorted(PKG.rglob("*.py"))
                 if path != Path(__file__).resolve()
                 for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
                 if "module_sec_13f" in line and re.match(r"\s*(from|import)\s", line)]

    assert offenders == []
