"""The Earnings board's inputs steps (E0, #372; split per schedule in #620).

``sweeps()`` is the step ``earnings`` of ``core/nightly_inputs.py`` (``STEPS``), all seven sweeps for a manual run.
The scheduled entries each run one smaller step (#620): ``calendar_sweeps`` (RC-Earnings-Calendar),
``short_interest_sweeps`` (RC-Short-Interest), ``daytime_sweeps`` (RC-Earnings-Daytime), ``macro_sweeps``
(RC-Macro-Weekly) and ``cftc_sweeps`` (RC-CFTC-Weekly). Each sweep stores its inputs under the run's session, inside
the run's call caps; each module is one sweep and one table.
"""
from __future__ import annotations


def sweeps():
    from RevenueChaser.module_earnings_radar.inputs import (
        cftc_cot, earnings_calendar, economic, estimates, macro, short_interest, vol_index)

    return (earnings_calendar.SWEEP, economic.SWEEP, macro.SWEEP, short_interest.SWEEP, estimates.SWEEP,
            vol_index.SWEEP, cftc_cot.SWEEP)


def calendar_sweeps():
    from RevenueChaser.module_earnings_radar.inputs import earnings_calendar
    return (earnings_calendar.SWEEP,)


def short_interest_sweeps():
    from RevenueChaser.module_earnings_radar.inputs import short_interest
    return (short_interest.SWEEP,)


def daytime_sweeps():
    from RevenueChaser.module_earnings_radar.inputs import economic, estimates
    return (economic.SWEEP, estimates.SWEEP)


def macro_sweeps():
    from RevenueChaser.module_earnings_radar.inputs import macro, vol_index
    return (macro.SWEEP, vol_index.SWEEP)


def cftc_sweeps():
    from RevenueChaser.module_earnings_radar.inputs import cftc_cot
    return (cftc_cot.SWEEP,)
