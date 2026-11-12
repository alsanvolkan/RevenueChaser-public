"""``python -m RevenueChaser.module_watchlists`` — import, export and list the watch lists (#786)."""
from __future__ import annotations

from .importer import main

if __name__ == "__main__":                                   # pragma: no cover - the CLI entry point
    raise SystemExit(main())
