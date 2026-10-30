"""What the spine (PR 5) adds to the builder's shared parts: the ``spine`` serving kind, the build-issue sync in
``ops.sqlite`` (D6), attended datasets (WRDS: a person logs in), the keyless POST helper, the after-swap step, and the
CLI's backfill of several datasets in one process (one WRDS connection)."""
from __future__ import annotations

from datetime import date

import pytest

from RevenueChaser.rcdata import ops, serving
from RevenueChaser.rcdata.build import base, due


class _Fake:
    """A dataset of the ``spine`` kind with one table; records what the builder asked of it."""

    kind = "spine"
    providers = ("fake",)
    def __init__(self, name="fake/one", attended=False):
        self.name, self.attended = name, attended
        self.table = "fake_" + name.split("/")[1]
        self.tables = (self.table,)
        self.fetched, self.after = [], []
        self.backfill_needs_from = False

    def default_as_of(self):
        return date(2026, 10, 27)

    def fetch(self, ctx, as_of):
        self.fetched.append(as_of)
        return []

    def to_parquet_all(self, ctx, raws):
        return []

    def backfill(self, ctx, start, end):
        self.fetched.append(("backfill", start, end))

    def build(self, ctx, con):
        con.execute(f"CREATE TABLE {self.table} (k INTEGER)")
        con.execute(f"INSERT INTO {self.table} VALUES (1)")
        return {self.table: 1}

    def check(self, ctx, con):
        return {"dataset": self.name, "result": "changed", "rows": 1, "added": 1, "removed": 0}

    def after_swap(self, ctx):
        self.after.append(ctx.run_id)


@pytest.fixture
def fake(monkeypatch):
    from RevenueChaser.rcdata import build
    ds = _Fake()
    monkeypatch.setattr(build, "DATASETS", {ds.name: ds})
    base.parquet_root().mkdir(parents=True, exist_ok=True)
    return ds


def test_spine_is_a_serving_kind_of_version_2():
    assert serving.SERVING_SCHEMA_VERSION["spine"] == 2      # the kind gained etf_holdings (funds/etf_holdings)
    assert "spine" in serving.KINDS
    assert serving.live_path("spine").name == "spine.duckdb"


def test_after_swap_runs_once_the_build_is_live(fake):
    row = base.run_request({"dataset": fake.name, "params": {}})
    assert row["result"] == "changed"
    assert len(fake.after) == 1
    with serving.open_read("spine") as con:
        assert con.execute("SELECT count(*) FROM fake_one").fetchone() == (1,)


def test_after_swap_does_not_run_when_the_swap_is_refused(fake, monkeypatch):
    fake.check = lambda ctx, con: {"dataset": fake.name, "result": "failed", "note": "bad"}
    row = base.run_request({"dataset": fake.name, "params": {}})
    assert row["result"] == "failed" and fake.after == []
    assert not serving.live_path("spine").exists()


def test_an_attended_dataset_is_skipped_unless_the_cli_asked(fake):
    fake.attended = True
    rid = ops.enqueue(fake.name, None, by="schedule")
    row = base.run_request({"id": rid, "dataset": fake.name, "requested_by": "schedule", "params": {}})
    assert row["result"] == "skipped" and "attended" in row["note"] and fake.fetched == []
    row = base.run_request({"dataset": fake.name, "requested_by": "cli", "params": {}})
    assert row["result"] == "changed" and fake.fetched == [date(2026, 10, 27)]


def test_an_attended_dataset_is_never_due(fake):
    fake.attended = True
    assert due.is_due(fake) == (False, "attended: run from the CLI on the server (a person logs in; WRDS_USERNAME "
                                     "must be set)")


def test_a_backfill_without_from_starts_at_the_sources_beginning(fake):
    row = base.run_request({"dataset": fake.name, "requested_by": "cli", "params": {"mode": "backfill"}})
    assert row["result"] == "changed" and fake.fetched == [("backfill", None, date(2026, 10, 27))]


def test_a_backfill_without_from_is_refused_for_a_dataset_that_needs_it(fake):
    fake.backfill_needs_from = True
    row = base.run_request({"dataset": fake.name, "requested_by": "cli", "params": {"mode": "backfill"}})
    assert row["result"] == "failed" and "--from" in row["note"]


def test_sync_issues_adds_new_keeps_open_and_resolves_what_is_gone():
    ops.sync_issues("spine/security", ["no_cusip", "no_figi"],
                    [("no_cusip", "MSFT", "a"), ("no_figi", "HEI", "b")], run_id="r1")
    ops.sync_issues("spine/security", ["no_cusip", "no_figi"],
                    [("no_cusip", "MSFT", "a again"), ("no_cusip", "ETN", "c")], run_id="r2")
    n, rows = ops.open_issues("spine/security")
    assert n == 2 and sorted((r["kind"], r["key"]) for r in rows) == [("no_cusip", "ETN"), ("no_cusip", "MSFT")]
    msft = [r for r in rows if r["key"] == "MSFT"][0]
    assert msft["run_id"] == "r1"                         # kept, not a second row
    with ops.connect() as con:
        gone = con.execute("SELECT resolved_on FROM build_issues WHERE key = 'HEI'").fetchone()[0]
    assert gone is not None


def test_sync_issues_leaves_other_kinds_and_datasets_alone():
    ops.record_issue("spine/security", "source_failed", "wrds", "x")
    ops.record_issue("market_series", "no_cusip", "MSFT", "x")
    ops.sync_issues("spine/security", ["no_cusip"], [], run_id="r")
    assert ops.open_issues("spine/security")[0] == 1 and ops.open_issues("market_series")[0] == 1


def test_http_post_counts_every_attempt_and_retries_a_429_after_its_reset(http_mock):
    answers = iter([(429, b"slow", {"ratelimit-reset": "7"}), (200, b"[]", {})])
    http_mock.add("figi.test", lambda request: next(answers))
    slept = []
    ctx = base.RunContext(run_id="r", dataset="d", sleep=slept.append)
    r = base.http_post(ctx, "openfigi", "https://figi.test/v3/mapping", json=[{"a": 1}], headers={"X-K": "k"})
    assert r.status_code == 200 and ctx.calls["openfigi"] == [2, 0, 1]
    assert slept == [8.0]                                  # ratelimit-reset + 1 s, not the ladder's backoff
    assert http_mock.calls("figi.test")[0].headers["X-K"] == "k"


def test_http_post_does_not_retry_a_401(http_mock):
    http_mock.add("figi.test", b"bad key", status=401)
    slept = []
    ctx = base.RunContext(run_id="r", dataset="d", sleep=slept.append)
    with pytest.raises(base.SourceError, match="HTTP 401"):
        base.http_post(ctx, "openfigi", "https://figi.test/v3/mapping", json=[])
    assert ctx.calls["openfigi"] == [1, 1, 0] and slept == []


def test_the_cli_backfills_several_datasets_in_one_process(fake, monkeypatch):
    from RevenueChaser.rcdata import build
    from RevenueChaser.rcdata.build import __main__ as cli
    two = _Fake("fake/two")
    monkeypatch.setattr(build, "DATASETS", {fake.name: fake, two.name: two})
    monkeypatch.setattr(cli, "DATASETS", build.DATASETS)
    assert cli.main(["backfill", "fake/one,", "fake/two"]) == 0
    assert fake.fetched == [("backfill", None, date(2026, 10, 27))]
    assert two.fetched == [("backfill", None, date(2026, 10, 27))]
