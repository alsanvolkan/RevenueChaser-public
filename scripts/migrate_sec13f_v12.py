"""One-time v12 reset of the 13F store (#208, F3).

Schema 12 gives ``sec13f_holdings`` a ``(accession, line_no)`` PRIMARY KEY and nine non-lossy
columns, and ``sec13f_submissions`` the amendment metadata. A changed key does not migrate by
itself (the store only adds missing columns), and re-keying ~20M rows in Python is the same
~14 GB trap the derive avoids on purpose. ``sec13f_holdings`` is fully regenerable from the
data-set zips, so the table is dropped and refilled instead of copied.

Run it once, with the dashboard **stopped** (the script writes the store through ``rawstore``
directly, so a running dashboard's writer would be a second writer on the same file), the store
backed up and ``/api/jobs`` idle (the script cannot see the job queue; the caller checks it)::

    python scripts/migrate_sec13f_v12.py --dry-run
    python scripts/migrate_sec13f_v12.py

Inputs: ``.store/raw.duckdb`` and the ``*_form13f.zip`` files in ``.cache/sec_13f``. The zips are
passed to ``ingest_zips`` as paths, so no SEC request is made and ``download_missing_zips`` never
runs. The preflight refuses while a zip named in ``sec13f_ingested_zips`` is missing from disk:
clearing the markers makes every listed zip a download candidate again, so a missing file would
cost a multi-GB re-download.

A second run refuses with "already at v12; use --force-reingest": rebuilding a table that is
already at the v12 key would throw the ingested rows away for nothing.

The derive runs in a FRESH process (``--derive-only``), because on the real store the re-ingest
spends this process's DuckDB temp budget and the VALUE-units median then fails with an
``OutOfMemoryException`` — see ``derive_in_a_fresh_process``. If that child fails, the re-ingest
is already committed and only the derive has to be repeated::

    python -m RevenueChaser.scripts.migrate_sec13f_v12 --derive-only

Exit codes: 0 ok, 1 refused (nothing written), 2 the re-ingest committed but the derive failed.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from RevenueChaser.core import rawstore
from RevenueChaser.module_sec_13f import engine

#: The v12 PRIMARY KEY of ``sec13f_holdings``.
V12_KEY = ["accession", "line_no"]

EXIT_OK = 0
EXIT_REFUSED = 1
#: The re-ingest committed but the derive process failed: distinct from a refusal, which writes nothing.
EXIT_DERIVE_FAILED = 2


class MigrationRefused(RuntimeError):
    """A preflight check refused the run; nothing was written."""


class DeriveSubprocessFailed(RuntimeError):
    """The re-ingest is committed but the derive process failed; the message names the retry command."""


def _store_verdict(ownership_dir: Path) -> dict:
    """The read-only half of the preflight: key, markers, zips on disk and the before counts."""
    try:
        with rawstore.connect(read_only=True) as conn:
            live_key = rawstore._live_primary_keys(conn, "sec13f_holdings").get("sec13f_holdings", [])
            ingested = [r[0] for r in conn.execute(
                "SELECT filename FROM sec13f_ingested_zips ORDER BY filename").fetchall()]
    except rawstore.StoreLockedError as exc:
        raise MigrationRefused(
            f"the store is held by another process ({exc}); close the dashboard and retry") from exc
    except (FileNotFoundError, OSError) as exc:
        raise MigrationRefused(f"cannot read the store at {rawstore.DB_PATH}: {exc}") from exc
    on_disk = sorted(p.name for p in ownership_dir.glob("*_form13f.zip")) if ownership_dir.exists() else []
    return {"live_key": live_key, "ingested": ingested, "on_disk": on_disk,
            "missing": [n for n in ingested if n not in on_disk]}


def _counts() -> dict:
    """Row counts, distinct filer-periods and holders per period — the card's before/after columns."""
    one = lambda sql: (rawstore.query(sql) or [{"n": 0}])[0]["n"]  # noqa: E731 - one expression per count
    holders = rawstore.query(
        "SELECT s.period AS period, count(DISTINCT h.cik) AS n FROM sec13f_holdings h "
        "JOIN sec13f_submissions s ON s.accession = h.accession "
        "WHERE s.type LIKE '13F-HR%' AND h.cik IS NOT NULL AND h.cik <> '' "
        "GROUP BY s.period ORDER BY s.period")
    return {
        "holdings": int(one("SELECT count(*) AS n FROM sec13f_holdings") or 0),
        "submissions": int(one("SELECT count(*) AS n FROM sec13f_submissions") or 0),
        "filer_periods": int(one(
            "SELECT count(*) AS n FROM (SELECT DISTINCT cik, period FROM sec13f_submissions "
            "WHERE period IS NOT NULL)") or 0),
        "holders": {r["period"]: int(r["n"] or 0) for r in holders},
    }


def preflight(ownership_dir: Path | None = None, *, force: bool = False) -> dict:
    """Read-only checks and the before counts. Raises ``MigrationRefused`` and writes nothing."""
    ownership_dir = Path(ownership_dir or engine.OWNERSHIP_DIR)
    verdict = _store_verdict(ownership_dir)
    if verdict["missing"]:
        raise MigrationRefused(
            "these ingested zips are not on disk; clearing the markers would make them download "
            "candidates again (" + ", ".join(verdict["missing"]) + ")")
    if verdict["live_key"] == V12_KEY and not force:
        raise MigrationRefused("already at v12; use --force-reingest")
    before = _counts()
    before.update({"zips_on_disk": len(verdict["on_disk"]), "live_key": verdict["live_key"]})
    return before


