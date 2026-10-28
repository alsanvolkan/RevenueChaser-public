"""``yfinance/estimates`` (build plan PR 12): analyst estimates per Tier A company, asked weekly, one request each
(``core/providers/yfinance.get_earnings_trend``, the yfinance library faked at its ``Ticker``, never Yahoo); the
newest snapshot served as ``estimate_latest`` and the old ``estimate_snapshots`` shape."""
from __future__ import annotations

from datetime import date

import pytest

from RevenueChaser.rcdata import ops
from RevenueChaser.rcdata.build.fundamentals import estimates

from . import fund_fixture as fx


def _blocks(eps_avg=1.5, nan_avg=False) -> dict:
    """The four blocks as yfinance's ``get_*(as_dict=True)`` gives them: ``{field: {period: value}}``."""
    periods = ["0q", "+1q", "0y", "+1y"]

    def block(**cols):
        return {k: dict(zip(periods, v)) for k, v in cols.items()}
    return {
        "earnings_estimate": block(numberOfAnalysts=[30, 28, 40, 38],
                                   avg=[float("nan") if nan_avg else eps_avg, 1.6, 6.5, 7.2],
                                   low=[1.4, 1.5, 6.1, 6.6], high=[1.7, 1.8, 7.0, 8.0], currency=["USD"] * 4),
        "revenue_estimate": block(numberOfAnalysts=[25, 24, 35, 33], avg=[9.1e10, 9.5e10, 3.9e11, 4.2e11]),
        "eps_trend": block(current=[1.5, 1.6, 6.5, 7.2], **{"7daysAgo": [1.5] * 4}),
        "eps_revisions": block(upLast7days=[2, 1, 3, 2], downLast7Days=[0, 0, 1, 0]),
    }


@pytest.fixture
def yf(monkeypatch):
    import yfinance
    asked: list[str] = []
    answers: dict[str, object] = {}

    class Ticker:
        def __init__(self, ticker):
            asked.append(ticker)
            self.answer = answers.get(ticker, _blocks())

        def _get(self, name):
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer.get(name, {})

        def get_earnings_estimate(self, as_dict=False):
            return self._get("earnings_estimate")

        def get_revenue_estimate(self, as_dict=False):
            return self._get("revenue_estimate")

        def get_eps_trend(self, as_dict=False):
            return self._get("eps_trend")

        def get_eps_revisions(self, as_dict=False):
            return self._get("eps_revisions")

    monkeypatch.setattr(yfinance, "Ticker", Ticker)
    fx.spine()
    return asked, answers


def test_one_request_per_company_and_the_newest_snapshot_served(yf):
    asked, answers = yf
    answers["MSFT"] = _blocks(nan_avg=True)
    check = fx.run(estimates.NAME, "2026-10-28")
    assert check["result"] == "changed", check["note"]
    assert asked == ["HEI", "MSFT", "ORCL", "TM"]                 # one per company: HEI, not HEI-A
    assert ops.calls_today("yfinance") == 4
    assert fx.q("SELECT period, eps_est_avg, eps_est_analysts, rev_est_avg, eps_rev_down_7d, source_as_of "
                "FROM estimate_latest WHERE ticker = 'MSFT' ORDER BY period") == [
        ("+1q", 1.6, 28.0, 9.5e10, 0.0, date(2026, 10, 28)), ("+1y", 7.2, 38.0, 4.2e11, 0.0, date(2026, 10, 28)),
        ("0q", None, 30.0, 9.1e10, 0.0, date(2026, 10, 28)),               # Yahoo's NaN is NULL, never 0
        ("0y", 6.5, 40.0, 3.9e11, 1.0, date(2026, 10, 28))]
    assert fx.q("SELECT cik FROM estimate_latest WHERE ticker = 'HEI' GROUP BY cik") == [("46619",)]
    assert fx.q("SELECT ticker, session_date, period, eps_est_avg, source FROM estimate_snapshots "
                "WHERE ticker = 'ORCL' AND period = '0q'") == [("ORCL", date(2026, 10, 28), "0q", 1.5, "yfinance")]
    assert "4 of 4 Tier A companies have estimates" in check["note"]


def test_asked_again_after_a_week_and_the_newer_snapshot_wins(yf):
    asked, answers = yf
    fx.run(estimates.NAME, "2026-10-28")
    fx.run(estimates.NAME, "2026-10-30")
    assert len(asked) == 4                                        # inside the week: nothing asked
    answers["MSFT"] = _blocks(eps_avg=1.55)
    fx.run(estimates.NAME, "2026-11-04")
    assert len(asked) == 8
    assert fx.q("SELECT eps_est_avg, source_as_of FROM estimate_latest WHERE ticker = 'MSFT' AND period = '0q'") == [
        (1.55, date(2026, 11, 4))]


def test_failures_in_a_row_stop_the_run(yf, monkeypatch):
    asked, answers = yf
    monkeypatch.setattr(estimates, "FAIL_STREAK", 2)
    answers["HEI"] = answers["MSFT"] = RuntimeError("429 Too Many Requests")
    check = fx.run(estimates.NAME, "2026-10-28")
    assert check["result"] == "failed" and asked == ["HEI", "MSFT"]
    answers.clear()
    answers["MSFT"] = RuntimeError("429 Too Many Requests")
    check = fx.run(estimates.NAME, "2026-10-29")
    assert check["result"] == "changed" and "1 failed" in check["note"]
    assert fx.q("SELECT DISTINCT ticker FROM estimate_latest ORDER BY 1") == [("HEI",), ("ORCL",), ("TM",)]
