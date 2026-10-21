"""The board HTML trust boundary (ADR-X1, #144).

Every board renders untrusted strings: SEC issuer names, 10-K snippets, LLM theme
names, provider fields and request parameters. They reach a page only through
these helpers:

- ``esc``: markup text or a quoted attribute;
- ``script_json``: a value embedded in an inline ``<script>`` block;
- ``fill``: one-pass placeholder substitution, so a value that spells another
  placeholder is never substituted again;
- ``ESC_JS``: the client-side ``rcEsc`` for anything a board's JS puts into
  ``innerHTML``. Inline handlers take no interpolated data; they read ``data-*``.

Boards run in same-origin iframes, so one missed escape has the shell's authority
(review: ``.hermes/plans_claude/arch/2026-10-02_xss_trust_boundary_review.md``).
"""
from __future__ import annotations

import html
import json
import re

__all__ = ["esc", "script_json", "fill", "ESC_JS"]


def esc(v) -> str:
    """Text safe in markup and in a ``"``- or ``'``-quoted attribute."""
    return html.escape(str(v if v is not None else ""), quote=True)


_SCRIPT_ESCAPES = {"<": "\\u003c", ">": "\\u003e", "&": "\\u0026",
                   "\u2028": "\\u2028", "\u2029": "\\u2029"}
_SCRIPT_RE = re.compile("[<>&\u2028\u2029]")


def script_json(obj) -> str:
    """JSON that is safe inside an inline ``<script>`` block.

    Escaping ``<`` itself (not only ``</``) keeps ``<!--`` and ``<script`` away from
    the HTML tokenizer: either one would stop the board's own ``</script>`` from
    closing the block and blank the whole board.
    """
    s = json.dumps(obj, ensure_ascii=False, default=str)
    return _SCRIPT_RE.sub(lambda m: _SCRIPT_ESCAPES[m.group(0)], s)


def fill(template: str, subs: dict[str, str]) -> str:
    """Replace every placeholder of ``subs`` in one pass (values are literal)."""
    if not subs:
        return template
    pattern = "|".join(map(re.escape, sorted(subs, key=len, reverse=True)))
    return re.sub(pattern, lambda m: subs[m.group(0)], template)


# The only client-side escaper (#175): the board ``esc`` copies, ``tcEsc`` and
# ``RCFilters.core.esc`` are gone, and ``core/tests/test_one_escaper_175.py`` fails on a
# new one. A page that calls ``rcEsc`` must embed this snippet (the filter and ticker-combo
# snippets already do).
ESC_JS = r"""
function rcEsc(s){
  return String(s == null ? "" : s).replace(/[&<>"']/g, function(c){
    return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];
  });
}
"""
