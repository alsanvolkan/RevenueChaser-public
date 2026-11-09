"""#446: ``options`` builds on a copy of the live file and reloads only the sessions whose partitions changed.

Metamorphic, as for the other kinds: whatever changed in Parquet, the build on the live copy serves exactly what a
whole build of the same tree serves (``_same_as_whole``), the window of ``option_eod`` included.
"""
from __future__ import annotations

from datetime import date, timedelta

import duckdb
import pytest

from RevenueChaser.rcdata import serving
from RevenueChaser.rcdata.build import base, dirty
from RevenueChaser.rcdata.build.options import KIND, ibkr_iv, tables, uw_list, uw_screener

LIST, SCREENER, IBKR = tables.LIST_SOURCE, tables.SCREENER_SOURCE, tables.IBKR_SOURCE


def _sessions(n: int, first: date = date(2026, 6, 1)) -> list[date]:
    out = []
    while len(out) < n:
        if first.weekday() < 5:
            out.append(first)
        first += timedelta(days=1)
    return out


DAYS = _sessions(34)


def _prov(day: date, fetched: float = 1.0, source: str = "uw") -> dict:
    return {"source": source, "source_as_of": day, "fetched_at": fetched, "run_id": "r", "observed": True}


def _eod(day: date, n: int = 2, strike_add: float = 0.0, fetched: float = 1.0) -> None:
    t = tables.TABLES["option_eod"]
    rows = [{"session_date": day, "ticker": "AAPL", "option_symbol": f"AAPL261120C{i:08d}", "expiry": date(2026, 11, 20),
             "strike": float(i) + strike_add, "option_type": "call", **_prov(day, fetched)} for i in range(n)]
    base.upsert_partition(rows, t.schema, list(t.key), tables.partition_path(LIST, t.name, day))


def _strike(source: str, day: date, set_name: str, price: float) -> None:
    t = tables.TABLES["option_strike"]
    rows = [{"session_date": day, "ticker": "NVDA", "set_name": set_name, "strike": 100.0, "price": price,
             **_prov(day)}]
    base.upsert_partition(rows, t.schema, list(t.key), tables.partition_path(source, t.name, day))


def _iv_uw(day: date, vol: float) -> None:
    t = tables.TABLES["iv_daily"]
    rows = [{"session_date": day, "ticker": "AAPL", "days": 30, "volatility": vol, **_prov(day)}]
    base.upsert_partition(rows, t.schema, list(t.key), tables.partition_path(LIST, t.name, day))


def _iv_ibkr(ticker: str, days: list[date], vol: float) -> None:
    t = tables.TABLES["iv_daily"]
    rows = [{"session_date": d, "ticker": ticker, "days": 30, "volatility": vol, **_prov(d, source="ibkr")}
            for d in days]
    base.upsert_partition(rows, t.schema, list(t.key), tables.ticker_partition_path(IBKR, t.name, ticker))


def _screener(day: date, tickers: list[str]) -> None:
    t = tables.TABLES["option_screener_daily"]
    rows = [{"session_date": day, "ticker": s, "screened": True, **_prov(day)} for s in tickers]
    base.upsert_partition(rows, t.schema, list(t.key), tables.partition_path(SCREENER, t.name, day))


def _seed() -> None:
    base.parquet_root().mkdir(parents=True, exist_ok=True)
    for d in DAYS[:32]:
        _eod(d)
    _strike(LIST, DAYS[30], "list", 10.0)
    _strike(SCREENER, DAYS[30], "earnings", 11.0)
    _strike(LIST, DAYS[31], "list", 12.0)
    _iv_uw(DAYS[30], 0.3)
    _iv_uw(DAYS[31], 0.31)
    _iv_ibkr("AAPL", DAYS[25:32], 0.2)
    _iv_ibkr("MSFT", DAYS[25:32], 0.25)
    _screener(DAYS[31], ["AAPL", "MSFT"])


@pytest.fixture(autouse=True)
def quiet_checks(monkeypatch):
    for ds in (uw_list.DATASET, uw_screener.DATASET, ibkr_iv.DATASET):
        monkeypatch.setattr(type(ds), "check", lambda self, ctx, con: {"dataset": self.name, "result": "changed"})


