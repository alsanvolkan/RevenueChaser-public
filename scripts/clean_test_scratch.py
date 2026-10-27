"""Remove the test scratch directories a crashed or killed pytest run left in the system temp dir.

A normal run cleans up after itself: the root ``conftest.py`` removes its own ``rc-pytest-*`` root at
session end, and ``tmp_path_retention_policy`` in ``pytest.ini`` keeps pytest's own ``pytest-of-*``
subdirectories from piling up. Neither helps when a run is killed, so this script is the broom for
what is already on disk.

It only ever removes a directory whose name matches one of the two prefixes **and** which nothing has
written to for ``--min-age-hours`` (default 2), so a run happening right now is never touched.

    python -m RevenueChaser.scripts.clean_test_scratch            # report only, removes nothing
    python -m RevenueChaser.scripts.clean_test_scratch --apply    # remove them
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import time
from pathlib import Path

# conftest.py's own root, and pytest's basetemp parent (`pytest-of-<user>`).
PREFIXES = ("rc-pytest-", "pytest-of-")
DEFAULT_MIN_AGE_HOURS = 2.0
# How many levels below each candidate the age check looks; see _newest_mtime.
SCAN_DEPTH = 2


def _dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue  # a file vanishing mid-walk is the normal case here, not an error
    return total


def _newest_mtime(path: Path, depth: int = SCAN_DEPTH) -> float | None:
    """The newest mtime in the top ``depth`` levels of ``path``, or None if it cannot be read.

    One level is not enough. Writing an existing file in place — which is what DuckDB does to
    ``<root>/.store/raw.duckdb``, two levels down — changes that file's mtime and nothing above it,
    so a running session would look untouched and be swept. Two levels covers the redirected roots
    (``.store``, ``.cache``, ``edgar_docs``, ``spx_gamma``); a write deeper than
    that is not seen, which is why the default age is hours and not minutes.
    """
    try:
        newest = path.stat().st_mtime
    except OSError:
        return None
    stack = [(path, 0)]
    while stack:
        here, level = stack.pop()
        if level >= depth:
            continue
        try:
            for child in here.iterdir():
                newest = max(newest, child.stat().st_mtime)
                if child.is_dir():
                    stack.append((child, level + 1))
        except OSError:
            continue  # a directory vanishing mid-walk is normal here
    return newest


def stale_dirs(root: Path, min_age_hours: float, now: float | None = None) -> list[Path]:
    """The scratch directories under ``root`` that are old enough to remove, oldest first."""
    cutoff = (time.time() if now is None else now) - min_age_hours * 3600
    out = []
    for p in sorted(root.iterdir() if root.is_dir() else []):
        if not p.is_dir() or not p.name.startswith(PREFIXES):
            continue
        newest = _newest_mtime(p)
        if newest is None:
            continue
        if newest < cutoff:
            out.append(p)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="actually remove them (default: report only)")
    ap.add_argument("--min-age-hours", type=float, default=DEFAULT_MIN_AGE_HOURS,
                    help=f"leave anything written to more recently than this (default {DEFAULT_MIN_AGE_HOURS})")
    ap.add_argument("--temp-dir", default=None, help="where to look (default: the system temp dir)")
    a = ap.parse_args(argv)

    root = Path(a.temp_dir) if a.temp_dir else Path(tempfile.gettempdir())
    victims = stale_dirs(root, a.min_age_hours)
    if not victims:
        print(f"nothing to clean in {root} (nothing matching {' / '.join(PREFIXES)} older than "
              f"{a.min_age_hours} h)")
        return 0

    total = sum(_dir_size(p) for p in victims)
    print(f"{len(victims)} scratch directories in {root}, {total / 1024 / 1024:,.0f} MB")
    for p in victims[:10]:
        print(f"  {p.name}")
    if len(victims) > 10:
        print(f"  ... and {len(victims) - 10} more")

    if not a.apply:
        print("report only; pass --apply to remove them")
        return 0

    removed = failed = 0
    for p in victims:
        shutil.rmtree(p, ignore_errors=True)
        if p.exists():
            failed += 1  # a file still open (Windows holds a lock); the next run gets it
        else:
            removed += 1
    print(f"removed {removed}, still present {failed}, freed about {total / 1024 / 1024:,.0f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
