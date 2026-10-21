"""Sponsor-name normalisation shared between SEC filers and FDA OOPD sponsors.

Both sources spell the same company differently ("Tenaya Therapeutics, Inc."
vs "Tenaya Therapeutics" vs "TENAYA THERAPEUTICS INC / MA"); normalising both
sides to the same token sequence is what lets ``designations_for`` match them.
"""
from __future__ import annotations

import re
import unicodedata

_STATE_SUFFIX_RE = re.compile(r"\s*/\s*[A-Za-z]{2}\s*/?\s*$")
_SLASH_PAIR_RE = re.compile(r"\b([A-Za-z])/([A-Za-z])\b")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

# Legal-entity / holding-structure suffixes dropped from a TRAILING run of
# tokens only (never mid-name: "AB Science" keeps "ab"), in no particular
# order.
_DROP_TOKENS = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "ltd",
    "limited", "plc", "llc", "lp", "sa", "ag", "nv", "bv", "ab", "as", "asa",
    "se", "gmbh", "holdings", "holding", "group", "the", "spa",
}


def normalise_sponsor(name: str) -> str:
    """Fold a sponsor/filer name to a comparable token sequence.

    Strips a trailing state-code suffix (``/ MA``, ``/DE/``), collapses a
    single-letter slash pair (``A/S`` -> ``AS``), folds accents to ASCII,
    expands ``&`` to ``and`` and drops legal-entity suffixes -- but only as a
    trailing run, so a suffix-looking word that is actually part of the name
    ("AB Science") survives.
    """
    if not name:
        return ""
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    text = _STATE_SUFFIX_RE.sub("", text)
    text = _SLASH_PAIR_RE.sub(r"\1\2", text)
    text = text.replace(".", "")
    text = text.replace("&", " and ")
    text = text.lower()
    text = _NON_ALNUM_RE.sub(" ", text)
    tokens = [t for t in text.split() if t]

    while tokens and (tokens[-1] in _DROP_TOKENS or tokens[-1] == "and"):
        tokens.pop()
    if tokens and tokens[0] == "the":
        tokens.pop(0)

    return " ".join(tokens)
