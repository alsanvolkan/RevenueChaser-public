"""One Chrome timeout for the whole tier (``harness.CHROME_TIMEOUT_S``).

The tier runs ``-n 4`` and every test boots its own dashboard, so four headless Chromes can start at once on a
hosted four-core runner. Six files each carried their own literal (90 s, 120 s), written for one Chrome at a time;
on 5 Nov 2026 one run timed out in four unrelated tests while the diff touched neither. The value lives in one
place now and ``RC_E2E_CHROME_TIMEOUT`` raises it without a code change.
"""
from __future__ import annotations

from . import harness


def test_the_default_is_minutes_not_ninety_seconds():
    assert harness.chrome_timeout_s() >= 180, "four parallel Chromes on a hosted runner need minutes"


def test_the_environment_raises_it(monkeypatch):
    monkeypatch.setenv("RC_E2E_CHROME_TIMEOUT", "600")
    assert harness.chrome_timeout_s() == 600.0


def test_a_value_that_is_not_a_positive_number_keeps_the_default(monkeypatch):
    for bad in ("", "   ", "soon", "0", "-5"):
        monkeypatch.setenv("RC_E2E_CHROME_TIMEOUT", bad)
        assert harness.chrome_timeout_s(default=240.0) == 240.0, bad
