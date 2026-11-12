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

The mtime alone reads two cases as clean when they are not — a copy, an rsync or a restore carrying an old mtime
over, and a partition deleted or renamed after the build. So a kind that builds incrementally writes a **manifest**
into its build file (:data:`MANIFEST_TABLE`: every Parquet file of its datasets with its size and mtime, taken
before any dataset reads), and the next build compares the tree with it file by file (:func:`changes`): a file
whose size or mtime differs, a new one and a missing one are all changed. Only a live file built before the
manifest existed falls back to the mtime against ``built_at`` (one build, the first after this landed).

:func:`plan` is what ``base.build_kind_many`` asks: whether the kind builds on a copy of the live file, and if so
each dataset's dirty set. A kind builds incrementally only when **every** one of its datasets declares ``dirty``;
any other kind is built whole exactly as before, and gets no manifest. A whole build is still forced by a serving
schema version bump, a change of the kind's build code (:func:`rules_fingerprint`: the extraction and table rules
are code, and a change to them restates every row), a dataset's own ``full_rebuild_reason`` (a WRDS monthly
snapshot), more than :data:`MAX_DIRTY_SHARE` of the files changed (a re-extract of everything is faster built
whole), and ``RC_BUILD_WHOLE=1`` (the lever by hand).
"""
from __future__ import annotations

import hashlib
import os
import sys
from dataclasses import dataclass, field
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
    clean: bool = False                     # nothing of the dataset changed: neither axis, and not "everything"

    @property
    def all(self) -> bool:
        return not (self.clean or self.ranges or self.keys)


#: Everything is dirty: what a dataset that declares no ``dirty`` returns, so its tables are built from the whole
#: Parquet tree as they are today.
ALL = Dirty()
#: Nothing is dirty: a keyed dataset none of whose partitions changed.
CLEAN = Dirty(clean=True)

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


# ── The incremental build's plan (#446 steps 3 and 7) ─────────────────────────────────────────────────────────

#: The build file's list of the Parquet files it was built from: ``(path, size, mtime)``, the path relative to the
#: Parquet root. Taken before any dataset reads, so a file written during the build is changed next time.
MANIFEST_TABLE = "build_manifest"
#: The fingerprint of the kind's build code the file was built by (:func:`rules_fingerprint`), one row.
RULES_TABLE = "build_rules"
#: More than this share of a kind's Parquet files changed: built whole, which is then faster than deleting most rows.
MAX_DIRTY_SHARE = 0.5
#: ``RC_BUILD_WHOLE=1`` forces a whole build of every kind (the lever by hand).
WHOLE_ENV = "RC_BUILD_WHOLE"


def incremental(datasets: Iterable[Any]) -> bool:
    """Whether a kind of these datasets builds incrementally: every one declares ``dirty`` (and there is one)."""
    datasets = list(datasets)
    return bool(datasets) and all(getattr(d, "dirty", None) is not None for d in datasets)


def dataset_dir(ds: Any) -> Path:
    """``parquet/<source>/<name>`` of dataset ``<source>/<name>``."""
    return base.parquet_root().joinpath(*str(ds.name).split("/", 1))


def read_dirs(ds: Any) -> list[Path]:
    """The Parquet folders ``ds``'s build reads: its own, and those of another kind's dataset it names in
    ``parquet_reads`` (``wrds/prices`` reads the spine's ``wrds/sec_idhist``), so a change there is seen too."""
    root = base.parquet_root()
    return [dataset_dir(ds), *(root.joinpath(*str(n).split("/", 1)) for n in getattr(ds, "parquet_reads", ()))]


def scan(datasets: Iterable[Any]) -> dict[str, tuple[int, float]]:
    """Every Parquet file of ``datasets``: its path relative to the Parquet root, its size and mtime."""
    root = base.parquet_root()
    out: dict[str, tuple[int, float]] = {}
    for folder in dict.fromkeys(f for d in datasets for f in read_dirs(d)):
        if not folder.is_dir():
            continue
        for p in folder.rglob("*.parquet"):
            try:
                st = p.stat()
            except FileNotFoundError:                 # replaced while listed: the next build sees it
                continue
            if p.is_file():
                out[p.relative_to(root).as_posix()] = (int(st.st_size), float(st.st_mtime))
    return dict(sorted(out.items()))


def rules_fingerprint(datasets: Iterable[Any]) -> str:
    """A hash of every Python file in the packages that hold ``datasets``' code, and of each dataset's
    ``rules_state()`` (a setting outside the package that changes what is served, the ``filing_item`` text cap): a
    change to the extraction or the table rules restates rows no Parquet file marks as changed."""
    datasets = list(datasets)
    folders = set()
    h = hashlib.sha256()
    for d in datasets:
        state = getattr(d, "rules_state", None)
        if state is not None:
            h.update(f"{d.name}={state()}".encode() + b"\0")
    # The code that decides what a build on the live copy reloads is a rule too: a fix to it must reach the rows
    # it built wrongly, which only a whole build revisits (review of #554).
    for module_name in (__name__, base.__name__, serving.__name__):
        path = getattr(sys.modules.get(module_name), "__file__", None)
        if path:
            h.update(Path(path).name.encode() + b"\0" + Path(path).read_bytes() + b"\0")
    for d in datasets:
        module = sys.modules.get(type(d).__module__)
        path = getattr(module, "__file__", None)
        if path:
            folders.add(Path(path).resolve().parent)
    for folder in sorted(folders):
        for p in sorted(folder.glob("*.py")):
            h.update(p.name.encode() + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()


def _live_rows(kind: str, sql: str) -> list[tuple] | None:
    try:
        with serving.open_read(kind) as con:
            return con.execute(sql).fetchall()
    except (serving.ServingFileMissing, duckdb.Error):
        return None


def live_manifest(kind: str) -> dict[str, tuple[int, float]] | None:
    rows = _live_rows(kind, f"SELECT path, size, mtime FROM {MANIFEST_TABLE}")
    return None if rows is None else {p: (int(s), float(m)) for p, s, m in rows}


def live_rules(kind: str) -> str | None:
    rows = _live_rows(kind, f"SELECT fingerprint FROM {RULES_TABLE}")
    return rows[0][0] if rows and len(rows) == 1 else None


def changes(kind: str, current: dict[str, tuple[int, float]]) -> tuple[Path, ...] | None:
    """The Parquet files changed since the live file was built, as absolute paths: changed, new or gone.

    Against the live file's manifest when it has one; otherwise (a file built before the manifest) the files of
    ``current`` written after its ``built_at`` — no gone file can be seen then. None when neither is known."""
    root = base.parquet_root()
    live = live_manifest(kind)
    if live is not None:
        out = {p for p, stat in current.items() if live.get(p) != stat} | (set(live) - set(current))
        return tuple(root / p for p in sorted(out))
    since = live_built_at(kind)
    if since is None:
        return None
    cutoff = since - MTIME_SLACK_S
    return tuple(root / p for p, (_, mtime) in current.items() if mtime > cutoff)


def keys_of(changed: Iterable[Path], under: Path, field_name: str = "cik") -> frozenset[str]:
    """The ``<field>=<value>`` values of the changed files under ``under`` (``cik=789019/part-0.parquet``)."""
    out = set()
    for p in changed:
        try:
            rel = p.relative_to(under)
        except ValueError:
            continue
        for part in rel.parts[:-1]:
            if part.startswith(field_name + "="):
                out.add(part.split("=", 1)[1])
    return frozenset(out)


def keyed(ctx: Any, ds: Any, field_name: str = "cik") -> Dirty:
    """A dataset partitioned by ``<field>=<key>``: the keys of its changed partitions, :data:`CLEAN` for none."""
    changed = getattr(ctx, "changed", None)
    if changed is None:
        return ALL
    keys = keys_of(changed, dataset_dir(ds), field_name)
    return Dirty(keys=keys) if keys else CLEAN


@dataclass
class Plan:
    """What ``build_kind_many`` does for an incremental kind: ``reason`` None builds on the live copy."""

    reason: str | None
    dirty: dict[str, Dirty]
    manifest: dict[str, tuple[int, float]]
    rules: str
    changed: tuple[Path, ...] | None
    #: The live file had no manifest, so ``changed`` came from its ``built_at``: no gone file is in it, and a
    #: keyed dataset adds the keys the live file serves that have no partition any more (``common.dirty_ciks``).
    fallback: bool = False
    memo: dict[str, Any] = field(default_factory=dict)       # what the datasets of one build share (their keys)

    def note(self) -> str:
        if self.reason:
            return f"built whole: {self.reason}"
        n = len(self.changed or ())
        if "note" in self.memo:                         # a kind that words its own dirty set (``prices``)
            return f"built on the live file: {n} Parquet file(s) changed, {self.memo['note']}"
        keys = self.memo.get("ciks")
        if keys is None:
            keys = set().union(*(d.keys for d in self.dirty.values())) if self.dirty else set()
        return f"built on the live file: {n} Parquet file(s) changed, {len(keys)} key(s) dirty"


def plan(kind: str, ctx: Any, datasets: Iterable[Any] | None = None) -> Plan | None:
    """The build plan of ``kind``; None when the kind does not build incrementally (built whole, as before)."""
    datasets = list(base.kind_datasets(kind) if datasets is None else datasets)
    if not incremental(datasets):
        return None
    current = scan(datasets)
    rules = rules_fingerprint(datasets)
    fallback = live_manifest(kind) is None
    changed = changes(kind, current)
    ctx.changed = changed
    reason = None
    if os.getenv(WHOLE_ENV, "").strip() == "1":
        reason = f"{WHOLE_ENV}=1"
    if reason is None:
        reason = full_rebuild_reason(kind, datasets, ctx)
    if reason is None:
        held = live_rules(kind)
        # A live file built before the fingerprint existed is trusted once: it was built by the code just before it.
        if held is not None and held != rules:
            reason = f"the build code of {kind} changed since the live file was built"
    if reason is None and changed is None:
        reason = f"nothing is known of what the live {kind} file was built from"
    if reason is None and current and len(changed) > MAX_DIRTY_SHARE * len(current):
        reason = f"{len(changed)} of {len(current)} Parquet files changed"
    sets: dict[str, Dirty] = {}
    if reason is None:
        for d in datasets:
            sets[d.name] = of(d, ctx)
            if sets[d.name].all and getattr(d, "dirty_keyed", False):
                reason = f"{d.name}: no narrower dirty set"
                sets = {}
                break
    return Plan(reason, sets, current, rules, changed, fallback)


def write_manifest(con: duckdb.DuckDBPyConnection, p: Plan) -> None:
    """Write ``p``'s manifest and rules fingerprint into the build file (over what a live copy carried)."""
    con.execute(f"CREATE OR REPLACE TABLE {MANIFEST_TABLE} (path TEXT, size BIGINT, mtime DOUBLE)")
    if p.manifest:
        paths = list(p.manifest)
        con.execute(f"INSERT INTO {MANIFEST_TABLE} SELECT unnest(?), unnest(?), unnest(?)",
                    (paths, [p.manifest[x][0] for x in paths], [p.manifest[x][1] for x in paths]))
    con.execute(f"CREATE OR REPLACE TABLE {RULES_TABLE} (fingerprint TEXT)")
    con.execute(f"INSERT INTO {RULES_TABLE} VALUES (?)", (p.rules,))
