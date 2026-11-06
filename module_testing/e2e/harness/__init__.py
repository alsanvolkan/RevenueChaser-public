"""Machinery for the end-to-end tier — no test assertions live here."""
from __future__ import annotations

import os

#: How long a headless-Chrome ``--dump-dom`` run may take before the test calls it a hang.
#:
#: The tier runs ``-n 4`` and every test boots its own dashboard, so four Chromes can start at once on a hosted
#: four-core runner. The old per-file literals (90 s, 120 s) were written for one Chrome at a time and timed out
#: on 5 Nov 2026 in four unrelated tests of one run (board_html_browser, board_earnings_radar) while the diff
#: touched neither. A real hang still fails: the job has its own limit, and this is minutes, not hours.
#: ``RC_E2E_CHROME_TIMEOUT`` overrides it without a code change.
def chrome_timeout_s(default: float = 240.0) -> float:
    try:
        value = float(os.environ.get("RC_E2E_CHROME_TIMEOUT", "").strip())
    except ValueError:
        return default
    return value if value > 0 else default


CHROME_TIMEOUT_S = chrome_timeout_s()
