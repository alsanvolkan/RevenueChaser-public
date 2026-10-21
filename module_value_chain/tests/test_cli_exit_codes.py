"""Value Chain CLI exit codes: a scheduled run that locked or failed must not read as green (#130)."""
from __future__ import annotations

import pytest

from RevenueChaser.module_value_chain import __main__ as cli


@pytest.mark.parametrize("result,code", [
    ({"requested": 3, "updated": 3, "current": 0, "failed": 0, "edges": 9}, 0),
    ({"requested": 3, "updated": 0, "current": 0, "failed": 0, "locked": True}, 1),
    ({"requested": 3, "updated": 2, "current": 0, "failed": 1, "edges": 9}, 1),
    ({"requested": 0, "updated": 0, "current": 0, "failed": 0, "no_fetch": True}, 0),
    # #299: the submissions sweep counts too; a batch not stored or a failed CIK is not green.
    ({"requested": 3, "updated": 3, "current": 0, "failed": 0,
      "submissions": {"requested": 9, "failed": 0, "headers_absent": 1}}, 0),
    ({"requested": 3, "updated": 3, "current": 0, "failed": 0,
      "submissions": {"requested": 9, "failed": 0, "error": "StoreLockedError: edgar"}}, 1),
    ({"requested": 3, "updated": 3, "current": 0, "failed": 0,
      "submissions": {"requested": 9, "failed": 2, "failed_ciks": ["1", "2"]}}, 1),
])
def test_sync_filings_exit_code(monkeypatch, capsys, result, code):
    monkeypatch.setattr(cli.sync, "sync_filings", lambda **k: result)
    assert cli.main(["--sync-filings"]) == code
    assert str(result["requested"]) in capsys.readouterr().out


def test_lock_is_non_zero_whatever_the_failed_count(monkeypatch):
    monkeypatch.setattr(cli.sync, "sync_filings",
                        lambda **k: {"requested": 1, "failed": 0, "locked": True})
    assert cli.main(["--sync-filings"]) != 0
