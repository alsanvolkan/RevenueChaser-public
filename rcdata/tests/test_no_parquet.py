"""``--no-parquet``: the raw-only mode (#498).

Before this, ``--no-build`` was the only "do not build" switch and it wrote Parquet as well as raw, so a fetch
campaign had no way to ask for the provider's answers alone. ``--no-parquet`` stages raw files and nothing else,
and implies ``--no-duckdb``, because Parquet is the only input a build reads: a build with no new partition would
be a no-op that still took the builder lock.

The guard sits in ``base.write_parquet`` (the one place a Parquet file is written) and is run-scoped, because a
dataset's own ``backfill`` converts without the ``RunContext`` in hand.
"""
from __future__ import annotations

import contextlib
import hashlib
from datetime import date
from pathlib import Path

import duckdb
import pytest

from RevenueChaser.rcdata import ops, serving
from RevenueChaser.rcdata.build import DATASETS, base, due
from RevenueChaser.rcdata.build import __main__ as cli


class _Stager:
    """A dataset that writes one Parquet partition through ``base.write_parquet``, in each mode."""

    name = "test/no_parquet"
    kind = "market"
    providers = ()
    tables = ()
    cadence = "daily"
    steps: list[str] = []
    written: list[Path] = []

    def __init__(self, root: Path):
        self.root = root

    def _write(self, ctx) -> Path:
        con = duckdb.connect()
        try:
            con.execute("CREATE TABLE t AS SELECT 1 AS i")
            out = base.write_parquet(con, "SELECT * FROM t", self.root / "part-0.parquet")
        finally:
            con.close()
        type(self).written.append(out)
        return out

    def default_as_of(self):
        return date(2026, 10, 28)

    def fetch(self, ctx, as_of):
        type(self).steps.append("fetch")
        return [Path("raw-file")]

    def to_parquet_all(self, ctx, raws):
        type(self).steps.append("to_parquet_all")
        return [self._write(ctx)]

    def backfill(self, ctx, start, end):
        type(self).steps.append("backfill")
        self._write(ctx)

    def build(self, ctx, con):
        return {}

    def check(self, ctx, con):
        return {"dataset": self.name, "result": "changed"}


@pytest.fixture()
def stager(monkeypatch, tmp_path):
    ds = _Stager(tmp_path / "parquet" / "test")
    type(ds).steps = []
    type(ds).written = []
    monkeypatch.setitem(DATASETS, ds.name, ds)
    built: list[str] = []
    monkeypatch.setattr(base, "build_kind", lambda ctx, d: built.append(d.name) or d.check(ctx, None))
    ds.built = built
    return ds


def _live_market(rows: int = 2) -> str:
    with serving.build("market") as b:
        b.con.execute("CREATE TABLE t (i INTEGER)")
        b.con.execute(f"INSERT INTO t SELECT range FROM range({rows})")
        b.swap({"t": rows})
    return hashlib.sha256(serving.live_path("market").read_bytes()).hexdigest()


# ── The daily mode ─────────────────────────────────────────────────────────────────────────────────────────────

def test_a_raw_only_run_never_asks_the_dataset_to_convert(stager):
    """The daily path skips the call outright, so the conversion work is not spent either."""
    row = base.run_request({"dataset": stager.name, "params": {"no_parquet": True}})
    assert row["result"] == "skipped"
    assert stager.steps == ["fetch"], stager.steps
    assert stager.written == [] and stager.built == []


def test_the_note_names_the_flag_and_counts_the_raw_files(stager):
    row = base.run_request({"dataset": stager.name, "params": {"no_parquet": True}})
    assert "--no-parquet" in (row["note"] or "") and "1 raw file staged" in (row["note"] or ""), row["note"]


def test_a_raw_only_run_leaves_the_serving_file_alone(stager):
    before = _live_market()
    base.run_request({"dataset": stager.name, "params": {"no_parquet": True}})
    assert hashlib.sha256(serving.live_path("market").read_bytes()).hexdigest() == before


def test_the_dataset_stays_due_after_a_raw_only_run(stager):
    base.run_request({"dataset": stager.name, "params": {"no_parquet": True}})
    is_due, why = due.is_due(stager)
    assert is_due and "skipped" in why, "the raw files are there but nothing serves them yet"


# ── The backfill mode: the dataset converts inside itself, so the guard must be run-scoped ────────────────────