def reset_holdings_and_markers() -> None:
    """DROP + CREATE ``sec13f_holdings`` at the v12 DDL and clear the ingested-zip markers, in one transaction."""
    ddl = "CREATE TABLE sec13f_holdings (\n    " + ",\n    ".join(
        rawstore.MANIFEST["sec13f_holdings"]["columns"]) + "\n)"

    def _reset(tx):
        tx.execute("DROP TABLE IF EXISTS sec13f_holdings")
        tx.execute(ddl)
        tx.execute("DELETE FROM sec13f_ingested_zips")

    rawstore.atomic(_reset)


#: The documented rebuild command, as a ``python -m`` argument list (see ``derive_in_a_fresh_process``).
DERIVE_ARGV = ["-m", "RevenueChaser.scripts.migrate_sec13f_v12", "--derive-only"]
#: Where that module is importable from: the directory holding the ``RevenueChaser`` package.
PACKAGE_PARENT = Path(__file__).resolve().parents[2]


def derive_command() -> str:
    """The rebuild command a human can paste, in the form the other store scripts document."""
    return "python " + " ".join(DERIVE_ARGV)


def derive_in_a_fresh_process(out=print) -> None:
    """Run the derive in a NEW process, and raise ``DeriveSubprocessFailed`` if it fails.

    Measured on the real store (21.2M holdings rows): calling ``recompute_totals()`` in THIS
    process, straight after the re-ingest, dies with
    ``duckdb.OutOfMemoryException: failed to offload data block ... (4.3 GiB/4.3 GiB used)`` out
    of the VALUE-units median, which is the heaviest spill in the derive. The ingest has already
    spent this connection's temp budget by then. A new process starts with a clean one and the
    same derive finishes in about seven minutes, so the fix is a process boundary, not a PRAGMA:
    the 4.3 GiB ceiling appeared with 45 GB of disk free and is not a number worth tuning blind.

    The child is the same entry point as the documented rebuild command, so a failure here leaves
    the operator one pasteable line rather than a traceback — the re-ingest is already committed
    and only the derive has to be repeated.
    """
    out(f"deriving in a fresh process: {derive_command()}")
    completed = subprocess.run([sys.executable, *DERIVE_ARGV], cwd=str(PACKAGE_PARENT), check=False)
    if completed.returncode != 0:
        raise DeriveSubprocessFailed(
            f"the re-ingest is committed but the derive failed (exit {completed.returncode}); "
            f"the stored totals are still the old ones. Run it again with:\n    {derive_command()}")


def run(*, dry_run: bool = False, force: bool = False, ownership_dir: Path | None = None,
        out=print) -> dict:
    """Preflight, then the reset + re-ingest + derive. Returns ``{"before", "after", "dry_run"}``."""
    ownership_dir = Path(ownership_dir or engine.OWNERSHIP_DIR)
    before = preflight(ownership_dir, force=force)
    _report("before", before, out)
    if dry_run:
        out("dry run: nothing written")
        return {"before": before, "after": None, "dry_run": True}
    reset_holdings_and_markers()
    zips = sorted(ownership_dir.glob("*_form13f.zip"))
    out(f"re-ingesting {len(zips)} zip(s)")
    engine.ingest_zips(zips)
    derive_in_a_fresh_process(out)
    after = _counts()
    after["zip_markers"] = len(rawstore.query("SELECT filename FROM sec13f_ingested_zips"))
    _report("after", after, out)
    return {"before": before, "after": after, "dry_run": False}


def _report(label: str, counts: dict, out=print) -> None:
    out(f"13F v12 migration — {label}")
    out(f"  holdings rows         {counts['holdings']:,}")
    out(f"  submission rows       {counts['submissions']:,}")
    out(f"  distinct (cik,period) {counts['filer_periods']:,}")
    if "zips_on_disk" in counts:      # before: the files a re-ingest reads
        out(f"  zips on disk          {counts['zips_on_disk']}")
    if "zip_markers" in counts:       # after: the rows a later Fetch skips downloading
        out(f"  ingested zip markers  {counts['zip_markers']}")
    for period, n in sorted((counts.get("holders") or {}).items()):
        out(f"  holders {period}      {n:,}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="preflight only; write nothing")
    parser.add_argument("--force-reingest", action="store_true",
                        help="rebuild even when sec13f_holdings is already at the v12 key")
    parser.add_argument("--derive-only", action="store_true",
                        help="rebuild the totals from what is already ingested and exit; "
                             "the migration runs this in a fresh process, and it is the command "
                             "to repeat by hand if that child fails")
    args = parser.parse_args(argv)
    if args.derive_only:
        engine.recompute_totals()
        return EXIT_OK
    try:
        run(dry_run=args.dry_run, force=args.force_reingest)
    except MigrationRefused as exc:
        print(f"refused: {exc}")
        return EXIT_REFUSED
    except DeriveSubprocessFailed as exc:
        print(f"derive failed: {exc}")
        return EXIT_DERIVE_FAILED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
