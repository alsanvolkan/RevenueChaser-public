"""The SEC form reader: one layer that cuts an annual report into sections and notes, and stores plain text.

Boards apply their own rules on top; nothing here is a board's rule. Its two sources of text are separate on
purpose:

- **the items** -- Item 1, 1A, 7, 8 and the 20-F/40-F equivalents -- cut by ``edgartools`` in its own venv
  (:mod:`cutter_worker`, driven by :mod:`cutter`), with our own versioned rule (``rcdata/sec_sections.py``) as the
  rollback;
- **the notes** -- the filer's own inline-XBRL text blocks, read by ``rcdata/sec_notes.py``, which needs no
  third-party package and no venv.

Nothing here connects to the SEC. Downloading and storing a filing is the fundamentals build's job; this layer is
handed paths to documents that are already on disk, and the cutter worker has the socket primitives taken away
from it before ``edgar`` is imported, so the library cannot reach out even by accident.
"""
