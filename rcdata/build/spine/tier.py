"""Tier rule v2: which SEC-file names the boards screen (Tier A) and why the others are not.

The rule is the one ``equity_universe`` and ``core/universe.py`` already apply, written once for the spine:

- membership is the Nasdaq / NYSE / Cboe rows of the SEC file (#264, ``docs/data_health.md`` 4.1: over-the-counter
  rows and rows with no exchange are out);
- the security type (Identity R4, #87): ``core.identity.types.EXCLUDED_TYPES`` (funds and ETFs, SPACs, preferreds,
  warrants, units) leave the universe; an untyped or unknown type stays (an untyped name is never hidden).

So:

- **A**: listed on Nasdaq, NYSE or Cboe, and a type that is not excluded (or no type yet);
- **B**: listed, but an excluded type (``EXCLUDED_TYPES``);
- **C**: over the counter (``OTC``);
- **D**: no exchange in the SEC file (or one outside these).

Tier A is therefore not every listed name: the plan's "A 7,691 exchange-listed" (2026-10-26) is A and B together.

The market-cap floor (gone since 29 Oct 2026) was never a tier: it needs market data, which the spine does not hold (``core/universe.py``
applies it at read time). The type comes from ``core.identity.types.classify_type`` over the spine's own inputs
(OpenFIGI's security types and the SEC name; no SEC submissions), so a preferred share written ``WFC-PC`` is typed by
the ticker rule even before OpenFIGI answered. A name those leave untyped is typed, in this order, by:

1. the ticker (v2): a warrant, unit or right is written ``-WT`` / ``-UN`` / ``-RI`` (NYSE) or with a fifth letter
   W / U / R (Nasdaq) in the SEC file;
2. Compustat's company SIC: 6770 ("blank checks") is a SPAC, over a common-like or untyped name only (#240). The
   identity master types a SPAC from the same code, but from the SEC submissions summary, which the spine does not
   fetch;
3. Compustat's ``tpci`` of the linked security (``TPCI_TYPES``);
4. the SEC name (v2): an ETF or ETN, a commodity, currency or crypto pool, a pool sponsor's trust, or a bank that
   issues ETNs (``ETP_NAMES``);
5. the venue (v2): an untyped Cboe listing is an exchange-traded product.

Rules 4 and 5 never type the issuers' and Cboe's own listed shares (``OWN_SHARES``: JPM, DB, BMO, CBOE).

Rule 2 is the only one that reads a column the spine asked for after its first snapshot: until the next
attended ``wrds/sec_idhist`` backfill the Parquet has no ``sic``, staging reads it as NULL, and the rule types
nothing (#240).

v2 (2026-10-28): the first live spine left 480 Tier A names with no Compustat link, and so no CUSIP and no OpenFIGI
answer, mostly ETFs, ETNs, commodity trusts, warrants, units and rights. Rules 1, 3 and 4 type 288 of them, and every
one of those Nasdaq Trader's symbol files also list (2026-10-28) is the same kind in its security name there.
"""
from __future__ import annotations

import re

from ....core.identity.types import _COMMON_LIKE, EXCLUDED_TYPES, classify_type, sic_int

VERSION = 2
LISTED = ("Nasdaq", "NYSE", "CBOE")
OTC = "OTC"
TIERS = ("A", "B", "C", "D")
WORDS = {"A": "listed, operating company or untyped", "B": "listed, excluded security type",
         "C": "over the counter", "D": "no exchange"}


def listed(exchange: str | None) -> bool:
    return (exchange or "").strip().upper() in {e.upper() for e in LISTED}


def tier(exchange: str | None, security_type: str | None) -> str:
    """The tier of one SEC-file row from its exchange and its security type (``None`` = not typed yet)."""
    ex = (exchange or "").strip().upper()
    if listed(ex):
        return "B" if security_type in EXCLUDED_TYPES else "A"
    if ex == OTC:
        return "C"
    return "D"