def test_a_raw_only_backfill_writes_no_partition(stager):
    """``market_series.backfill`` and the fundamentals backfills call ``to_parquet_all`` themselves."""
    row = base.run_request({"dataset": stager.name,
                            "params": {"mode": "backfill", "from": "2026-10-01", "no_parquet": True}})
    assert row["result"] == "skipped" and stager.steps == ["backfill"] and stager.built == []
    assert [p for p in stager.written if p.exists()] == [], "no Parquet file may be written"


def test_an_ordinary_backfill_still_writes_its_partition(stager):
    base.run_request({"dataset": stager.name, "params": {"mode": "backfill", "from": "2026-10-01"}})
    assert stager.written and stager.written[0].is_file(), "the guard must only hold in a raw-only run"


def test_the_guard_is_released_when_the_run_ends(stager):
    base.run_request({"dataset": stager.name,
                      "params": {"mode": "backfill", "from": "2026-10-01", "no_parquet": True}})
    assert base.raw_only() is False
    base.run_request({"dataset": stager.name, "params": {"mode": "backfill", "from": "2026-10-01"}})
    assert stager.written[-1].is_file()


def test_the_guard_is_released_after_a_failed_run(stager, monkeypatch):
    monkeypatch.setattr(type(stager), "backfill",
                        lambda self, ctx, start, end: (_ for _ in ()).throw(RuntimeError("boom")))
    row = base.run_request({"dataset": stager.name,
                            "params": {"mode": "backfill", "from": "2026-10-01", "no_parquet": True}})
    assert row["result"] == "failed" and base.raw_only() is False


def test_write_parquet_returns_the_path_it_did_not_write():
    """Callers append the result to a list they return; in a raw-only run the file is simply not there."""
    con = duckdb.connect()
    try:
        con.execute("CREATE TABLE t AS SELECT 1 AS i")
        with base.raw_only_run():
            out = base.write_parquet(con, "SELECT * FROM t", Path("/nonexistent/part-0.parquet"))
    finally:
        con.close()
    assert out == Path("/nonexistent/part-0.parquet") and not out.exists()


# ── The CLI ────────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_flag_implies_no_duckdb_in_the_request(stager, monkeypatch):
    seen: list = []

    @contextlib.contextmanager
    def always(name, wait=0.0, **kw):
        yield True
    monkeypatch.setattr(cli.job_lock, "acquire", always)
    monkeypatch.setattr(cli.base, "run_request", lambda req, **kw: seen.append(req) or {
        "dataset": req["dataset"], "result": "skipped", "source_as_of": None, "rows": 0, "added": 0,
        "removed": 0, "calls": 0, "note": None})
    assert cli.main(["backfill", stager.name, "--from", "2026-10-01", "--no-parquet"]) == 0
    assert seen[-1]["params"].get("no_parquet") is True and seen[-1]["params"].get("no_duckdb") is True, seen


def test_a_raw_only_run_is_fetch_only_for_the_locks(monkeypatch):
    """It opens no serving file, so it takes one ``rcdata_fetch_<dataset>`` lock and never the builder's."""
    taken: list = []

    @contextlib.contextmanager
    def spy(name, wait=0.0, **kw):
        taken.append(name)
        yield True
    monkeypatch.setattr(cli.job_lock, "acquire", spy)
    monkeypatch.setattr(cli.base, "idle_problem", lambda: pytest.fail("the gate was asked for a raw-only run"))
    monkeypatch.setattr(cli.base, "run_request", lambda req, **kw: {
        "dataset": req["dataset"], "result": "skipped", "source_as_of": None, "rows": 0, "added": 0,
        "removed": 0, "calls": 0, "note": None})
    assert cli.main(["backfill", "market_series", "--from", "2026-10-01", "--no-parquet"]) == 0
    assert taken == ["rcdata_fetch_market_series"] and cli.base.BUILDER_LOCK not in taken


def test_the_flag_needs_a_dataset_on_the_hourly_command(capsys):
    with pytest.raises(SystemExit):
        cli.main(["run", "--once", "--no-parquet"])
    assert "need --dataset" in capsys.readouterr().err


def test_the_hourly_pass_never_asks_for_raw_only(stager):
    [(name, why, rid, error)] = due.enqueue_due({stager.name: stager})
    assert rid and not error, why
    req = next(r for r in ops.rows("build_requests") if r["id"] == rid)
    assert not (req["params"] or {}).get("no_parquet")
