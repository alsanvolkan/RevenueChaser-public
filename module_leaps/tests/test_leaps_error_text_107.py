"""#107: an exception's text never reaches the board notice or the result history, only its class name.

A store error of the kind these branches exist for names the file it could not open, an absolute path on the
machine that ran the job. The full text stays in the log.
"""
from __future__ import annotations

import logging

from RevenueChaser.module_leaps import history as leaps_history
from RevenueChaser.module_leaps.tests.test_leaps_history_377 import _scan, store  # noqa: F401 - the fixture

PATH = "/home/someone/RevenueChaser/.store/history.duckdb"


class StoreLocked(Exception):
    pass


def _boom(*a, **k):
    raise StoreLocked(f'IO Error: Could not set lock on file "{PATH}"')


def test_a_baselines_read_error_names_its_class_and_not_its_path(store, monkeypatch, caplog):
    monkeypatch.setattr(leaps_history, "baselines", _boom)
    warnings: list = []
    with caplog.at_level(logging.WARNING):
        _scan({"AAA": (1000, 200)}, warnings=warnings)
    (note,) = [w for w in warnings if "baselines" in w]
    assert "StoreLocked" in note and PATH not in note
    assert PATH in caplog.text, "the full text belongs in the log"


def test_a_failed_report_date_read_names_its_class_and_not_its_path(store, monkeypatch, caplog):
    """G9: the near-event dates are a store read (``option_screener_daily``); its error is a class on the board."""
    from RevenueChaser.rcdata import read
    monkeypatch.setattr(read, "option_screener", _boom)
    warnings: list = []
    with caplog.at_level(logging.WARNING):
        _scan({"AAA": (1000, 200)}, warnings=warnings)
    (note,) = [w for w in warnings if "earnings calendar" in w]
    assert "StoreLocked" in note and PATH not in note
    assert PATH in caplog.text, "the full text belongs in the log"
