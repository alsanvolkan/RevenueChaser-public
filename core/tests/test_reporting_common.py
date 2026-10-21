"""Shared dashboard chrome — the help box and the shared job poller.

The universal filter kit lives in ``core/static/rc_filters.{js,css}`` (``FILTERS_JS`` /
``FILTERS_CSS``) and is unit-tested in ``core/tests/test_rc_filters.py``. The old per-board
constants kit was deleted in #383 C1d.
"""
from __future__ import annotations

from RevenueChaser.core import reporting_common


def test_help_html_carries_the_body_and_a_summary():
    out = reporting_common.help_html("<b>hello</b>")
    assert "<b>hello</b>" in out
    assert 'class="help"' in out
    assert "<summary>" in out


def test_help_css_styles_the_help_class():
    assert ".help" in reporting_common.HELP_CSS


def test_help_helpers_are_exported():
    assert "help_html" in reporting_common.__all__
    assert "HELP_CSS" in reporting_common.__all__


def test_job_js_is_the_one_shared_poller():
    rc = reporting_common
    assert "JOB_JS" in rc.__all__
    for fn in ("settingsJobVerdict", "settingsPoll", "boardSettingsReset"):
        assert f"function {fn}(" in rc.JOB_JS, fn
