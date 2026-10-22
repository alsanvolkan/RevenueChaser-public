"""The dashboard shell page as a page scan sees it (#383, #21): everything except the registry's JSON block.

The shell embeds the whole "Open issues" registry as one ``<script type="application/json">`` block
(``templates/dashboard.html``), and that block is issue prose: the text of 48 GitHub issues, copied by hand. It can
legitimately carry a word a scan of the page looks for (``TradeWhisperer`` in a migration note, ``Regime``, a raw
``YYYY-MM-DDTHH:MM:SS`` timestamp) or a string a scan forbids (``function esc(``). A scan whose subject is the
shell's own markup must not read it, or the registry's content decides the result.

``strip_open_issues_data`` cuts exactly that block out; ``open_issues_block`` returns its text for the tests that do
want the registry (the panel's payload, and the embedded size). Not a test module (no ``test_`` prefix).
"""
from __future__ import annotations

import re

BLOCK_ID = "rc-open-issues-data"
_BLOCK = re.compile(r'<script type="application/json" id="' + BLOCK_ID + r'">.*?</script>', re.S)


def open_issues_block(html: str) -> str:
    """The registry JSON the page embeds (between the block's tags); fails when the block is missing or repeated.

    The block cannot contain ``</script>``: ``html_safe.script_json`` escapes ``<`` before it is embedded.
    """
    found = _BLOCK.findall(html)
    assert len(found) == 1, f"expected one #{BLOCK_ID} block in the shell page, found {len(found)}"
    return found[0].split(">", 1)[1].rsplit("</script>", 1)[0]


def strip_open_issues_data(html: str) -> str:
    """``html`` without the registry's JSON block, so a scan covers the page and not the issue text."""
    return _BLOCK.sub("", html)
