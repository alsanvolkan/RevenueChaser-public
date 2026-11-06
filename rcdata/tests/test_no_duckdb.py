"""``--no-duckdb``: fetch and stage Parquet, leave the serving file alone (#446, renamed in #498).

``--no-build`` is the deprecated alias of this flag and is covered here too: a request queued before the rename
carries ``no_build`` and must keep working.

For staging days of data without holding the builder lock through a build each time — the fetch campaign of a kind
that has never been built, or preparing the data here and building it elsewhere. The request ends ``skipped``
rather than carrying a new result value into ``due.py`` and ops, because the serving file really did not change;
the dataset stays due and the next ordinary pass builds it at no provider call (the raw file it staged is the
resume marker).
"""
from __future__ import annotations

import contextlib
import hashlib
from datetime import date
from pathlib import Path

import pytest

from RevenueChaser.rcdata import ops, serving
from RevenueChaser.rcdata.build import DATASETS, base, due
from RevenueChaser.rcdata.build import __main__ as cli


class _Stager:
    """A dataset that fetches nothing but records every step its run took."""

    name = "test/no_duckdb"
    kind = "market"
    providers = ()
    tables = ()
    cadence = "daily"
    steps: list[str] = []

    def default_as_of(self):
        return date(2026, 10, 28)

    def fetch(self, ctx, as_of):
        type(self).steps.append("fetch")
        return [Path("raw-file")]

    def to_parquet_all(self, ctx, raws):
        type(self).steps.append("to_parquet_all")
        return []

    def backfill(self, ctx, start, end):
        type(self).steps.append("backfill")

    def build(self, ctx, con):
        return {}

    def check(self, ctx, con):
        return {"dataset": self.name, "result": "changed"}


@pytest.fixture()
def stager(monkeypatch):
    ds = _Stager()
    type(ds).steps = []
    monkeypatch.setitem(DATASETS, ds.name, ds)
    built = []
    monkeypatch.setattr(base, "build_kind", lambda ctx, d: built.append(d.name) or d.check(ctx, None))
    ds.built = built
    return ds


def _live_market(rows: int = 2) -> str:
    with serving.build("market") as b:
        b.con.execute("CREATE TABLE t (i INTEGER)")
        b.con.execute(f"INSERT INTO t SELECT range FROM range({rows})")
        b.swap({"t": rows})
    return hashlib.sha256(serving.live_path("market").read_bytes()).hexdigest()


def test_the_note_says_how_many_raw_files_the_fetch_staged(stager):
    """The first live run said "raw and Parquet staged" while its fetch had returned nothing (the dataset's
    window was closed), which reads as a success it was not."""
    row = base.run_request({"dataset": stager.name, "params": {"no_duckdb": True}})
    assert "1 raw file staged" in (row["note"] or ""), row["note"]


def test_the_note_says_so_when_the_fetch_staged_nothing(stager, monkeypatch):
    monkeypatch.setattr(type(stager), "fetch", lambda self, ctx, as_of: [])
    row = base.run_request({"dataset": stager.name, "params": {"no_duckdb": True}})
    assert "nothing staged" in (row["note"] or ""), row["note"]


def test_a_backfill_note_claims_no_count(stager):
    """``backfill`` stages inside the dataset and returns nothing, so the note counts nothing either."""
    row = base.run_request({"dataset": stager.name,
                            "params": {"mode": "backfill", "from": "2026-10-01", "no_duckdb": True}})
    assert "--no-duckdb" in (row["note"] or "") and "file" not in (row["note"] or ""), row["note"]


def test_a_no_duckdb_run_stages_the_data_and_leaves_the_serving_file_alone(stager):
    before = _live_market()
    row = base.run_request({"dataset": stager.name, "params": {"no_duckdb": True}})
    assert row["result"] == "skipped" and "--no-duckdb" in (row["note"] or "")
    assert stager.built == [], "no build was asked for"
    assert hashlib.sha256(serving.live_path("market").read_bytes()).hexdigest() == before


def test_the_fetch_and_the_parquet_step_still_run(stager):
    base.run_request({"dataset": stager.name, "params": {"no_duckdb": True}})
    assert stager.steps == ["fetch", "to_parquet_all"], "the point of the switch is that the data is staged"


def test_without_the_switch_the_kind_is_built_as_before(stager):
    row = base.run_request({"dataset": stager.name, "params": {}})
    assert row["result"] == "changed" and stager.built == [stager.name]


def test_the_dataset_stays_due_after_a_no_duckdb_run(stager):
    base.run_request({"dataset": stager.name, "params": {"no_duckdb": True}})
    is_due, why = due.is_due(stager)
    assert is_due and "skipped" in why, "the data is staged but nothing serves it yet"


@pytest.mark.usefixtures("kinds_full")
def test_the_hourly_pass_never_asks_for_no_duckdb(stager):
    """The switch is a person's: the schedule queues no params at all, so it cannot leave a kind unbuilt."""
    [(name, why, rid, error)] = due.enqueue_due({stager.name: stager})
    assert rid and not error, why
    req = next(r for r in ops.rows("build_requests") if r["id"] == rid)
    assert not (req["params"] or {}).get("no_duckdb")


def test_a_backfill_takes_the_switch_too(stager):
    row = base.run_request({"dataset": stager.name,
                            "params": {"mode": "backfill", "from": "2026-10-01", "no_duckdb": True}})
    assert row["result"] == "skipped" and stager.steps == ["backfill"] and stager.built == []


def test_the_switch_needs_a_dataset_on_the_hourly_command(capsys):
    """Without --dataset it would shape nothing and be dropped silently, as the other request flags are (F5).

    The message is checked, not only the exit: a run that refused the flag because it does not know it would
    raise ``SystemExit`` too, and this test would pass while the switch did not exist.
    """
    with pytest.raises(SystemExit):
        cli.main(["run", "--once", "--no-duckdb"])
    assert "need --dataset" in capsys.readouterr().err


# ── The deprecated alias (#498) ───────────────────────────────────────────────────────────────────────────────

def test_the_old_no_build_param_still_skips_the_build(stager):
    """A request queued before the rename carries ``no_build``: it is read as ``no_duckdb``."""
    row = base.run_request({"dataset": stager.name, "params": {"no_build": True}})
    assert row["result"] == "skipped" and "--no-duckdb" in (row["note"] or ""), row["note"]
    assert stager.built == [] and stager.steps == ["fetch", "to_parquet_all"]


def test_the_old_no_build_flag_queues_the_new_param(stager, monkeypatch):
    """The CLI translates the alias once, so every check reads one name; the request carries both, because
    ops.sqlite outlives a rollback to a builder that knows only the old one (review SHOULD 2)."""
    seen: list = []

    @contextlib.contextmanager
    def always(name, wait=0.0, **kw):
        yield True
    monkeypatch.setattr(cli.job_lock, "acquire", always)
    monkeypatch.setattr(cli.base, "run_request", lambda req, **kw: seen.append(req) or {
        "dataset": req["dataset"], "result": "skipped", "source_as_of": None, "rows": 0, "added": 0,
        "removed": 0, "calls": 0, "note": None})
    assert cli.main(["backfill", stager.name, "--from", "2026-10-01", "--no-build"]) == 0
    assert seen and seen[-1]["params"].get("no_duckdb") is True, seen
    assert seen[-1]["params"].get("no_build") is True, seen[-1]["params"]
