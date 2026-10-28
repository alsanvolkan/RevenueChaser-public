"""Tier rule v1: which SEC-file names the boards screen (Tier A) and why the others are not.

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

The $300M market-cap floor is not a tier: it needs market data, which the spine does not hold (``core/universe.py``
applies it at read time). The type comes from ``core.identity.types.classify_type`` over the spine's own inputs
(OpenFIGI's security types and the SEC name; no SEC submissions), so a preferred share written ``WFC-PC`` is typed by
the ticker rule even before OpenFIGI answered; a name those leave untyped takes Compustat's ``tpci`` (``TPCI_TYPES``).
"""
from __future__ import annotations

from ....core.identity.types import EXCLUDED_TYPES, classify_type

VERSION = 1
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


def security_type(ticker: str, name: str | None, figi: dict | None, tpci: str | None = None) -> tuple[str, str]:
    """``(type, source)`` from the spine's inputs, through the identity master's one rule set; Compustat's ``tpci``
    fills a name those leave untyped."""
    inputs = {"name": name, "openfigi": figi or {}, "sec": None, "nasdaq": {}, "uw": {}, "fdic": {}}
    typ, src = classify_type(ticker, inputs)
    code = str(tpci).strip() if tpci is not None else ""
    if typ in UNTYPED and code in TPCI_TYPES:
        return TPCI_TYPES[code], f"compustat_tpci({code})"
    return typ, src