# Compustat's issue type code (``comp.security.tpci``) of the linked security, used only when OpenFIGI and the name
# rules leave a name untyped (review should-fix 6). Only the codes read with confidence are mapped; not verified
# against WRDS from here, so an unexpected code types nothing.
TPCI_TYPES = {"0": "common", "F": "adr", "%": "etp"}
UNTYPED = ("unknown", "other", None)

# 2a. Compustat's company SIC (``comp.company.sic``). 6770 is "blank checks": the identity master already types a
# SPAC from it (``core.identity.types.spac_signals``), but it reads the SEC submissions summary, which the spine does
# not fetch (``security_type`` passes ``"sec": None``), so that rule never fires in a spine build. The name rule
# ``SPAC_NAME`` matches none of "Churchill Capital Corp XI", "Gores Holdings X" or "Cantor Equity Partners" (#240),
# so Compustat's SIC is the spine's only SPAC source. The gate is the master's own ``_COMMON_LIKE`` (shared, so the
# two cannot drift) widened by ``UNTYPED``: the SIC beats a common-like or untyped name, and never an ETP, a
# preferred or a type a stronger source gave.
SIC_SPAC = 6770
SIC_SOURCE = f"compustat_sic({SIC_SPAC})"
SIC_GATE = frozenset(_COMMON_LIKE) | frozenset(UNTYPED)
# Compustat's ``sic`` arrives as text, and through the worker's pandas frame a whole number can arrive as
# ``6770.0``; the master's ``sic_int`` reads neither that nor a leading zero away, so the shape is read here.
_SIC_FLOAT = re.compile(r"^(\d+)\.0+$")


def sic_code(v) -> int | None:
    """Compustat's SIC as a number, whichever text shape it arrives in; ``None`` when it is not one."""
    s = str(v).strip() if v is not None else ""
    m = _SIC_FLOAT.match(s)
    return sic_int(m.group(1) if m else s)

# 1. The ticker. The SEC file writes an NYSE warrant ``AMPX-WT`` (a series ``NE-WTA``), a unit ``BCSS-UN`` and a right
# ``CELG-RI``. A Nasdaq one is a fifth letter: of Nasdaq Trader's five-letter symbols (nasdaqlisted.txt, 2026-10-28) all
# 358 ending W are warrants (PSNYW's name says "ADS (ADW)", a depositary warrant; OpenFIGI types it first), 247 of 247
# ending U units and 104 of 104 ending R rights; other fifth letters mix notes, preferreds and share classes (GOOGL,
# FWONK), so they type nothing. A right (a SPAC's right to a fraction of a share, a contingent value right) is typed
# warrant: a claim on shares, not a share, with no type of its own in ``EXCLUDED_TYPES``; the source says right.
TICKER_SUFFIXES = ((re.compile(r"-WT[A-Z]?$"), "warrant", "-WT"), (re.compile(r"-UN$"), "unit", "-UN"),
                   (re.compile(r"-RI$"), "warrant", "-RI: right"))
NASDAQ_FIFTH = {"W": ("warrant", "W"), "U": ("unit", "U"), "R": ("warrant", "R: right")}

# 3. The SEC name of a product: the fund or trust itself, or for an ETN the bank that issued the note (the SEC file
# lists BMO's MicroSectors ETNs under BANK OF MONTREAL /CAN/). A royalty trust is an operating type, whatever the
# commodity in its name. Measured on the 5,800 operating-company symbols of Nasdaq Trader's files (2026-10-28): these
# patterns match none but BMO itself (typed by OpenFIGI first) and a misspelt royalty trust.
_ASSETS = (r"gold|silver|platinum|palladium|precious metals?|base metals|commodit(?:y|ies)|oil|natural gas|gasoline|"
           r"agricultur\w*|bitcoin|ethereum|ether|solana|xrp|litecoin|dogecoin|crypto|currencyshares|dollar index|"
           r"carbon allowance")