def _build() -> str:
    ctx = base.RunContext(run_id="t-options", dataset=uw_list.NAME)
    base.build_kind_many(KIND, [(uw_list.DATASET, ctx)], lead=ctx)
    return " ".join(ctx.notes)


def _snapshot() -> dict[str, list]:
    with serving.open_read(KIND) as con:
        out = {t: sorted(con.execute(f'SELECT * FROM "{t}"').fetchall(), key=repr) for t in tables.TABLES}
        out["window"] = sorted(con.execute(f"SELECT * FROM {serving.WINDOW_TABLE}").fetchall())
        return out


def _same_as_whole(monkeypatch, note: str) -> dict[str, list]:
    assert "built on the live file" in note, note
    got = _snapshot()
    monkeypatch.setenv(dirty.WHOLE_ENV, "1")
    whole = _build()
    monkeypatch.delenv(dirty.WHOLE_ENV)
    assert "built whole" in whole, whole
    want = _snapshot()
    for name in want:
        if got[name] != want[name]:
            print("DIFF", name, "\n inc-only", sorted(set(got[name]) - set(want[name]), key=repr)[:10],
                  "\n whole-only", sorted(set(want[name]) - set(got[name]), key=repr)[:10])
        assert got[name] == want[name], name
    return want


@pytest.fixture
def seeded():
    _seed()
    note = _build()
    assert "built whole" in note, note
    return note


def test_every_options_dataset_declares_its_dirty_set():
    assert dirty.incremental(base.kind_datasets(KIND))


def test_a_live_file_without_a_manifest_is_built_whole_once(seeded):
    with duckdb.connect(str(serving.live_path(KIND))) as con:
        con.execute(f"DROP TABLE {dirty.MANIFEST_TABLE}")
    assert "no build manifest" in _build()
    assert "built on the live file" in _build()


def test_nothing_changed(seeded, monkeypatch):
    note = _build()
    assert "0 Parquet file(s) changed" in note, note
    _same_as_whole(monkeypatch, note)


def test_a_new_session_moves_the_window_and_a_rewritten_one_is_replaced(seeded, monkeypatch):
    _eod(DAYS[32])
    _eod(DAYS[31], n=3, strike_add=0.5, fetched=2.0)               # typed again: a third contract, new strikes
    _strike(SCREENER, DAYS[31], "earnings", 13.0)                  # a second source's partition of a session
    _iv_uw(DAYS[32], 0.33)
    _screener(DAYS[32], ["NVDA"])
    want = _same_as_whole(monkeypatch, _build())
    assert min(r[0] for r in want["option_eod"]) == DAYS[3]
    assert len({r[0] for r in want["option_eod"]}) == tables.EOD_SESSIONS


def test_a_gone_newest_session_brings_back_the_one_below_the_window(seeded, monkeypatch):
    tables.partition_path(LIST, "option_eod", DAYS[31]).unlink()
    tables.partition_path(SCREENER, "option_strike", DAYS[30]).unlink()
    monkeypatch.setattr(serving, "MAX_SHRINK", 1.0)
    want = _same_as_whole(monkeypatch, _build())
    assert min(r[0] for r in want["option_eod"]) == DAYS[1]


def test_a_name_s_ibkr_year_reloads_iv_daily_and_keeps_the_uw_rows(seeded, monkeypatch):
    _iv_ibkr("MSFT", DAYS[20:32], 0.27)
    want = _same_as_whole(monkeypatch, _build())
    assert sum(1 for r in want["iv_daily"] if r[1] == "MSFT") == 12
    assert sum(1 for r in want["iv_daily"] if r[1] == "AAPL" and r[6] == "uw") == 2


def test_a_uw_session_keeps_the_ibkr_rows_of_that_session(seeded, monkeypatch):
    _iv_uw(DAYS[31], 0.4)
    want = _same_as_whole(monkeypatch, _build())
    assert {(r[1], r[6]) for r in want["iv_daily"] if r[0] == DAYS[31]} == {("AAPL", "uw"), ("AAPL", "ibkr"),
                                                                           ("MSFT", "ibkr")}
