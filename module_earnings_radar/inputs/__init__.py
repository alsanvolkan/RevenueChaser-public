"""RC-Earnings-Inputs (E0, #372): the Earnings board's nightly inputs step.

``sweeps()`` is the step ``earnings`` of ``core/nightly_inputs.py`` (``STEPS``): independent sweeps that store
tonight's inputs under the run's session, inside the night's call caps. Each module is one sweep and one table.
"""
from __future__ import annotations


def sweeps():
    from RevenueChaser.module_earnings_radar.inputs import (
        cftc_cot, earnings_calendar, economic, estimates, macro, short_interest, vol_index)

    return (earnings_calendar.SWEEP, economic.SWEEP, macro.SWEEP, short_interest.SWEEP, estimates.SWEEP,
            vol_index.SWEEP, cftc_cot.SWEEP)
