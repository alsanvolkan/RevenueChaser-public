"""#400 (avo, 10 Nov 2026): the spine's ``security`` row carries Compustat's company SIC (spine schema 6).

``raw.sec_company_sic`` holds no rows on homesrv, so the relatedness profile had no SIC; the spine already reads
``comp.company.sic`` for its SPAC rule, and now keeps it. The bridge view ``security_master`` serves it too.
"""
from __future__ import annotations

from RevenueChaser.rcdata import read, serving

from .test_spine_spac_sic_240 import _sec, built_with  # noqa: F401 - the fixture build with one SIC replaced


def test_the_security_row_keeps_the_compustat_sic_as_four_digit_text(built_with):
    built_with("002968", "6331.0")                      # CNA; a pandas frame can hand a whole number as 6331.0
    assert _sec("CNA")["sic"] == "6331" and _sec("MSFT")["sic"] is None
    assert serving.SERVING_SCHEMA_VERSION["spine"] == 6
    assert [r["sic"] for r in read.identity(["CNA"])] == ["6331"]
    with serving.open_read("spine") as con:
        assert con.execute("SELECT sic FROM security_master WHERE ticker = 'CNA'").fetchone() == ("6331",)
