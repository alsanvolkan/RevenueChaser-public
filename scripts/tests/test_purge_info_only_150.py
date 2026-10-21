"""#150 rollback helper: ``purge_info_only`` removes flagged rows and nothing else. Temp store, no network."""
from __future__ import annotations

import time

import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.core.providers import cache
from RevenueChaser.scripts import purge_info_only as pio


def _seed():
    cache.save("FULL", {"info": {"a": 1}, "annual": [{"fiscal_year": "2023"}]})
    cache.save("INFO", {"info": {"a": 1}, "annual": [], "info_only": True})
    cache.save("INFO2", {"info": {"a": 2}, "annual": [], "info_only": True})
    cache.save("PLAIN", {"info": {"a": 3}, "annual": []})                     # info but no flag: not ours
    cache.save("MERGED", {"info": {"a": 1}, "annual": [{"fiscal_year": "2023"}]})
    cache.save("MERGED", {"info": {"a": 9}, "annual": [], "info_only": True})  # merged into the full row


def _tickers():
    return sorted(r["ticker"] for r in rawstore.query("SELECT ticker FROM ticker_payload_cache"))


def test_the_default_run_only_counts(rawstore_tmp_db, capsys):
    _seed()
    assert pio.main([]) == 0
    assert "2 info_only row(s)" in capsys.readouterr().out
    assert _tickers() == ["FULL", "INFO", "INFO2", "MERGED", "PLAIN"]


def test_apply_deletes_only_the_flagged_rows(rawstore_tmp_db, capsys):
    _seed()
    assert pio.main(["--apply"]) == 0
    assert "deleted 2 info_only row(s); 3 row(s) left" in capsys.readouterr().out
    assert _tickers() == ["FULL", "MERGED", "PLAIN"]
    assert rawstore.get("ticker_payload_cache", "MERGED")["payload"]["info"] == {"a": 9}   # merged row untouched


def test_a_second_apply_deletes_nothing(rawstore_tmp_db, capsys):
    _seed()
    pio.purge()
    assert pio.purge() == 0


def test_it_refuses_while_another_process_uses_the_store(rawstore_tmp_db, monkeypatch, capsys):
    _seed()
    monkeypatch.setattr(rawstore, "_peer_pids", lambda path: [4242])
    assert pio.main(["--apply"]) == 1
    assert "stop them first" in capsys.readouterr().err
    assert len(_tickers()) == 5
