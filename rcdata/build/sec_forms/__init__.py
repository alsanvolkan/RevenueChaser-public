"""``sec_forms`` -- the one layer that cuts and stores the text of a stored SEC annual report (#752-#757).

The boards read plain text out of it and apply their own rules; nothing here extracts a number or calls a model.
Two tables: ``filing_section`` (the item or business section of a document) and ``filing_note`` (the filer's own
inline-XBRL note blocks, ``notes.py``).
"""
from __future__ import annotations

KIND = "sec_forms"
