"""One build per kind per pass (PR A, review of 7 Nov 2026): the queue takes every queued request of a kind when that
kind's turn comes, fetches each, and builds the kind's serving file once.

The datasets here are fakes registered over ``DATASETS``: they send no call and build one one-row table each, so a
test counts builds and reads the check rows without a provider or a Parquet tree."""
from __future__ import annotations

from datetime import date

import pytest

from RevenueChaser.rcdata import ops, serving
from RevenueChaser.rcdata import build as build_pkg
from RevenueChaser.rcdata.build import __main__ as cli
from RevenueChaser.rcdata.build import base, due

AS_OF = date(2026, 11, 6)


class Fake:
    providers: tuple[str, ...] = ()

    def __init__(self, name: str, kind: str = "market", *, fail_fetch: bool = False, log: list | None = None):
        self.name, self.kind, self.fail_fetch, self.log = name, kind, fail_fetch, log if log is not None else []
        self.tables = (f"t_{name.replace('/', '_')}",)

    def default_as_of(self) -> date:
        return AS_OF

    def fetch(self, ctx, as_of):
        self.log.append(("fetch", self.name))
        if self.fail_fetch:
            raise base.SourceError(f"{self.name}: provider down")
        return []

    def to_parquet_all(self, ctx, raws):
        return []

    def build(self, ctx, con):
        self.log.append(("build", self.name))
        con.execute(f"CREATE OR REPLACE TABLE {self.tables[0]} AS SELECT 1 AS x")
        return {self.tables[0]: 1}

    def check(self, ctx, con):
        return {"dataset": self.name, "result": "changed", "rows": 1, "added": 1, "removed": 0}


@pytest.fixture
def fakes(monkeypatch):
    """Four market fakes and one spine fake, registered in a deliberately non-alphabetical order."""
    from RevenueChaser.core import config
    (base.parquet_root()).mkdir(parents=True, exist_ok=True)
    log: list = []
    reg = {n: Fake(n, log=log) for n in ("m/zeta", "m/alpha", "m/mid", "m/beta")}
    reg["s/one"] = Fake("s/one", kind="spine", log=log)
    monkeypatch.setattr(build_pkg, "DATASETS", reg)
    builds: list[str] = []
    real = serving.build

    def counted(kind, *a, **k):
        builds.append(kind)
        return real(kind, *a, **k)
    monkeypatch.setattr(serving, "build", counted)
    assert config.MARKET_DIR
    return reg, log, builds


def _status(rid: int) -> dict:
    return next(r for r in ops.rows("build_requests") if r["id"] == rid)


def test_four_requests_of_one_kind_write_the_file_once(fakes):
    reg, log, builds = fakes
    rids = [ops.enqueue(n, None, by="test") for n in ("m/beta", "m/alpha", "m/zeta", "m/mid")]
    assert cli._run_queue(None, once=True) == 0
    assert builds == ["market"]
    assert [_status(r)["status"] for r in rids] == ["done"] * 4
    assert {n: ops.latest_check(n)["result"] for n in ("m/zeta", "m/alpha", "m/mid", "m/beta")} == \
        dict.fromkeys(("m/zeta", "m/alpha", "m/mid", "m/beta"), "changed")
    # every fetch first, in registry order, then the one build of every dataset of the kind
    assert [e for e in log if e[0] == "fetch"] == [("fetch", n) for n in ("m/zeta", "m/alpha", "m/mid", "m/beta")]


def test_two_kinds_in_one_pass_are_two_builds(fakes):
    reg, log, builds = fakes
    for n in ("m/alpha", "s/one", "m/beta", "m/mid"):
        ops.enqueue(n, None, by="test")
    assert cli._run_queue(None, once=True) == 0
    assert sorted(builds) == ["market", "spine"]


def test_a_stage_only_request_alone_does_not_build(fakes):
    reg, log, builds = fakes
    rid = ops.enqueue("m/alpha", {"no_duckdb": True}, by="test")
    assert cli._run_queue(None, once=True) == 0
    assert builds == []
    assert _status(rid)["status"] == "skipped"
    assert ops.latest_check("m/alpha")["result"] == "skipped"


def test_a_stage_only_member_gets_a_real_check_when_its_kind_builds(fakes):
    """D-8 (owner, 8 Nov): its tables were rebuilt, so it gets the real row rather than a skip that leaves it due."""
    reg, log, builds = fakes
    stage = ops.enqueue("m/alpha", {"no_duckdb": True}, by="test")
    full = ops.enqueue("m/beta", None, by="test")
    assert cli._run_queue(None, once=True) == 0
    assert builds == ["market"]
    assert _status(stage)["status"] == "done" and _status(full)["status"] == "done"
    assert ops.latest_check("m/alpha")["result"] == "changed"


def test_a_failed_fetch_does_not_block_the_kinds_build(fakes):
    reg, log, builds = fakes
    reg["m/mid"].fail_fetch = True
    bad = ops.enqueue("m/mid", None, by="test")
    good = ops.enqueue("m/alpha", None, by="test")
    assert cli._run_queue(None, once=True) == 1
    assert builds == ["market"]
    assert _status(bad)["status"] == "failed" and ops.latest_check("m/mid")["result"] == "failed"
    assert _status(good)["status"] == "done" and ops.latest_check("m/alpha")["result"] == "changed"
    assert serving.live_path("market").exists()


