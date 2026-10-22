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

# NFKD + ASCII does not fold these, it deletes them; map them first so
# "Strauß" / "Ørsted" keep their letters.
_PRE_FOLD = str.maketrans({"ß": "ss", "Ø": "o", "ø": "o"})

# Legal-entity / holding-structure suffixes dropped from a TRAILING run of
# tokens only (never mid-name: "AB Science" keeps "ab"), in no particular
# order.
_DROP_TOKENS = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "ltd",
    "limited", "plc", "llc", "lp", "sa", "ag", "nv", "bv", "ab", "as", "asa",
    "se", "gmbh", "holdings", "holding", "group", "the", "spa",
    "sas", "pty", "srl", "kgaa",
}


def normalise_sponsor(name: str) -> str:
    """Fold a sponsor/filer name to a comparable token sequence.

    Strips a trailing state-code suffix (``/ MA``, ``/DE/``), collapses a
    single-letter slash pair (``A/S`` -> ``AS``), folds accents to ASCII,
    expands ``&`` to ``and`` and drops legal-entity suffixes -- but only as a
    trailing run, so a suffix-looking word that is actually part of the name
    ("AB Science") survives.

    EDGAR spells a slash pair out ("NOVO NORDISK A S"), so a trailing run of
    two or more one-letter tokens is fused before the suffix drop, landing on
    the same ``as`` a written ``A/S`` produces. A lone trailing letter is left
    alone (it is part of the name, not a suffix).
    """
    if not name:
        return ""
    text = name.translate(_PRE_FOLD)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = _STATE_SUFFIX_RE.sub("", text)
    text = _SLASH_PAIR_RE.sub(r"\1\2", text)
    text = text.replace(".", "")
    text = text.replace("&", " and ")
    text = text.lower()
    text = _NON_ALNUM_RE.sub(" ", text)
    tokens = [t for t in text.split() if t]

    if len(tokens) > 1 and all(len(t) == 1 for t in tokens[-2:]):
        run = 2
        while run < len(tokens) and len(tokens[-run - 1]) == 1:
            run += 1
        tokens[-run:] = ["".join(tokens[-run:])]

    while tokens and (tokens[-1] in _DROP_TOKENS or tokens[-1] == "and"):
        tokens.pop()
    if tokens and tokens[0] == "the":
        tokens.pop(0)

    return " ".join(tokens)
