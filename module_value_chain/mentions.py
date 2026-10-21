"""Turn Item 1/4 prose into supplier / customer / competitor mentions.

Direction is fixed by the filing: the issuer is the source, the mentioned
company the destination. A name that appears without a relation verb is ``other``
(display-only). Unresolved names keep an empty ticker and never seed a hop.
"""
from __future__ import annotations

import re

from . import aliases, config, sentences

_SUPPLIER = re.compile(
    r"\b(purchase[sd]?|supplier[s]?|supplies|supply chain|foundr(?:y|ies)|"
    r"fab(?:ricat\w+)?|wafer[s]?|contract manufacturer|subcontractor|"
    r"outsourc\w+|we (?:buy|source|utilize|use)|provided by|"
    r"rely on|depend on|assembly|packaging)\b", re.I)
_CUSTOMER = re.compile(
    # "customer relationship management" is a software category, not a customer.
    r"\b(customer(?![\s-]+relationship[\s-]+management\b)s?|we sell|sold to|sell to|distributor[s]?|"
    r"our (?:largest|major|key) customers|end users?)\b", re.I)
_COMPETITOR = re.compile(r"\b(competitor[s]?|competition|compete[s]?|rival[s]?)\b", re.I)


_PATTERNS = (("supplier", _SUPPLIER), ("customer", _CUSTOMER), ("competitor", _COMPETITOR))


def _relation(text: str, pos: int, bounds: sentences.Bounds | None = None) -> str:
    """Relation from the *nearest* verb in the window, not a fixed priority.

    Priority order mislabels adjacent sentences ("Our customers include X. We
    purchase from Y.") because every window sees both verbs, and so does the
    nearest verb when the name's own sentence has none: the window stops at the
    sentence holding the name.
    """
    sent_lo, sent_hi = (bounds or sentences.Bounds(text)).at(pos)
    lo = max(sent_lo, pos - config.MENTION_WINDOW)
    hi = min(sent_hi, pos + config.MENTION_WINDOW)
    best_name, best_dist = "other", None
    for name, pattern in _PATTERNS:
        for m in pattern.finditer(text, lo, hi):
            d = abs(m.start() - pos)
            if best_dist is None or d < best_dist:
                best_name, best_dist = name, d
    return best_name


def find_mentions(text: str, filed: str = "") -> list[dict]:
    """Mentions of known companies in ``text`` with a relation + snippet.

    Names come from ``aliases.find_aliases`` (proper-noun and product-name
    guards); a product mention ("Apple Pay") is kept as display-only ``other``.
    """
    text = text or ""
    found: dict[tuple[str, str], dict] = {}
    bounds = sentences.Bounds(text)
    for hit in aliases.find_aliases(text):
        lo = max(0, hit["start"] - config.MENTION_WINDOW)
        hi = min(len(text), hit["end"] + config.MENTION_WINDOW)
        rel = "other" if hit["context_only"] else _relation(text, hit["start"], bounds)
        key = (hit["ticker"], rel)
        if key not in found:
            found[key] = {
                "dst_name": hit["name"],
                "dst_ticker": hit["ticker"],
                "relation": rel,
                "snippet": " ".join(text[lo:hi].split())[:300],
                "filed": filed,
            }
    return list(found.values())
