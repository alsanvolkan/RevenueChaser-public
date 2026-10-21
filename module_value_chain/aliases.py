"""Company-name aliases for the Value Chain board.

A filing names its suppliers and customers in prose ("Taiwan Semiconductor
Manufacturing Company Limited", "TSMC"), not by ticker. This table maps the
legal names we expect to the ticker, after light suffix normalisation. It is a
hand-maintained list on purpose — a general resolver is a different product.
"""
from __future__ import annotations

import re

# Legal suffixes/forms stripped before lookup.
_SUFFIXES = re.compile(
    r"\b(incorporated|inc|corporation|corp|company|co|limited|ltd|plc|llc|"
    r"holdings?|group|technologies|technology|semiconductor|manufacturing|"
    r"international|nv|sa|ag|gmbh|kk|oyj|pty)\b", re.I)


def normalize(name: str) -> str:
    s = (name or "").upper()
    s = re.sub(r"[^A-Z0-9& ]+", " ", s)
    s = _SUFFIXES.sub(" ", s)
    return " ".join(s.split())


# Canonical alias -> ticker. Keys are matched after ``normalize``.
ALIASES: dict[str, str] = {
    "NVIDIA": "NVDA",
    "TAIWAN SEMICONDUCTOR": "TSM",
    "TSMC": "TSM",
    "ADVANCED MICRO DEVICES": "AMD",
    "ASML": "ASML",
    "APPLIED MATERIALS": "AMAT",
    "LAM RESEARCH": "LRCX",
    "KLA": "KLAC",
    "MICRON": "MU",
    "INTEL": "INTC",
    "QUALCOMM": "QCOM",
    "TEXAS INSTRUMENTS": "TXN",
    "BROADCOM": "AVGO",
    "MARVELL": "MRVL",
    "SYNOPSYS": "SNPS",
    "CADENCE": "CDNS",
    "APPLE": "AAPL",
    "MICROSOFT": "MSFT",
    "ALPHABET": "GOOGL",
    "GOOGLE": "GOOGL",
    "META PLATFORMS": "META",
    "FACEBOOK": "META",
    "AMAZON": "AMZN",
    "ORACLE": "ORCL",
    "SALESFORCE": "CRM",
    "ADOBE": "ADBE",
    "SERVICENOW": "NOW",
    "PALO ALTO NETWORKS": "PANW",
    "CROWDSTRIKE": "CRWD",
    "SNOWFLAKE": "SNOW",
    "CLOUDFLARE": "NET",
    "DATADOG": "DDOG",
    "ARISTA NETWORKS": "ANET",
    "DELL": "DELL",
    "HEWLETT PACKARD": "HPQ",
    "WESTERN DIGITAL": "WDC",
    "SEAGATE": "STX",
    "MICROCHIP": "MCHP",
    "ANALOG DEVICES": "ADI",
    "SKYWORKS": "SWKS",
    "QORVO": "QRVO",
    "MONOLITHIC POWER SYSTEMS": "MPWR",
    "ENTEGRIS": "ENTG",
    "COHERENT": "COHR",
    "SUPER MICRO": "SMCI",
    "VERTIV": "VRT",
    "EATON": "ETN",
    "AMPHENOL": "APH",
    "SAMSUNG": "SSNLF",
    "UNITED MICROELECTRONICS": "UMC",
    "GLOBALFOUNDRIES": "GFS",
    # Longest alias wins at a position, so HPE never falls through to HPQ.
    "HEWLETT PACKARD ENTERPRISE": "HPE",
    "HPE": "HPE",
}

# Aliases that are also ordinary English words ("a regular cadence", "our
# salesforce", "sub-micron", "coherent optical", "a microchip shortage"). They
# only match as a proper noun: Title-case ("Cadence") or ALL CAPS ("CADENCE
# DESIGN SYSTEMS").
CASE_SENSITIVE: frozenset[str] = frozenset({
    "CADENCE", "INTEL", "MICRON", "COHERENT", "SALESFORCE", "AMAZON", "APPLE",
    "ORACLE", "EATON", "DELL", "MICROCHIP",
})

