"""One heavy dataset per batch and per builder process (#812).

On homesrv (15.4 GB RAM, swap full) the hourly pass fetched ``sec/companyfacts`` (6.4-6.8 GB resident) in the same
batch as ``fundamentals/tables``, and the kernel OOM-killed the builder twice on 12 Nov 2026
(``constraint=CONSTRAINT_NONE``: the whole machine, so the unit's ``MemoryMax`` was no protection). The rule here is
the fix: a batch claims at most one heavy dataset, and once a process has run one, every other heavy request stays
queued for the next pass, which starts with this process's memory given back.

The datasets are the fakes of ``test_build_batch.py``: they send no call and build one one-row table each.
"""
from __future__ import annotations

from datetime import date

import pytest

from RevenueChaser.rcdata import ops, serving
from RevenueChaser.rcdata import build as build_pkg
from RevenueChaser.rcdata.build import __main__ as cli
from RevenueChaser.rcdata.build import base

AS_OF = date(2026, 11, 6)


class Fake:
    providers: tuple[str, ...] = ()

    def __init__(self, name: str, kind: str = "market", *, heavy: bool = False, log: list | None = None):
        self.name, self.kind, self.log = name, kind, log if log is not None else []
        self.heavy = heavy
        self.tables = (f"t_{name.replace('/', '_')}",)

    def default_as_of(self) -> date:
        return AS_OF

    def fetch(self, ctx, as_of):
        self.log.append(self.name)
        return []

    def to_parquet_all(self, ctx, raws):
        return []

    def build(self, ctx, con):
        con.execute(f"CREATE OR REPLACE TABLE {self.tables[0]} AS SELECT 1 AS x")
        return {self.tables[0]: 1}

    def check(self, ctx, con):
        return {"dataset": self.name, "result": "changed", "rows": 1, "added": 1, "removed": 0}


@pytest.fixture
def fakes(monkeypatch):
    """Two heavy datasets in the ``market`` kind, one heavy in ``spine``, and two light ones beside them."""
    (base.parquet_root()).mkdir(parents=True, exist_ok=True)
    log: list = []
    reg = {"m/heavy1": Fake("m/heavy1", heavy=True, log=log),
           "m/light1": Fake("m/light1", log=log),
           "m/heavy2": Fake("m/heavy2", heavy=True, log=log),
           "s/heavy3": Fake("s/heavy3", kind="spine", heavy=True, log=log),
           "s/light2": Fake("s/light2", kind="spine", log=log)}
    monkeypatch.setattr(build_pkg, "DATASETS", reg)
    builds: list[str] = []
    real = serving.build

    def counted(kind, *a, **k):
        builds.append(kind)
        return real(kind, *a, **k)
    monkeypatch.setattr(serving, "build", counted)
    return reg, log, builds


def _status(rid: int) -> str:
    return next(r for r in ops.rows("build_requests") if r["id"] == rid)["status"]


def test_heavy_reads_the_flag_and_names_the_heavy_datasets(fakes):
    reg, _, _ = fakes
    assert base.heavy(reg["m/heavy1"]) is True
    assert base.heavy(reg["m/light1"]) is False
    assert base.heavy(None) is False                      # an unknown dataset is not heavy
    assert base.heavy_names() == {"m/heavy1", "m/heavy2", "s/heavy3"}


def test_the_measured_datasets_are_marked_heavy():
    """The three runs measured on homesrv on 12 Nov 2026 carry the flag, so the rule reaches the real registry."""
    from RevenueChaser.rcdata.build import DATASETS
    assert {"sec/companyfacts", "fundamentals/tables", "ownership/serve"} <= base.heavy_names()
    assert not base.heavy(DATASETS["market_series"])      # a light dataset is untouched


def test_two_heavy_datasets_of_one_kind_are_not_claimed_into_one_batch(fakes):
    """The OOM itself: both ran in one process, fetch after fetch, and their gigabytes added up."""
    reg, log, builds = fakes
    first = ops.enqueue("m/heavy1", None, by="test")
    second = ops.enqueue("m/heavy2", None, by="test")
    light = ops.enqueue("m/light1", None, by="test")
    assert cli._run_queue(None, once=True) == 0
    assert log == ["m/heavy1", "m/light1"]                # the light member still rides along
    assert _status(first) == "done" and _status(light) == "done"
    assert _status(second) == "queued"                    # left for the next pass
    assert builds == ["market"]


def test_one_heavy_dataset_per_process_leaves_the_other_kinds_heavy_request_queued(fakes):
    """A second heavy dataset of another kind would otherwise be the very next turn of the same process."""
    reg, log, builds = fakes
    heavy = ops.enqueue("m/heavy1", None, by="test")
    other = ops.enqueue("s/heavy3", None, by="test")
    light = ops.enqueue("s/light2", None, by="test")
    assert cli._run_queue(None, once=True) == 0
    assert log == ["m/heavy1", "s/light2"]
    assert _status(heavy) == "done" and _status(light) == "done"
    assert _status(other) == "queued"
    assert builds == ["market", "spine"]                  # the light one's kind is still built


def test_the_pass_says_which_heavy_dataset_it_ran_and_says_nothing_for_a_light_one(fakes, capsys):
    reg, log, builds = fakes
    ops.enqueue("m/heavy1", None, by="test")
    assert cli._run_queue(None, once=True) == 0
    said = [ln for ln in capsys.readouterr().err.splitlines() if "one heavy dataset per pass" in ln]
    assert len(said) == 1 and "m/heavy1" in said[0]
    ops.enqueue("m/light1", None, by="test")
    assert cli._run_queue(None, once=True) == 0
    assert "one heavy dataset per pass" not in capsys.readouterr().err


def test_a_dataset_named_on_the_command_line_runs_however_heavy_it_is(fakes):
    """``run --dataset X`` and ``backfill`` are a person's own run: the rule is the pass's, not theirs."""
    reg, log, builds = fakes
    first = ops.enqueue("m/heavy1", None, by="test")
    second = ops.enqueue("m/heavy2", None, by="test")
    assert cli._run_queue(None, once=True) == 0
    assert _status(first) == "done" and _status(second) == "queued"
    assert cli._run_queue("m/heavy2", once=True) == 0      # the same process, asked for by name
    assert _status(second) == "done"


def test_the_attended_runner_is_not_held_off_by_the_heavy_rule(fakes):
    """``attended`` claims with ``only=``, which ``next_request`` cannot take beside ``skip=``; nothing attended
    is heavy, so the rule must not reach that path."""
    reg, log, builds = fakes
    reg["m/light1"].attended = True
    heavy = ops.enqueue("m/heavy1", None, by="test")
    second = ops.enqueue("m/heavy2", None, by="test")
    assert cli._run_queue(None, once=True) == 0             # a heavy pass, with the light one left to its runner
    assert _status(heavy) == "done" and _status(second) == "queued"
    rid = ops.enqueue("m/light1", None, by="test")
    assert cli._run_queue(None, once=True, only={"m/light1"}, attended_ok=True) == 0
    assert _status(rid) == "done"