ETP_NAMES = (
    (re.compile(r"\bET[FN]s?\b|\bexchange[- ]traded\b", re.I), "ETF/ETN"),
    (re.compile(rf"\b(?:{_ASSETS})\b.*\b(?:trust|fund)\b|\b(?:trust|fund)\b.*\b(?:{_ASSETS})\b", re.I),
     "commodity, currency or crypto pool"),
    (re.compile(r"\bproshares trust\b|\binvesco db\b|^vs trust$", re.I), "pool sponsor"),
    (re.compile(r"^(?:bank of montreal /can/|barclays bank plc|ubs ag|credit suisse ag|"
                r"deutsche bank aktiengesellschaft|jpmorgan chase & co)$", re.I), "ETN issuer"),
)
ROYALTY = re.compile(r"\broyalty\b", re.I)

# 4. The venue: Cboe lists products. Of its 1,679 symbols in Nasdaq Trader's otherlisted.txt (2026-10-28), 1,673 are
# ETFs, three ETNs (ATMP, VXX, VXZ), two test issues and one a company (Cboe Global Markets, typed by OpenFIGI first).
PRODUCT_VENUES = ("CBOE",)
# The listed shares of an ETN issuer and of Cboe itself: OpenFIGI and ``tpci`` type them first in a normal build, and
# with neither (an empty WRDS source, a failed link) they stay untyped in Tier A, never one of their products.
OWN_SHARES = frozenset({"JPM", "DB", "BMO", "CBOE"})


def ticker_type(ticker: str, exchange: str | None) -> tuple[str, str] | None:
    """``(type, source)`` a warrant's, unit's or right's ticker says, else ``None``."""
    t = (ticker or "").strip().upper()
    for pattern, typ, label in TICKER_SUFFIXES:
        if pattern.search(t):
            return typ, f"ticker_rule({label})"
    if (exchange or "").strip().upper() == "NASDAQ" and len(t) == 5 and t.isalpha() and t[-1] in NASDAQ_FIFTH:
        typ, label = NASDAQ_FIFTH[t[-1]]
        return typ, f"ticker_rule(Nasdaq fifth letter {label})"
    return None


def product_type(ticker: str, name: str | None, exchange: str | None) -> tuple[str, str] | None:
    """``("etp", source)`` when the SEC name or the venue says exchange-traded product, else ``None``."""
    if (ticker or "").strip().upper() in OWN_SHARES:
        return None
    text = (name or "").strip()
    if text and not ROYALTY.search(text):
        for pattern, label in ETP_NAMES:
            if pattern.search(text):
                return "etp", f"name_rule({label})"
    if (exchange or "").strip().upper() in PRODUCT_VENUES:
        return "etp", "venue_rule(Cboe)"
    return None


def security_type(ticker: str, name: str | None, figi: dict | None, tpci: str | None = None,
                  exchange: str | None = None, sic: str | None = None) -> tuple[str, str]:
    """``(type, source)`` from the spine's inputs, through the identity master's one rule set; a name those leave
    untyped takes its ticker's type, and a common-like or untyped one Compustat's SIC 6770, then Compustat's
    ``tpci``, then its SEC name's or its venue's (the module doc)."""
    inputs = {"name": name, "openfigi": figi or {}, "sec": None, "nasdaq": {}, "uw": {}, "fdic": {}}
    typ, src = classify_type(ticker, inputs)
    by_ticker = ticker_type(ticker, exchange) if typ in UNTYPED else None
    if by_ticker:
        return by_ticker
    if sic_code(sic) == SIC_SPAC and typ in SIC_GATE:
        return "spac", SIC_SOURCE
    if typ not in UNTYPED:
        return typ, src
    code = str(tpci).strip() if tpci is not None else ""
    if code in TPCI_TYPES:
        return TPCI_TYPES[code], f"compustat_tpci({code})"
    return product_type(ticker, name, exchange) or (typ, src)
