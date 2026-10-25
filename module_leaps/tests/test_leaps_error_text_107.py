"""#107: an exception's text never reaches the board notice or the result history, only its class name.

A store error of the kind these branches exist for names the file it could not open, an absolute path on the
machine that ran the job. The full text stays in the log.
"""
from __future__ import annotations

import logging

from RevenueChaser.module_leaps import engine, history as leaps_history
from RevenueChaser.module_leaps.tests.test_leaps_history_377 import _UW, _scan, store  # noqa: F401 - the fixture

PATH = "/home/someone/RevenueChaser/.store/history.duckdb"


class StoreLocked(Exception):
    pass


def _boom(*a, **k):
    raise StoreLocked(f'IO Error: Could not set lock on file "{PATH}"')


def test_a_baselines_read_error_names_its_class_and_not_its_path(store, monkeypatch, caplog):
    monkeypatch.setattr(leaps_history, "baselines", _boom)
    warnings: list = []
    with caplog.at_level(logging.WARNING):
        _scan(_UW({"AAA": (1000, 200)}), warnings=warnings)
    (note,) = [w for w in warnings if "baselines" in w]
    assert "StoreLocked" in note and PATH not in note
    assert PATH in caplog.text, "the full text belongs in the log"


def test_a_failed_first_print_page_names_its_class_and_not_its_path(store):
    class _Tape(_UW):
        def option_trades(self, **kw):
            _boom()

    warnings: list = []
    _scan(_Tape({"AAA": (1000, 200)}), warnings=warnings)
    (note,) = [w for w in warnings if "first page" in w]
    assert "StoreLocked" in note and PATH not in note


def test_a_failed_read_and_a_failed_fetch_record_only_the_class(store, monkeypatch):
    class _Reads(_UW):
        def oi_per_expiry(self, ticker, date=None):
            if ticker == "BBB":
                _boom()
            return super().oi_per_expiry(ticker, date)

    footprints, _, _ = _scan(_Reads({"AAA": (1000, 200)}))
    (bbb,) = [f for f in footprints if f["ticker"] == "BBB"]
    assert bbb["inputs"]["error"] == "StoreLocked"

    real = engine.fetch_and_classify

    def raising(ticker, uw, **kw):
        if ticker == "BBB":
            _boom()
        return real(ticker, uw, **kw)

    monkeypatch.setattr(engine, "fetch_and_classify", raising)
    footprints, _, _ = _scan(_UW({"AAA": (1000, 200)}))
    (bbb,) = [f for f in footprints if f["ticker"] == "BBB"]
    assert bbb["fetch_status"] == "error: StoreLocked"
