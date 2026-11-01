"""``sec/ncen``: the SEC's quarterly Form N-CEN data sets, the ETF universe's second half (#346).

The fetch is ``sec/fsds``'s (the newest quarter that has ended, a 404 is "not published yet", a quarter held is not
asked again); what is new is the read of two of the zip's tables joined on ``FUND_ID``, and the filers kept so a
trust that files no N-CEN at all can be told apart (``etf``'s commodity rule).
"""
from __future__ import annotations

from datetime import date

import duckdb
import pytest

from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.rcdata.build import base
from RevenueChaser.rcdata.build.spine import ncen

from . import spine_fixture as fx

ROOT = "https://sec.test/files/dera/data/form-n-cen-data-sets/"


@pytest.fixture
def env(monkeypatch, http_mock):
    monkeypatch.setattr(sec_edgar, "SEC_WWW_BASE", "https://sec.test")
    monkeypatch.setenv("RC_HTTP_CONTACT", "ops@example.org")
    fx.add_ncen_routes(http_mock)
    return http_mock


def _asked(env) -> list[str]:
    return [str(r.url).rsplit("/", 1)[1] for r in env.requests]


def _run(day="2026-11-02", **params) -> dict:
    return base.run_request({"dataset": ncen.NAME, "params": {"as_of": day, **params}})


def _read(quarter: str, name: str = "part-0.parquet", select: str = "*", order: str = "") -> list[tuple]:
    path = ncen.parquet_dir() / f"quarter={quarter}" / name
    sql = f"SELECT {select} FROM read_parquet('{path}')" + (f" ORDER BY {order}" if order else "")
    return duckdb.connect().execute(sql).fetchall()


def test_the_daily_run_holds_the_last_four_published_quarters_and_types_them(env):
    """Review M1 of PR 349: a first Fetch behaves like the Backfill, so the commodity rule never meets one quarter's
    filers (2026q3 had 396 filer CIKs, 2026q1 alone 1,796)."""
    row = _run()
    assert row["result"] == "changed", row["note"]
    assert _asked(env) == ["2026q3_ncen.zip", "2026q2_ncen.zip", "2026q1_ncen.zip", "2025q4_ncen.zip"]
    req = env.requests[0]
    assert str(req.url) == ROOT + "2026q3_ncen.zip" and "ops@example.org" in req.headers["user-agent"]
    assert sorted(p.name for p in ncen.parquet_dir().iterdir()) == [
        "quarter=2025q4", "quarter=2026q1", "quarter=2026q2", "quarter=2026q3"]
    rows = _read("2026q3", select="series_id, ticker, exchange, cik, accession, filing_date, report_end, is_etf, "
                                  "is_index, is_multi_inverse_index, is_fund_of_fund, is_money_market, "
                                  "daily_avg_net_assets, monthly_avg_net_assets", order="ticker")
    assert rows[0] == ("S000099001", "CCOI", "BATS", "1415726", "0001-26-000012", date(2026, 9, 10),
                       date(2026, 6, 30), True, False, False, True, False, None, 50_000_000.0)
    assert rows[1][:2] == ("S000024908", "SOXL") and rows[1][9] is True          # leveraged / inverse index
    filers = _read("2026q3", "filer.parquet", "cik", order="cik")
    assert filers == [("1064641",), ("1415726",), ("1424958",)]


def test_a_quarter_not_published_yet_falls_back_to_the_one_before(env):
    env.routes.insert(0, ("2026q3_ncen.zip", b"not found", 404, {}))
    env.add("2025q3_ncen.zip", fx.NCEN_Q4)
    row = _run()
    assert row["result"] == "changed"
    assert _asked(env) == ["2026q3_ncen.zip", "2026q2_ncen.zip", "2026q1_ncen.zip", "2025q4_ncen.zip",
                           "2025q3_ncen.zip"]
    assert _read("2026q2", select="count(*)") == [(3,)]
    assert ncen.quarters_held() == 4


def test_a_quarter_held_is_not_asked_again(env):
    _run()
    env.requests.clear()
    row = _run("2026-12-01")
    assert not env.requests and row["result"] == "unchanged" and "4 quarters held" in row["note"]


def test_a_quarter_held_raw_but_never_typed_is_typed_on_the_next_run(env):
    """Review S3: a run that kept the zip and died before its Parquet must not leave the quarter out for good."""
    import shutil
    _run()
    shutil.rmtree(ncen.parquet_dir() / "quarter=2026q2")
    env.requests.clear()
    row = _run("2026-11-03")
    assert not env.requests and row["result"] == "changed", row["note"]
    assert _read("2026q2", select="count(*)") == [(3,)]


def test_the_backfill_fetches_the_last_four_quarters(env):
    row = base.run_request({"dataset": ncen.NAME, "params": {"mode": "backfill", "to": "2026-11-02"}})
    assert row["result"] == "changed", row["note"]
    assert _asked(env) == ["2026q3_ncen.zip", "2026q2_ncen.zip", "2026q1_ncen.zip", "2025q4_ncen.zip"]
    assert ncen.quarters_held() == 4


def test_the_newest_filing_of_a_series_wins(env):
    """XLK files in 2026q2 and 2026q3: the build counts it once, as the July filing."""
    base.run_request({"dataset": ncen.NAME, "params": {"mode": "backfill", "to": "2026-11-02"}})
    row = _run()
    assert row["rows"] == 5                         # staged rows: XLK once, SOXX, SOXL, CCOI and the Acorn fund (not an ETF)
    assert "4 ETF series" in row["note"]


@pytest.mark.parametrize("body", [b"<html>not a zip</html>", "no_fund_table"])
def test_an_answer_that_is_not_the_data_set_fails_and_is_not_served(env, body):
    if body == "no_fund_table":
        import io
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("SUBMISSION.tsv", "ACCESSION_NUMBER\tCIK\n")
        body = buf.getvalue()
    env.routes.insert(0, ("2026q3_ncen.zip", body, 200, {}))
    row = _run()
    assert row["result"] == "failed" and "sec ncen 2026q3" in row["note"]
    assert not (ncen.parquet_dir() / "quarter=2026q3").exists()


def test_it_is_a_monthly_keyless_spine_dataset_that_starts_by_hand():
    from RevenueChaser.rcdata.build import DATASETS
    ds = DATASETS[ncen.NAME]
    assert ds.kind == "spine" and ds.cadence == "monthly" and ds.starts_by_hand and ds.providers == ("sec",)
