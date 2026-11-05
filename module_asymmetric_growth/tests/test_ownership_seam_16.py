"""#16: the Asymmetric Growth scan reads the stored 13F totals, not the 13F board's engine.

The owner's decision of 5 Nov 2026 is the "if yes" branch of the issue: institutional ownership stays part
of the Asymmetric Growth score, and the fetch is not re-run for it — the scan reads the rows that are already
in DuckDB. So the dependency that has to go is the *import*, not the input: ``engine.py`` called
``module_sec_13f.engine.load_totals()``, which meant an AG run could not be reasoned about or tested without
the 13F side present, even though the call itself only ever read one table.

``core/ownership.py`` is where the shared side lives (it already owns ``latest_institutional``, the reader
rule Asymmetric Growth uses for the institutional share), so the stored-totals read belongs there too.

Store only: the seeded table is a temp DuckDB per test, no provider is asked.
"""
from __future__ import annotations

from pathlib import Path

from RevenueChaser.core import ownership, rawstore

PKG = Path(__file__).resolve().parent.parent


def _entry(shares: float) -> dict:
    return {"2026-03-31": {"holders": 10, "new_holders": 2, "exited_holders": 1,
                           "buy_flow": 5.0, "sell_flow": 1.0, "shares": shares}}


def test_the_stored_13f_totals_are_read_through_the_shared_reader(rawstore_tmp_db):
    rawstore.write_rows("sec13f_totals", [{"ticker": "AAA", "entry": _entry(100.0)},
                                          {"ticker": "BBB", "entry": _entry(250.0)}])

    totals = ownership.sec13f_totals()

    assert totals == {"AAA": _entry(100.0), "BBB": _entry(250.0)}


def test_a_row_whose_entry_is_not_a_mapping_is_left_out_rather_than_scored(rawstore_tmp_db):
    """The old reader skipped those silently and the scan must keep doing so: a malformed entry is a name
    with no institutional block, never a crashed scan."""
    rawstore.write_rows("sec13f_totals", [{"ticker": "AAA", "entry": _entry(100.0)},
                                          {"ticker": "BAD", "entry": None}])

    assert ownership.sec13f_totals() == {"AAA": _entry(100.0)}


def test_an_empty_table_is_an_empty_mapping_not_a_failure(rawstore_tmp_db):
    assert ownership.sec13f_totals() == {}


def test_no_module_of_asymmetric_growth_imports_the_13f_board():
    """The seam itself. A scan reads rows; it does not reach into another board's engine."""
    offenders = [f"{p.relative_to(PKG.parent)}:{n}"
                 for p in sorted(PKG.glob("*.py"))
                 for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
                 if "module_sec_13f" in line]

    assert offenders == []