def test_a_kind_whose_every_fetch_failed_is_not_built(fakes):
    """D-4 (owner, 8 Nov): nothing new arrived, so a build would only rewrite the same file."""
    reg, log, builds = fakes
    reg["m/mid"].fail_fetch = reg["m/alpha"].fail_fetch = True
    for n in ("m/mid", "m/alpha"):
        ops.enqueue(n, None, by="test")
    assert cli._run_queue(None, once=True) == 1
    assert builds == []
    assert [ops.latest_check(n)["result"] for n in ("m/mid", "m/alpha")] == ["failed", "failed"]


def test_a_request_named_on_the_command_line_runs_alone(fakes):
    reg, log, builds = fakes
    mine = ops.enqueue("m/alpha", None, by="test")
    other = ops.enqueue("m/beta", None, by="test")
    assert cli._run_queue("m/alpha", once=True) == 0
    assert _status(mine)["status"] == "done" and _status(other)["status"] == "queued"


@pytest.mark.parametrize("kill", [KeyboardInterrupt, SystemExit])
def test_a_kill_inside_a_batch_ends_every_request_it_holds(fakes, monkeypatch, kill):
    reg, log, builds = fakes
    rids = [ops.enqueue(n, None, by="test") for n in ("m/alpha", "m/beta", "m/mid")]

    def boom(ctx, con):
        raise kill()
    monkeypatch.setattr(reg["m/zeta"], "build", boom)        # the kind's first dataset in registry order
    with pytest.raises(kill):
        cli._run_queue(None, once=True)
    assert [_status(r)["status"] for r in rids] == ["failed"] * 3
    assert all(_status(r)["note"].startswith("killed:") for r in rids)
    assert {r["status"] for r in ops.rows("job_runs2")} == {"killed"}


def test_the_pass_logs_one_line_per_build(fakes, capsys):
    reg, log, builds = fakes
    for n in ("m/alpha", "m/beta"):
        ops.enqueue(n, None, by="test")
    assert cli._run_queue(None, once=True) == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("build kind=")]
    assert len(lines) == 1
    assert lines[0].startswith("build kind=market datasets=2/4 duration=") and " swap=" in lines[0]


def test_enqueue_due_walks_the_registry_order_not_the_alphabet(monkeypatch):
    reg = {n: Fake(n) for n in ("m/zeta", "m/alpha", "m/mid")}
    monkeypatch.setattr(due, "is_due", lambda ds, mode="full": (False, "test"))
    assert [row[0] for row in due.enqueue_due(reg)] == ["m/zeta", "m/alpha", "m/mid"]


def test_two_requests_of_one_dataset_are_not_claimed_into_one_batch(fakes):
    """Review 4: the batch keys by dataset, so a second request of a member's dataset waits for the next turn."""
    reg, log, builds = fakes
    lead = ops.enqueue("m/alpha", None, by="test")
    first = ops.enqueue("m/beta", None, by="test")
    second = ops.enqueue("m/beta", {"as_of": "2026-11-05"}, by="test")
    assert cli._run_queue(None, once=True) == 0
    assert [_status(r)["status"] for r in (lead, first, second)] == ["done"] * 3
    assert builds == ["market", "market"]


def test_a_busy_store_while_claiming_does_not_end_the_builder(fakes, monkeypatch):
    """Review 2: the claim stops at a locked ops store and the batch claimed so far runs."""
    import sqlite3
    reg, log, builds = fakes
    rids = [ops.enqueue(n, None, by="test") for n in ("m/alpha", "m/beta")]
    real, calls = ops.next_request, []

    def flaky(dataset=None, **kw):
        calls.append(kw.get("only"))
        if kw.get("only") is not None:
            raise sqlite3.OperationalError("database is locked")
        return real(dataset, **kw)
    monkeypatch.setattr(ops, "next_request", flaky)
    assert cli._run_queue(None, once=True) == 0         # before the fix: the error ended the builder
    assert [_status(r)["status"] for r in rids] == ["done", "done"]
    assert builds == ["market", "market"]               # not claimed together: one turn each


def test_a_busy_store_mid_batch_leaves_the_requests_that_ended_as_they_are(fakes, monkeypatch):
    """Review 1: only the requests the batch had not ended are failed as "ops store busy"."""
    import sqlite3
    reg, log, builds = fakes
    rids = [ops.enqueue(n, None, by="test") for n in ("m/alpha", "m/beta")]
    real, seen = ops.record_check, []

    def flaky(dataset, result, **kw):
        seen.append(dataset)
        if dataset == "m/beta":
            raise sqlite3.OperationalError("database is locked")
        return real(dataset, result, **kw)
    monkeypatch.setattr(ops, "record_check", flaky)
    monkeypatch.setattr(cli, "POLL_S", 0)
    assert cli._run_queue(None, once=True) == 1
    assert _status(rids[0])["status"] == "done"
    assert _status(rids[1])["status"] == "failed" and "ops store busy" in _status(rids[1])["note"]


def test_each_fetch_writes_its_calls_before_the_next_member_fetches(fakes, monkeypatch):
    """Review 3: a member's day-cap read sees the calls the members before it sent."""
    reg, log, builds = fakes
    seen = []

    def fetch_counting(self, ctx, as_of):
        seen.append(ops.calls_today("fakeprov"))
        ctx.count("fakeprov", calls=1)
        return []
    monkeypatch.setattr(Fake, "fetch", fetch_counting)
    for n in ("m/alpha", "m/beta"):
        ops.enqueue(n, None, by="test")
    assert cli._run_queue(None, once=True) == 0
    assert seen == [0, 1]
