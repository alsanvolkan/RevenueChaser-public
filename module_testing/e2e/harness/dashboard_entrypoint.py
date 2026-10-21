"""Boot the real dashboard for an end-to-end run.

    python -m RevenueChaser.module_testing.e2e.harness.dashboard_entrypoint <port>

Four things must happen here, in this order, which is the whole reason this file
exists rather than the test importing the app directly:

1. The yfinance fake is installed **before** any RevenueChaser import, because
   ``module_spx_gamma/candles.py`` and ``vix.py`` do ``import yfinance as yf`` at
   module scope and would otherwise capture the real class.
2. ``RC_STORE_DIR`` / ``RC_CACHE_DIR`` are already in the environment (set by the
   harness), so ``core/config.py`` resolves the throwaway roots on first import.
3. The Task Scheduler is replaced by a fake, so no e2e run can reach the real one (#130).
4. ``RC_E2E_NOW`` (an ISO time with a UTC offset), when set, replaces the session clock
   ``core.calendar._now``. The dashboard is a subprocess, so a monkeypatch in the test process
   cannot reach it; the forecast-ledger freeze (#128) decides on "the latest complete NY session".

Keeping this out of the app means no test machinery ships in the production
import path.
"""
from __future__ import annotations

import os
import sys


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765

    # 1. fakes first — before RevenueChaser pulls yfinance in transitively
    from RevenueChaser.module_testing.e2e.harness import yfinance_fake
    yfinance_fake.install()

    # 2. sanity: refuse to run against the developer's real data
    for var in ("RC_STORE_DIR", "RC_CACHE_DIR"):
        if not os.environ.get(var):
            raise SystemExit(
                f"{var} is not set — refusing to start an e2e dashboard against "
                f"the real .store/.cache. The harness must set both."
            )

    # 3. never reach the real Task Scheduler, even on a Windows developer machine (#130)
    from RevenueChaser.core import automations
    automations.set_scheduler(automations.FakeScheduler())

    # 4. test clock (#128): the ledger's session rule must see a fixed "now" inside this subprocess
    fixed_now = os.environ.get("RC_E2E_NOW")
    if fixed_now:
        from datetime import datetime
        from RevenueChaser.core import calendar
        fixed = datetime.fromisoformat(fixed_now.replace("Z", "+00:00"))
        if fixed.tzinfo is None:
            raise SystemExit("RC_E2E_NOW must carry a UTC offset, e.g. 2026-09-25T21:00:00+00:00")
        calendar._now = lambda: fixed
        print(f"[e2e] clock fixed at {fixed.isoformat()}", flush=True)

    from RevenueChaser.app import dashboard
    from RevenueChaser.core.config import DATA_DIR, OUTPUT_DIR
    print(f"[e2e] store={DATA_DIR} cache={OUTPUT_DIR} port={port}", flush=True)
    dashboard.app.run(host="127.0.0.1", port=port, debug=False,
                      use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
