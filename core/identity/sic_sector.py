"""SIC code -> sector: the last sector fallback before the type rule (issue #272).

SEC EDGAR gives every filer a four-digit SIC code. SIC is a coarse 1930s list, so it ranks **below** the S&P 1500
list, UW and Yahoo in ``core.identity.gics`` (it agrees with UW on 85.9% of names where both exist: Google, Meta and
Walmart land in the wrong sector), and it only fills a name that has no sector from those. The table is the explicit
263-code table that #265 deleted (``core/sic_gics.py`` at ``570ab1d^``, ``_EXPLICIT``), written in the 11 Morningstar
names that ``core.gics_map.canonical_sector`` maps its GICS sector names to. A code that is not listed (7389, 6500,
8880 ...) is unknown, never a guess: the division-range rules were removed in #86.

``parts`` gives the two raw columns the identity master also stores (``sic`` as four digits and ``sic_group2``, the
two-digit major group). Pure: no store, no network.
"""
from __future__ import annotations

from .types import sic_int

# The listed codes, by the sector they map to. Every key is one of ``core.gics_map.MORNINGSTAR_SECTORS``.
_BY_SECTOR: dict[str, tuple[int, ...]] = {
    "Basic Materials": (
        1021, 1040, 1041, 1090, 1094, 1099, 2810, 2813, 2819, 2821, 2860, 2870, 2890, 2899, 3221, 3229,
        3241, 3270, 3281, 3312, 3317, 3334, 3350, 3411,
    ),
    "Communication Services": (
        2710, 2711, 2721, 2731, 2732, 2741, 4812, 4813, 4832, 4833, 4841, 7310, 7311, 7319, 7812, 7819,
        7822, 7829, 7900, 7922, 7929,
    ),
    "Consumer Cyclical": (
        2200, 2211, 2221, 2300, 2311, 2330, 3021, 3140, 3631, 3634, 3635, 3694, 3711, 3713, 3714, 3715,
        3716, 3751, 3942, 3944, 3949, 4412, 4481, 4482, 5065, 5200, 5211, 5231, 5310, 5311, 5331, 5399,
        5500, 5511, 5521, 5531, 5621, 5651, 5699, 5712, 5722, 5731, 5734, 5812, 5813, 5940, 5944, 5945,
        5990, 7011, 7200, 7948, 7990, 7997, 8200, 8299,
    ),
    "Consumer Defensive": (
        100, 200, 700, 720, 2000, 2011, 2013, 2015, 2020, 2030, 2040, 2050, 2060, 2070, 2080, 2082, 2084,
        2085, 2086, 2090, 2098, 2100, 2111, 2840, 2842, 2844, 5140, 5141, 5411, 5412, 5912,
    ),
    "Energy": (
        1220, 1221, 1222, 1311, 1321, 1381, 1382, 1389, 2911, 4610, 4612, 4613, 4619,
    ),
    "Financial Services": (
        6021, 6022, 6029, 6035, 6036, 6141, 6153, 6159, 6162, 6163, 6199, 6211, 6282, 6311, 6321, 6331,
        6351, 6361, 6411, 6712, 6726, 6770, 6799,
    ),
    "Healthcare": (
        2833, 2834, 2836, 3826, 3841, 3842, 3843, 3844, 3845, 3851, 5047, 5122, 6324, 8011, 8021, 8031,
        8041, 8049, 8051, 8052, 8062, 8063, 8071, 8082, 8090,
    ),
    "Industrials": (
        3721, 3724, 3728, 3761, 3764, 3769, 3795, 4011, 4013, 4210, 4213, 4219, 4512, 4513, 4731, 7361,
        7363, 8711, 8741, 8742, 8744,
    ),
    "Real Estate": (
        6510, 6512, 6513, 6519, 6531, 6532, 6552, 6798,
    ),
    "Technology": (
        3570, 3571, 3572, 3575, 3576, 3577, 3578, 3579, 3661, 3663, 3669, 3670, 3671, 3672, 3674, 3675,
        3676, 3677, 3678, 3679, 3827, 3829, 3861, 7370, 7371, 7372, 7373, 7374, 7375, 7376, 7377, 7378, 7379,
    ),
    "Utilities": (
        4911, 4922, 4923, 4924, 4931, 4939, 4941, 4991,
    ),
}

SIC_SECTOR: dict[int, str] = {code: sector for sector, codes in _BY_SECTOR.items() for code in codes}


def code_of(value) -> int | None:
    """The SIC code as an int from a 4-digit string, an int or a whole float; ``None`` for a blank, a malformed or an
    out-of-range value (a real code is 1..9999)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not value.is_integer():
            return None
        value = int(value)
    code = sic_int(value)
    return code if code is not None and 0 < code <= 9999 else None


SUBMISSIONS = "sec_submissions"      # the SEC submissions cache row (``inputs["sec"]``)
STORE = "sec_company_sic"            # the store's SIC table (``inputs["sic_store"]``), the fallback


def code_from(inputs: dict) -> tuple[int, str] | None:
    """``(code, source)`` of a ticker's SIC from its master inputs: the SEC submissions row when it has a code, else
    the store's ``sec_company_sic`` code (never a mix: a code the table does not list does not fall through to the
    other one), else ``None``. ``source`` is :data:`SUBMISSIONS` or :data:`STORE`."""
    code = code_of((inputs.get("sec") or {}).get("sic"))
    if code is not None:
        return code, SUBMISSIONS
    code = code_of(inputs.get("sic_store"))
    return (code, STORE) if code is not None else None


def sector_for(sic) -> str | None:
    """The Morningstar sector of a SIC code, or ``None`` when the code is blank, malformed or not in the table."""
    code = code_of(sic)
    return SIC_SECTOR.get(code) if code is not None else None


def parts(sic) -> tuple[str, str] | None:
    """``(four-digit code, two-digit major group)`` of a SIC code, zero-padded (``100`` -> ``("0100", "01")``), or
    ``None`` when there is no valid code."""
    code = code_of(sic)
    return (f"{code:04d}", f"{code // 100:02d}") if code is not None else None
