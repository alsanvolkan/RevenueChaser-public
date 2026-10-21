"""#108 S0 (#388, CL-A): the Value Chain CLI takes a floor (USD millions) or a ticker list.

Without either, ``--sync-filings`` and ``--fetch`` select exactly today's names (``tickers=None``: the $300M
classification universe, and for the sync the submissions sweep of every universe CIK, #299). Offline.
"""
from __future__ import annotations

import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.module_value_chain import __main__ as cli


@pytest.fixture
def spies(rawstore_tmp_db, monkeypatch):
    rawstore.write_rows("equity_universe", [
        {"ticker": t, "name": t, "market_cap": cap} for t, cap in (("BIG", 1e10), ("MID", 1e8), ("TINY", 1e7))])
    calls = {"sync": [], "fetch": []}
    monkeypatch.setattr(cli.sync, "sync_filings",
                        lambda **k: calls["sync"].append(k) or {"requested": 0, "failed": 0})
    monkeypatch.setattr(cli.engine, "fetch_corpus", lambda **k: calls["fetch"].append(k) or {})
    return calls


@pytest.mark.parametrize("flag,key", [("--sync-filings", "sync"), ("--fetch", "fetch")])
def test_no_new_argument_selects_exactly_today(spies, flag, key):
    assert cli.main([flag]) == 0
    assert spies[key][-1].get("tickers") is None


@pytest.mark.parametrize("flag,key", [("--sync-filings", "sync"), ("--fetch", "fetch")])
def test_floor_50_adds_the_50_to_300m_names(spies, flag, key):
    assert cli.main([flag, "--floor", "50"]) == 0
    assert spies[key][-1]["tickers"] == ["BIG", "MID"]


def test_tickers_pick_the_names(spies):
    assert cli.main(["--sync-filings", "--tickers", "mid,big"]) == 0
    assert spies["sync"][-1]["tickers"] == ["MID", "BIG"]


@pytest.mark.parametrize("argv", [["--sync-filings", "--floor", "0"],
                                  ["--sync-filings", "--floor", "50", "--tickers", "BIG"]])
def test_a_bad_selection_is_refused(spies, argv):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2 and spies["sync"] == []


@pytest.mark.parametrize("argv", [["--sync-filings", "--tickers", " , "], ["--sync-filings", "--floor", "1e9"],
                                  ["--fetch", "--floor", "1e9"]])
def test_an_empty_selection_is_refused_not_widened(spies, argv):
    """``sync_filings(tickers=[])`` reads an empty list as no selection, i.e. the whole universe: refuse it."""
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2 and spies["sync"] == [] and spies["fetch"] == []


def test_the_help_warns_about_sec_requests(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    assert "Makes SEC requests for these names" in " ".join(capsys.readouterr().out.split())
