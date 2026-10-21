"""The identity CLI: ``--refresh`` runs one refresh and prints the summary line."""
from __future__ import annotations

from RevenueChaser.core.identity import __main__ as cli
from RevenueChaser.core.identity import refresh as R


def _rec(status="ok"):
    return {"run_id": "r1", "status": status, "error": None,
            "counts": {"tickers": 3, "by_type": {}, "by_sector_source": {}, "unclassified": 0, "conflicts": 0},
            "sources": {s: {"selected": True, "status": "ok", "rows": 1, "fetched": 0, "seconds": 0.1, "error": None}
                        for s in R.SOURCES}}


def test_refresh_passes_options_and_prints_the_summary(monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr(R, "refresh", lambda **kw: seen.update(kw) or _rec())
    assert cli.main(["--refresh", "--limit", "3", "--sources", "nasdaq, uw", "--yahoo-limit", "7"]) == 0
    assert seen["limit"] == 3 and seen["sources"] == ["nasdaq", "uw"] and seen["yahoo_limit"] == 7
    out = capsys.readouterr().out
    assert "3 tickers" in out and "status=ok" in out


def test_without_refresh_it_only_prints_help(monkeypatch, capsys):
    monkeypatch.setattr(R, "refresh", lambda **kw: (_ for _ in ()).throw(AssertionError("must not run")))
    assert cli.main([]) == 2


def test_an_error_run_exits_nonzero(monkeypatch):
    monkeypatch.setattr(R, "refresh", lambda **kw: _rec("error"))
    assert cli.main(["--refresh"]) == 1
