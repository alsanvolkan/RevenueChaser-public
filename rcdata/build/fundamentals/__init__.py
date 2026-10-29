"""The fundamentals data layer (build plan PR 12): what each Tier A company reported, when, and in which filing,
served from ``fundamentals.duckdb``.

The source datasets fetch raw files and type them to Parquet; none of them writes a serving table of its own. Each
one's ``build`` loads its Parquet into TEMP staging tables of the build connection (as the spine's sources do), and
the last dataset of the kind, ``fundamentals/tables``, builds the served tables and views from them
(``tables.py`` holds their schemas). A source that never ran has empty staging tables, never a missing one.

The companies are the spine's Tier A names (``rcdata.read.universe``): their CIKs, gvkeys and GICS codes come from
``spine.duckdb``, so the spine is built first. Every dataset ``starts_by_hand``: none is due before a first CLI run
or Fetch built it, so the first hourly pass after a deploy does not start them before the attended backfill.
"""
from __future__ import annotations

KIND = "fundamentals"
