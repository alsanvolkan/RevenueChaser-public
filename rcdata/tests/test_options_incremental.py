"""#446: ``options`` builds on a copy of the live file and reloads only its dirty partitions.

Metamorphic, as ``test_fund_incremental``: whatever changed in Parquet, the build on the live copy must serve exactly
what a whole build of the same tree serves.
"""
from __future__ import annotations

import os
from datetime import date, timedelta

import pytest

from RevenueChaser.rcdata import serving
from RevenueChaser.rcdata.build import base, dirty
from RevenueChaser.rcdata.build.options import ibkr_iv, tables

LIST, SCREENER = tables.LIST_SOURCE, tables.SCREENER_SOURCE


def _ctx() -> base.RunContext:
    return base.RunContext(run_id="t-options", dataset="test")


def _sessions(n: int, first: date = date(2026, 6, 1)) -> list[date]:
    out = []
    while len(out) < n:
        if first.weekday() < 5:
            out.append(first)
        first += timedelta(days=1)
    return out


def _prov(day: date, fetched: float = 1.0, source: str = "uw") -> dict:
    return {"source": source, "source_as_of": day, "fetched_at": fetched, "run_id": "r", "observed": True}


def _write_day(day: date, price: float = 1.0, fetched: float = 1.0) -> None:
    eod, strike, iv, scr = (tables.TABLES[n] for n in ("option_eod", "option_strike", "iv_daily",
                                                         "option_screener_daily"))
    base.upsert_partition([{"session_date": day, "ticker": "AAPL", "option_symbol": f"AAPL-C{i}", "strike": float(i),
                            "nbbo_bid": price, **_prov(day, fetched)} for i in range(3)],
                          eod.schema, list(eod.key), tables.partition_path(LIST, eod.name, day))
    base.upsert_partition([{"session_date": day, "ticker": "AAPL", "set_name": "list", "strike": 100.0,
                            "price": price, **_prov(day, fetched)}], strike.schema, list(strike.key),
                          tables.partition_path(LIST, strike.name, day))
    base.upsert_partition([{"session_date": day, "ticker": "MSFT", "set_name": "earnings", "strike": 100.0,
                            "price": price, **_prov(day, fetched)}], strike.schema, list(strike.key),
                          tables.partition_path(SCREENER, strike.name, day))
    base.upsert_partition([{"session_date": day, "ticker": "AAPL", "days": 30, "volatility": price,
                            **_prov(day, fetched)}], iv.schema, list(iv.key), tables.partition_path(LIST, iv.name, day))
    base.upsert_partition([{"session_date": day, "ticker": "MSFT", "screened": True, **_prov(day, fetched)}],
                          scr.schema, list(scr.key), tables.partition_path(SCREENER, scr.name, day))


def _write_ibkr(ticker: str, days: list[date], vol: float = 0.3) -> None:
    t = tables.TABLES["iv_daily"]
    base.upsert_partition([{"session_date": d, "ticker": ticker, "days": 30, "volatility": vol,
                            **_prov(d, source="ibkr"), "observed": False} for d in days],
                          t.schema, list(t.key), ibkr_iv.partition(ticker))


def _build() -> str:
    ctx = _ctx()
    base.build_kind_many("options", [], lead=ctx)
    return " ".join(ctx.notes)


def _snapshot() -> dict[str, list]:
    with serving.open_read("options") as con:
        out = {}
        for t in tables.TABLES.values():
            cols = ", ".join(f'"{c}"' for c in t.columns if c not in ("tags", "report_flags"))
            out[t.name] = con.execute(f'SELECT {cols} FROM "{t.name}" ORDER BY ALL').fetchall()
        out["window"] = con.execute(f"SELECT * FROM {serving.WINDOW_TABLE} ORDER BY ALL").fetchall()
        return out


def _same_as_whole(monkeypatch) -> dict[str, list]:
    on_live = _snapshot()
    monkeypatch.setenv(dirty.WHOLE_ENV, "1")
    assert "built whole" in _build()
    monkeypatch.delenv(dirty.WHOLE_ENV)
    whole = _snapshot()
    for name in whole:
        assert on_live[name] == whole[name], name
    return whole


def _touch_later(path) -> None:
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 10))


@pytest.fixture
def seeded():
    base.parquet_root().mkdir(parents=True, exist_ok=True)
    days = _sessions(32)
    for d in days:
        _write_day(d)
    _write_ibkr("AAPL", days[-5:])
    _write_ibkr("MSFT", days[-5:])
    assert "built whole" in _build()
    return days


def test_every_options_dataset_declares_a_dirty_set():
    assert dirty.incremental(base.kind_datasets("options"))


def test_nothing_changed_builds_on_the_live_copy_and_serves_the_same(seeded):
    before = _snapshot()
    note = _build()
    assert "built on the live file: 0 Parquet file(s) changed" in note, note
    assert _snapshot() == before


def test_a_new_session_is_loaded_and_moves_the_window(seeded, monkeypatch):
    new = _sessions(1, seeded[-1] + timedelta(days=1))[0]
    _write_day(new, price=2.0)
    assert "built on the live file" in _build()
    whole = _same_as_whole(monkeypatch)
    eod_days = sorted({r[0] for r in whole["option_eod"]})
    assert len(eod_days) == tables.EOD_SESSIONS and eod_days[-1] == new


def test_a_restated_session_and_an_ibkr_name_are_reloaded(seeded, monkeypatch):
    _write_day(seeded[-3], price=5.0, fetched=2.0)
    _write_ibkr("AAPL", seeded[-5:], vol=0.9)
    assert "built on the live file" in _build()
    _same_as_whole(monkeypatch)
    with serving.open_read("options") as con:
        assert con.execute("SELECT volatility FROM iv_daily WHERE source = 'ibkr' AND ticker = 'AAPL' "
                           "GROUP BY ALL").fetchall() == [(0.9,)]
        # the UW row of the same name and day was kept, the IBKR rows of MSFT too
        assert con.execute("SELECT count(*) FROM iv_daily WHERE session_date = ?", [seeded[-3]]).fetchone()[0] == 3


def test_a_partition_with_an_old_mtime_is_still_reloaded(seeded, monkeypatch):
    path = tables.partition_path(SCREENER, "option_strike", seeded[-2])
    st = path.stat()
    _write_day(seeded[-2], price=7.0, fetched=3.0)
    os.utime(path, (st.st_atime, st.st_mtime))
    assert "built on the live file" in _build()
    _same_as_whole(monkeypatch)


def test_a_gone_partition_drops_its_rows(seeded, monkeypatch):
    monkeypatch.setattr(serving, "_verify_against_live", lambda *a, **k: None)   # the drop is a shrink, on purpose
    tables.partition_path(SCREENER, "option_strike", seeded[-1]).unlink()
    ibkr_iv.partition("MSFT").unlink()
    assert "built on the live file" in _build()
    whole = _same_as_whole(monkeypatch)
    assert not [r for r in whole["iv_daily"] if r[1] == "MSFT" and r[-5] == "ibkr"]


def test_a_live_file_without_a_manifest_still_matches_a_whole_build(seeded, monkeypatch):
    with serving.build("options", base="live") as b:
        b.con.execute(f"DROP TABLE {dirty.MANIFEST_TABLE}")
        b.con.execute(f"DROP TABLE {dirty.RULES_TABLE}")
        b.swap({})
    _write_day(seeded[-1], price=4.0, fetched=4.0)
    assert "built on the live file" in _build()
    _same_as_whole(monkeypatch)
