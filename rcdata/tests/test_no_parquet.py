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
from RevenueChaser.rcdata.build.ownership import edgar, sec13f_sets


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


def test_write_parquet_returns_the_path_it_did_not_write(tmp_path):
    """Callers append the result to a list they return; in a raw-only run the file is simply not there.

    The target is under ``tmp_path`` and its parent does not exist either, so the guard is also shown to skip the
    ``mkdir`` that ``write_parquet`` does before writing.
    """
    final = tmp_path / "never" / "part-0.parquet"
    con = duckdb.connect()
    try:
        con.execute("CREATE TABLE t AS SELECT 1 AS i")
        with base.raw_only_run():
            out = base.write_parquet(con, "SELECT * FROM t", final)
    finally:
        con.close()
    assert out == final and not out.exists() and not final.parent.exists()


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


# ── A real dataset whose conversion step also writes a resume marker (review BLOCKER 1) ───────────────────────

class _Feed(edgar.Feed):
    """The smallest ``edgar.Feed``: its ``type_day`` writes one partition through ``base.write_parquet``.

    The EDGAR feeds (``sec/13f``, ``sec/form4``, ``sec/sched13dg``) write the day's ``.done`` marker in
    ``to_parquet_all``, beside the typing, and ``.done`` is what the walk skips on. A raw-only run that wrote the
    marker without the partition would hide that day's filings from every later run.
    """

    name = "test/feed"
    forms = frozenset({"13F-HR"})
    sub = "feedtest"
    budget_s = None

    def type_day(self, ctx, day):
        con = duckdb.connect()
        try:
            con.execute("CREATE TABLE filings AS SELECT 1 AS i")
            return [base.write_parquet(con, "SELECT * FROM filings",
                                       self.parquet_dir() / "filings" / f"day={day}" / "part-0.parquet")]
        finally:
            con.close()


def _feed_day(feed: _Feed, day: date) -> Path:
    d = feed.day_dir(day)
    d.mkdir(parents=True, exist_ok=True)
    (d / "0000000000-26-000001.txt.gz").write_bytes(b"")
    return d


def test_a_raw_only_run_does_not_mark_an_edgar_day_done():
    """Else the day is never walked again: its filings sit in raw, unreachable, and only a hand-deleted marker
    brings them back."""
    feed, day = _Feed(), date(2026, 10, 1)
    _feed_day(feed, day)
    ctx = base.RunContext(run_id="r1", dataset=feed.name)
    ctx.pending[feed.name] = {"complete": [day], "more": False}
    with base.raw_only_run():
        out = feed.to_parquet_all(ctx, [feed.day_dir(day)])
    assert out == [], out
    assert not feed.is_done(day), "a raw-only run must leave the day open for the next walk"
    assert not (feed.parquet_dir() / "filings" / f"day={day}").exists()


def test_an_ordinary_run_still_marks_the_edgar_day_done():
    feed, day = _Feed(), date(2026, 10, 2)
    _feed_day(feed, day)
    ctx = base.RunContext(run_id="r2", dataset=feed.name)
    ctx.pending[feed.name] = {"complete": [day], "more": False}
    out = feed.to_parquet_all(ctx, [feed.day_dir(day)])
    assert feed.is_done(day) and out and out[0].is_file(), out


def test_a_raw_only_run_does_not_mark_the_13f_data_set_days_done(monkeypatch, tmp_path):
    """``sec13f_sets._fill_one`` marks every day of the set's range, covered or not, so a raw-only run of
    ``backfill sec13f`` would burn the whole range."""
    feed = _Feed()
    zip_path = tmp_path / "set.zip"
    zip_path.write_bytes(b"not read: the raw-only guard returns before the zip is opened")
    monkeypatch.setattr(sec13f_sets, "_load",
                        lambda *a, **kw: pytest.fail("a raw-only run must not type the set"))
    ctx = base.RunContext(run_id="r3", dataset=feed.name)
    with base.raw_only_run():
        written = sec13f_sets._fill_one(ctx, feed, zip_path, "2026q3", date(2026, 7, 1), date(2026, 7, 31),
                                        {"forms": ("13F-HR",), "filings": (), "holdings": (), "dollars_from": None})
    assert written == 0
    assert not any(feed.is_done(date(2026, 7, d)) for d in range(1, 32)), "no day may be marked"
    assert any("raw only" in n for n in ctx.notes), ctx.notes


# ── The request params a rollback would meet (review SHOULD 2) and the other CLI paths ────────────────────────

def _queue_spy(monkeypatch):
    seen: list = []

    @contextlib.contextmanager
    def always(name, wait=0.0, **kw):
        yield True
    monkeypatch.setattr(cli.job_lock, "acquire", always)
    monkeypatch.setattr(cli.base, "run_request", lambda req, **kw: seen.append(req) or {
        "dataset": req["dataset"], "result": "skipped", "source_as_of": None, "rows": 0, "added": 0,
        "removed": 0, "calls": 0, "note": None})
    return seen


def test_a_queued_request_carries_the_old_param_name_too(stager, monkeypatch):
    """ops.sqlite outlives a rollback: a builder from before the rename reads ``no_build`` and nothing else, and
    without it that builder would build the serving file the person asked it not to touch."""
    seen = _queue_spy(monkeypatch)
    assert cli.main(["backfill", stager.name, "--from", "2026-10-01", "--no-parquet"]) == 0
    assert seen[-1]["params"].get("no_build") is True, seen[-1]["params"]


def test_the_run_dataset_path_queues_the_flag_too(stager, monkeypatch):
    """``--no-parquet`` on ``run --dataset`` goes through a different params branch than ``backfill``."""
    seen = _queue_spy(monkeypatch)
    assert cli.main(["run", "--once", "--dataset", stager.name, "--no-parquet"]) == 0
    params = seen[-1]["params"]
    assert params.get("no_parquet") is True and params.get("no_duckdb") is True and params.get("no_build") is True


def test_both_flags_together_are_raw_only(stager, monkeypatch):
    seen = _queue_spy(monkeypatch)
    assert cli.main(["backfill", stager.name, "--from", "2026-10-01", "--no-duckdb", "--no-parquet"]) == 0
    assert seen[-1]["params"].get("no_parquet") is True and seen[-1]["params"].get("no_duckdb") is True


def test_a_request_with_both_names_is_read_as_raw_only(stager):
    """``no_build`` (the old name) beside ``no_parquet``: the raw-only reading wins, so nothing is converted."""
    row = base.run_request({"dataset": stager.name, "params": {"no_build": True, "no_parquet": True}})
    assert row["result"] == "skipped" and "--no-parquet" in (row["note"] or ""), row["note"]
    assert stager.steps == ["fetch"] and stager.written == [] and stager.built == []


def test_the_deprecated_flag_says_so_on_stderr(stager, monkeypatch, capsys):
    _queue_spy(monkeypatch)
    assert cli.main(["backfill", stager.name, "--from", "2026-10-01", "--no-build"]) == 0
    assert "--no-build is deprecated" in capsys.readouterr().err
