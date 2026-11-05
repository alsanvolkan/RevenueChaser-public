"""What changed since the live serving file was built: the dirty-set contract (#446 step 2).

A build on a copy of the live file (``serving.build(kind, base="live")``, step 1) only pays off when it can say
which rows to reload. That answer has two parts, and this module holds both:

- :func:`full_rebuild_reason` — the cases where no dirty set is safe and the kind must be built whole: a live file
  that is missing or built by another serving schema version (``serving.live_version``), or a dataset that says its
  source restated history (``full_rebuild_reason(ctx)``, e.g. a WRDS monthly refresh, SEC republishing a set).
- :func:`of` — one dataset's dirty set, as a :class:`Dirty`. A dataset declares ``dirty(ctx)``; a dataset that
  declares nothing is :data:`ALL`, which is why this PR changes no behaviour: every kind is still built whole.

A dirty set has two axes, because the tables have two shapes (the classification the incremental steps read):
a **cumulative** table only ever gains rows for new source days, so what is dirty is a *range* of days; a
**restated** table can have an old row changed by new data, so what is dirty is a *key* whose whole history is
rebuilt. A third shape, a chosen list (``my_universe``), narrows to neither and is always built whole. ``Dirty``
carries both axes and is "everything" when it carries neither, so a dataset that forgets to narrow gets the slow,
correct answer rather than a silently partial table.

The signal a dataset's ``dirty`` is meant to read is :func:`changed_partitions`: the Parquet files written after
the live file's build stamp (``serving_schema_version.built_at``). A dataset's own job is only to map a partition
to the keys it touches — never to guess what changed inside one. The stamp is written when the build file opens
(``serving._stamp``), before any dataset reads Parquet, so a partition written while a long build runs is newer
than the stamp and comes out dirty on the next build rather than being lost (test
``test_the_build_stamp_is_the_builds_start_not_its_swap``).

shortcut: the signal is the partition's mtime, so two cases read as clean when they are not — a copy, an rsync
or a restore from backup carrying an old mtime over from another machine (a build run on another box and its file
moved here is the live case), and a partition that was deleted or renamed after the build, whose rows stay in the
live file with nothing to mark them. Upgrade to a manifest of (path, size, mtime) written into the build file and
compared manifest-to-manifest, which catches both, before any kind relies on the dirty set for data correctness —
that is, before step 3 lands. ``test_dirty.py`` holds both cases as xfail.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable

import duckdb

from .. import serving
from . import base

@dataclass(frozen=True)
class Dirty:
    """What of a dataset is dirty: day ranges (cumulative tables) and keys (restated tables).

    Carrying neither means everything (:attr:`all`), which is the safe default: a dataset that narrows nothing is
    built whole, as every dataset is today. ``ranges`` are inclusive ``(first, last)`` day pairs.
    """

    ranges: tuple[tuple[date, date], ...] = ()
    keys: frozenset[str] = frozenset()

    @property
    def all(self) -> bool:
        return not (self.ranges or self.keys)


#: Everything is dirty: what a dataset that declares no ``dirty`` returns, so its tables are built from the whole
#: Parquet tree as they are today.
ALL = Dirty()

#: How far before the stamp a partition still counts as dirty: one second, for a file system that keeps mtimes to
#: the second (``changed_partitions``). Both sides are UTC epoch seconds (``time.time`` and ``st_mtime``).
MTIME_SLACK_S = 1.0


def live_built_at(kind: str) -> float | None:
    """When the live ``kind`` file's build started (its ``built_at`` stamp), or None when there is no stamp.

    None means "nothing known about this file": every partition is then dirty (:func:`changed_partitions`).
    """
    try:
        with serving.open_read(kind) as con:
            rows = con.execute(f"SELECT built_at FROM {serving.VERSION_TABLE} WHERE kind = ?", (kind,)).fetchall()
    except (serving.ServingFileMissing, duckdb.Error):
        return None
    if len(rows) != 1 or rows[0][0] is None:
        return None
    return float(rows[0][0])


def changed_partitions(since: float | None, under: Path | str | None = None) -> list[Path]:
    """The Parquet files under ``under`` (the whole Parquet root by default) written after ``since``, in path order.

    ``since`` None returns every partition: nothing is known about the live file, so nothing can be called clean.
    A missing directory is empty, not an error — a dataset that has never staged anything has no dirty partition.

    The cutoff is ``since`` less :data:`MTIME_SLACK_S`, because a file system (or an rsync) that keeps mtimes to
    the second truncates one written inside the stamp's own second to before it, which would read as clean. The
    slack re-reads a partition now and then; it never calls a written one clean.
    """
    root = Path(under) if under is not None else base.parquet_root()
    if not root.is_dir():
        return []
    files = sorted(p for p in root.rglob("*.parquet") if p.is_file())
    if since is None:
        return files
    cutoff = since - MTIME_SLACK_S
    return [p for p in files if p.stat().st_mtime > cutoff]


def of(ds: Any, ctx: Any) -> Dirty:
    """``ds``'s dirty set: its own ``dirty(ctx)``, else :data:`ALL`.

    A dataset answers with a :class:`Dirty` (or None for "everything"); any other shape is a mistake in that
    dataset and raises here rather than being read as a narrowing, which would serve a partial table.
    """
    step = getattr(ds, "dirty", None)
    if step is None:
        return ALL
    answer = step(ctx)
    if answer is None:
        return ALL
    if not isinstance(answer, Dirty):
        raise TypeError(f"{getattr(ds, 'name', ds)}.dirty returned {type(answer).__name__}, not a Dirty")
    return answer


def full_rebuild_reason(kind: str, datasets: Iterable[Any] | None = None, ctx: Any = None) -> str | None:
    """Why ``kind`` must be built whole, or None when a build on the live copy is safe.

    ``datasets`` defaults to the kind's own (``base.kind_datasets``); a dataset is asked through its optional
    ``full_rebuild_reason(ctx)``, and the first one that answers wins, named in the reason.
    """
    wanted = serving.SERVING_SCHEMA_VERSION[kind]
    if serving.live_version(kind) != wanted:
        return f"no live {kind} file at serving schema version {wanted}"
    if live_built_at(kind) is None:
        return f"the live {kind} file carries no build stamp"
    for ds in (base.kind_datasets(kind) if datasets is None else datasets):
        step = getattr(ds, "full_rebuild_reason", None)
        if step is None:
            continue
        why = step(ctx)
        if why:
            return f"{ds.name}: {why}"
    return None