# The next word proves the name is not the company ("the Amazon region",
# "our Intel Graph", "Cadence Bank", "Apple Hospitality REIT", "Eaton Vance").
DROP_FOLLOWERS: dict[str, frozenset[str]] = {
    "AMAZON": frozenset({"region", "rainforest", "basin", "river"}),
    "INTEL": frozenset({"graph"}),
    "CADENCE": frozenset({"bank"}),
    "APPLE": frozenset({"hospitality"}),
    "EATON": frozenset({"vance"}),
}

# A capitalised word straight before the name makes it part of another proper
# noun ("Avo Intel", "Private Capital Intel", "Our Intel Graph").
DROP_AFTER_CAPITALISED: frozenset[str] = frozenset({"INTEL"})

# The next word makes it a product or platform, not a counterparty ("Apple
# Pay", "Amazon Alexa", "Microsoft Teams"): kept as a display-only ``other``.
PRODUCT_FOLLOWERS: dict[str, frozenset[str]] = {
    "APPLE": frozenset({"Pay", "Music", "TV", "App", "Watch", "CarPlay", "iPad", "iPads",
                        "iPhone", "Wallet", "Maps", "Card", "News"}),
    "AMAZON": frozenset({"Alexa", "Fire", "Prime", "Sidewalk", "Appstore", "Echo", "Kindle",
                         "Bedrock", "MGM", "Basics"}),
    "GOOGLE": frozenset({"Pay", "Play", "Maps", "Chrome", "Android", "Assistant", "TV"}),
    "MICROSOFT": frozenset({"Teams", "365", "Office", "Windows", "Xbox"}),
}

# Tickers of one issuer: a filing naming its own other share class is not an edge.
SAME_ISSUER: dict[str, str] = {"GOOG": "GOOGL", "BRK-B": "BRK-A", "BRK.B": "BRK.A",
                               "FOX": "FOXA", "NWS": "NWSA"}


def issuer(ticker: str) -> str:
    t = (ticker or "").upper()
    return SAME_ISSUER.get(t, t)


def _pattern() -> "re.Pattern[str]":
    alts = sorted(ALIASES, key=len, reverse=True)
    body = "|".join(re.escape(a).replace(r"\ ", r"\s+") for a in alts)
    return re.compile(r"\b(" + body + r")\b", re.I)


_ALIAS_RE = _pattern()
_NEXT_WORD = re.compile(r"\s+([A-Za-z0-9]+)")
_PREV_CAPITALISED = re.compile(r"\b[A-Z][a-z]+ $")


def find_aliases(text: str) -> list[dict]:
    """Every company-name hit in ``text``: ``{name, ticker, start, end, context_only}``.

    Common-word aliases must be a proper noun, and a few follower/preceder rules
    drop place names and product names; product mentions are ``context_only``.
    """
    text = text or ""
    hits: list[dict] = []
    for m in _ALIAS_RE.finditer(text):
        raw = m.group(0)
        key = " ".join(raw.upper().split())
        if key not in ALIASES:
            continue
        if key in CASE_SENSITIVE and raw not in (key.title(), key):
            continue
        nxt = _NEXT_WORD.match(text, m.end())
        word = nxt.group(1) if nxt else ""
        if word.lower() in DROP_FOLLOWERS.get(key, ()):
            continue
        if key in DROP_AFTER_CAPITALISED and _PREV_CAPITALISED.search(text, max(0, m.start() - 30), m.start()):
            continue
        hits.append({"name": key.title(), "ticker": ALIASES[key], "start": m.start(),
                     "end": m.end(), "context_only": word in PRODUCT_FOLLOWERS.get(key, ())})
    return hits

