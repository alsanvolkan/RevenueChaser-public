"""The gaps the independent review of PR 220 found (comment 6504335191 on #220): S-1, S-2, S-3 and N-1."""
from __future__ import annotations

import pytest

from RevenueChaser.rcdata import serving
from RevenueChaser.rcdata.build import base


# ── S-1: allow_shrink relaxes only the asking dataset's tables ──────────────────────────────────────────────────
def _build_two(t_rows: int, u_rows: int, **swap_kw) -> None:
    with serving.build("market") as b:
        b.con.execute(f"CREATE TABLE t AS SELECT range AS i FROM range({t_rows})")
        b.con.execute(f"CREATE TABLE u AS SELECT range AS i FROM range({u_rows})")
        b.swap({"t": t_rows, "u": u_rows}, **swap_kw)


def test_s1_a_shrink_allowed_for_one_table_still_refuses_another_that_empties():
    _build_two(10, 391)
    with pytest.raises(serving.SwapRefused, match="u empties"):
        _build_two(0, 0, allow_shrink=("t",))
    with serving.open_read("market") as con:
        assert con.execute("SELECT count(*) FROM u").fetchone()[0] == 391


def test_s1_the_named_table_may_shrink_while_the_others_hold():
    _build_two(10, 391)
    _build_two(0, 391, allow_shrink=("t",))
    with serving.open_read("market") as con:
        assert con.execute("SELECT count(*) FROM t").fetchone()[0] == 0


class _Fake:
    """A source dataset of the market kind serving one table, its row count set by the test."""
    kind = "market"
    providers = ("test",)

    def __init__(self, name: str, table: str, rows: int):
        self.name, self.table, self.rows = name, table, rows
        self.tables = (table,)

    def build(self, ctx, con):
        con.execute(f"CREATE TABLE {self.table} AS SELECT range AS i FROM range({self.rows})")
        return {self.table: self.rows}

    def check(self, ctx, con):
        return {"dataset": self.name, "result": "changed"}


def test_s1_a_request_with_allow_shrink_cannot_empty_another_datasets_table(monkeypatch):
    """The review's probe: SPX built with 391 rows, its Parquet gone, then market_series run with allow_shrink."""
    series, spx = _Fake("market_series", "market_series", 50), _Fake("uw/spot_expiry_strike", "spx", 391)
    monkeypatch.setattr(base, "kind_datasets", lambda kind: [series, spx])
    base.parquet_root().mkdir(parents=True, exist_ok=True)
    base.build_kind(base.RunContext(run_id="r1", dataset=series.name), series)
    series.rows, spx.rows = 40, 0
    ctx = base.RunContext(run_id="r2", dataset=series.name, allow_shrink=True)
    with pytest.raises(serving.SwapRefused, match="spx empties"):
        base.build_kind(ctx, series)
    spx.rows = 391
    base.build_kind(ctx, series)                    # its own table may shrink: 50 -> 40
    with serving.open_read("market") as con:
        assert con.execute("SELECT count(*) FROM market_series").fetchone()[0] == 40
        assert con.execute("SELECT count(*) FROM spx").fetchone()[0] == 391


def test_s1_the_scope_is_the_own_tables_plus_the_kinds_derived_ones(monkeypatch):
    series, spx = _Fake("market_series", "market_series", 1), _Fake("uw/spot_expiry_strike", "spx", 1)
    derived = _Fake("market/serve", "derived", 1)
    derived.providers = ()
    feeder = _Fake("feeder", "unused", 1)
    feeder.tables = ()
    monkeypatch.setattr(base, "kind_datasets", lambda kind: [series, spx, derived, feeder])
    assert base.shrink_scope(series) == ("market_series", "derived")
    assert base.shrink_scope(feeder) == ("derived",)


# ── S-2: a session that has not finished is never fetched ────────────────────────────────────────────────────────
from datetime import date, datetime, timezone  # noqa: E402

from RevenueChaser.core import calendar  # noqa: E402
from RevenueChaser.core.providers import unusual_whales  # noqa: E402
from RevenueChaser.rcdata.build import uw_spot_expiry_strike as uw  # noqa: E402

from . import uw_fixture  # noqa: E402

UW_HEADERS = {"x-uw-daily-req-count": "355", "x-uw-req-per-minute-remaining": "1000000",
              "x-uw-req-per-minute-reset": "60000", "x-uw-token-req-limit": "100000000",
              "content-type": "application/json"}


@pytest.fixture
def uw_at_11_new_york(monkeypatch):
    """Tue 27 Oct 2026, 11:00 New York: the 27th is open, the latest complete session is the 26th."""
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "test-key")
    monkeypatch.setattr(unusual_whales, "BASE", "https://uw.test/api")
    monkeypatch.setattr(calendar, "_now", lambda: datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc))


def test_s2_an_explicit_as_of_of_an_open_session_is_refused_without_a_call(http_mock, uw_at_11_new_york):
    http_mock.add("expiry-strike", uw_fixture.body(date(2026, 10, 27)), headers=UW_HEADERS)
    row = base.run_request({"dataset": uw.NAME, "params": {"as_of": "2026-10-27"}})
    assert row["result"] == "failed" and "not complete" in row["note"]
    assert http_mock.calls("expiry-strike") == []
    assert not uw.raw_path(date(2026, 10, 27)).exists()


def test_s2_a_backfill_to_an_open_session_stops_at_the_latest_complete_one(http_mock, uw_at_11_new_york):
    http_mock.add("date=2026-10-26", uw_fixture.body(date(2026, 10, 26)), headers=UW_HEADERS)
    http_mock.add("date=2026-10-27", uw_fixture.body(date(2026, 10, 27)), headers=UW_HEADERS)
    base.run_request({"dataset": uw.NAME, "params": {"mode": "backfill", "from": "2026-10-26", "to": "2026-10-27"}})
    asked = [c.url.query.decode() for c in http_mock.calls("expiry-strike")]
    assert len(asked) == 1 and "date=2026-10-26" in asked[0]
    assert not uw.raw_path(date(2026, 10, 27)).exists()


def test_s2_the_cli_help_names_the_real_default():
    from RevenueChaser.rcdata.build import __main__ as cli
    import inspect
    assert "default: today's" not in inspect.getsource(cli)
