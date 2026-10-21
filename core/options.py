"""Option-contract helpers shared across modules.

The OCC option symbol encodes root + expiry + right + strike in one string
(``SPXW260814C07800000`` -> root ``SPXW``, 2026-08-14, call, strike 7800.0).
Both the SPX Gamma board and the Implied Probability board parse the same UW
``option_symbol`` field, so the parser lives here once rather than in either
module.
"""
from __future__ import annotations

import re
from datetime import date as _date

# OCC symbol: letters-only root, 6-digit YYMMDD expiry, C/P right, 8-digit
# strike in thousandths (3 implied decimals).
_SYM_RE = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")


def parse_option_symbol(symbol: str) -> dict | None:
    """Split a UW option symbol into root/expiry/type/strike; None when unparsable."""
    m = _SYM_RE.match(symbol or "")
    if not m:
        return None
    root, ymd, cp, strike_digits = m.groups()
    year = 2000 + int(ymd[:2])
    month = int(ymd[2:4])
    day = int(ymd[4:6])
    try:
        _date(year, month, day)
    except ValueError:
        return None
    return {
        "root": root,
        "expiry": f"{year:04d}-{month:02d}-{day:02d}",
        "type": cp,
        "strike": int(strike_digits) / 1000.0,
    }
