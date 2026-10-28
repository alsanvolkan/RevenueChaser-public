"""``sec/fsds`` (build plan PR 12): the SEC's quarterly Financial Statement Data Sets as a check of the served facts
(a recorded-shape zip, never the SEC)."""
from __future__ import annotations

import io
import zipfile

import pytest

from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.rcdata.build.fundamentals import companyfacts, fsds

from . import fund_fixture as fx

# The columns as the SEC's data-set notes list them (a subset of sub.txt's; num.txt's with ``segments``).
SUB = ("adsh\tcik\tname\tsic\tform\tperiod\tfy\tfp\tfiled\n"
       f"{fx.Q26}\t789019\tMICROSOFT CORP\t7372\t10-Q\t20250930\t2026\tQ1\t20251029\n"
       "0000072971-25-000099\t72971\tWELLS FARGO\t6021\t10-Q\t20250930\t2025\tQ3\t20251030\n")
NUM = ("adsh\ttag\tversion\tddate\tqtrs\tuom\tsegments\tcoreg\tvalue\tfootnote\n"
       f"{fx.Q26}\tRevenues\tus-gaap/2025\t20250930\t1\tUSD\t\t\t77000\t\n"
       f"{fx.Q26}\tNetIncomeLoss\tus-gaap/2025\t20250930\t1\tUSD\t\t\t27000\t\n"
       f"{fx.Q26}\tAssets\tus-gaap/2025\t20250930\t0\tUSD\t\t\t510000\t\n"
       f"{fx.Q26}\tEarningsPerShareDiluted\tus-gaap/2025\t20250930\t1\tUSD/shares\t\t\t3.70\t\n"
       f"{fx.Q26}\tRevenues\tus-gaap/2025\t20250930\t1\tUSD\tBusinessSegments=Cloud;\t\t30000\t\n"   # a segment
       f"{fx.Q26}\tRevenues\tus-gaap/2025\t20250930\t1\tUSD\t\tSubsidiaryCo\t1\t\n"                 # a co-registrant
       "0000072971-25-000099\tRevenues\tus-gaap/2025\t20250930\t1\tUSD\t\t\t20000\t\n")             # Tier B


def _zip() -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("sub.txt", SUB)
        z.writestr("num.txt", NUM)
        z.writestr("readme.htm", "<html></html>")
    return out.getvalue()


@pytest.fixture
def sec(monkeypatch, http_mock):
    monkeypatch.setattr(sec_edgar, "SEC_WWW_BASE", "https://sec.test")
    monkeypatch.setattr(sec_edgar, "SEC_DATA_BASE", "https://data.sec.test")
    fx.spine()
    fx.sec_routes(http_mock, {"789019": fx.msft_facts()})
    fx.run(companyfacts.NAME, mode="backfill", to="2026-10-26")
    http_mock.add("financial-statement-data-sets/2026q2.zip", _zip())    # 2026q3 not published yet: a 404
    return http_mock


def test_the_newest_published_quarter_checks_the_served_facts(sec):
    check = fx.run(fsds.NAME, "2026-10-28")
    assert check["result"] == "changed", check["note"]
    assert [str(r.url).rsplit("/", 1)[1] for r in sec.calls("financial-statement-data-sets")] == [
        "2026q3.zip", "2026q2.zip"]
    # MSFT's revenue, net income and assets agree; its diluted EPS (3.65 served) does not. The segment row, the
    # co-registrant's and the Tier B company's are not compared.
    assert "FSDS 2026q2: 3 of 4 values agree with the served facts (revenue 1 of 1, net income 1 of 1, " \
           "total assets 1 of 1, diluted EPS 0 of 1)" in check["note"]
    calls = len(sec.calls("financial-statement-data-sets"))
    fx.run(fsds.NAME, "2026-11-02")
    assert [str(r.url).rsplit("/", 1)[1] for r in sec.calls("financial-statement-data-sets")[calls:]] == [
        "2026q3.zip"]                                    # the held quarter is not asked again


def test_only_the_tier_a_companies_own_values_are_kept(sec):
    fx.run(fsds.NAME, "2026-10-28")
    import duckdb
    from RevenueChaser.rcdata.build import base
    files = list(base.parquet_root().rglob("sec/fsds/*/part-0.parquet"))
    rows = duckdb.connect().execute(f"SELECT cik, tag, value FROM read_parquet({base.sql_path(files[0])}) "
                                    "ORDER BY tag").fetchall()
    # The company's own values only: not the segment's, not the co-registrant's, not the Tier B company's.
    assert rows == [("789019", "Assets", 510_000.0), ("789019", "EarningsPerShareDiluted", 3.70),
                    ("789019", "NetIncomeLoss", 27_000.0), ("789019", "Revenues", 77_000.0)]
