"""#446 steps 3 and 4: ``prices`` builds on a copy of the live file and reloads only what changed.

The main proof is metamorphic, as for ``fundamentals`` (``test_fund_incremental``): whatever changed in Parquet or in
the spine, the build on the live copy must serve exactly what a whole build of the same tree serves, table by table
and view by view, and leave the same build issues open (``_same_as_whole``). The one table it does not rebuild is the
WRDS difference report, which a build on the live copy keeps as it was (``incremental.KEPT``).

The fixture is ``test_prices_wrds``'s: AAPL (``001690:01``) with a 2-for-1 split ex 4 Jun 2025 and Compustat bars to
27 Oct 2026, a delisted OLDCO (``099999:01``, 2024) the spine does not hold, Alpaca's bars and a dividend in October.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from RevenueChaser.core import calendar
from RevenueChaser.rcdata import ops, serving
from RevenueChaser.rcdata.build import base, dirty
from RevenueChaser.rcdata.build.prices import incremental, stage, wrds_history
from RevenueChaser.rcdata.build.prices import serving as prices_serving

from .prices_fixture import SPINE, write_spine
from .test_prices_wrds import AAPL, OLDCO, _build, _q, env, fake  # noqa: F401

SCHEMA = wrds_history.SECD + wrds_history.PROVENANCE
VIEW_BUILD_COLUMNS = ("fetched_at", "run_at")          # a view's build time, not data


def _clock(day: date) -> None:
    calendar._now = lambda: datetime(day.year, day.month, day.day, 21, 0, tzinfo=timezone.utc)


def _serve() -> dict:
    row = base.run_request({"dataset": prices_serving.NAME, "params": {}})
    assert row["result"] != "failed", row
    return row


def _note(row: dict) -> str:
    return row.get("note") or ""


def _secd_rows(year: int) -> list[dict]:
    return stage.read_rows(wrds_history.partition_path("secd", year))


def _write_secd(year: int, rows: list[dict]) -> None:
    stage.write_rows(rows, SCHEMA, wrds_history.partition_path("secd", year), order="gvkey, iid, datadate")


def _aapl_day(day: date, close: float, **extra) -> dict:
    last = [r for r in _secd_rows(2026) if r["gvkey"] == "001690"][-1]
    return {**last, "datadate": day, "prccd": close, "prcod": close, "prchd": close * 1.01, "prcld": close * 0.99,
            "source_as_of": day, "run_id": last["run_id"], **extra}


def _respine(rows: list[dict]) -> None:
    serving.live_path("spine").unlink()
    write_spine(rows)


def _snapshot() -> dict[str, list]:
    out = {}
    with serving.open_read("prices") as con:
        for name in (*prices_serving.TABLES, *prices_serving.VIEWS):
            if name in incremental.KEPT:
                continue
            cols = [r[0] for r in con.execute(f"DESCRIBE {name}").fetchall()
                    if not (name in prices_serving.VIEWS and r[0] in VIEW_BUILD_COLUMNS)]
            rows = con.execute(f"SELECT {', '.join(chr(34) + c + chr(34) for c in cols)} FROM {name}").fetchall()
            out[name] = sorted(rows, key=repr)
    out["open issues"] = sorted((r["kind"], r["key"]) for r in _open_issues())
    return out


def _open_issues() -> list[dict]:
    with ops._open() as con:
        return [dict(r) for r in con.execute("SELECT kind, key FROM build_issues WHERE dataset = ? "
                                             "AND resolved_on IS NULL", (prices_serving.NAME,)).fetchall()]


def _report() -> list:
    return sorted(tuple(r.values()) for r in _q("SELECT * FROM wrds_price_diff"))


def _same_as_whole(monkeypatch, row: dict) -> dict[str, list]:
    """The live file was just built on the live copy: it serves what a whole build of the same tree serves."""
    assert "built on the live file" in _note(row), _note(row)
    got = _snapshot()
    monkeypatch.setenv(dirty.WHOLE_ENV, "1")
    whole = _serve()
    monkeypatch.delenv(dirty.WHOLE_ENV)
    assert "built whole" in _note(whole), _note(whole)
    want = _snapshot()
    for name in want:
        assert got[name] == want[name], name
    return want


OLD = [date(2015, 3, 2), date(2015, 3, 3), date(2015, 3, 4), date(2015, 3, 5)]


def _old_history() -> None:
    """AAPL in 2015, a year partition no later change touches: a build on the live copy stages none of it, so it
    must keep these rows (and the dividend and caps on them) exactly as the whole build made them."""
    template = [r for r in _secd_rows(2025) if r["gvkey"] == "001690"][0]
    rows = [{**template, "datadate": d, "prccd": c, "prcod": c, "prchd": c, "prcld": c, "ajexdi": 4.0,
             "trfd": 1.0 + 0.001 * i, "cshoc": 5.8e9, "divd": 0.47 if i == 2 else None, "div": None, "divsp": None,
             "source_as_of": date(2015, 3, 31), "run_id": "old"}
            for i, (d, c) in enumerate(zip(OLD, (128.0, 129.0, 127.5, 126.4)))]
    _write_secd(2015, rows)


@pytest.fixture
def live(fake, monkeypatch):
    """A whole build with an old year, then one on the live copy that changes nothing (the manifest)."""
    _build()
    _old_history()
    monkeypatch.setenv(dirty.WHOLE_ENV, "1")
    _serve()
    monkeypatch.delenv(dirty.WHOLE_ENV)
    assert len(_q("SELECT 1 FROM price_bar_daily WHERE session_date < '2016-01-01'")) == 4
    row = _serve()
    assert "built on the live file" in _note(row), _note(row)
    return fake


# ── The cases a build on the live copy must get right ───────────────────────────────────────────────────────

def test_nothing_changed_serves_the_same_file(live, monkeypatch):
    before = _snapshot()
    report = _report()
    row = _serve()
    assert "0 Parquet file(s) changed" in _note(row), _note(row)
    assert _snapshot() == before and _report() == report
    _same_as_whole(monkeypatch, row)


def test_a_new_compustat_day_is_added(live, monkeypatch):
    day = date(2026, 10, 28)
    _write_secd(2026, _secd_rows(2026) + [_aapl_day(day, 203.0)])
    _clock(day)
    row = _serve()
    assert [r["close"] for r in _q("SELECT close FROM price_bar_daily WHERE security_id = ? AND session_date = ?",
                                   [AAPL, day])] == [203.0]
    assert "rebuilt from 2026-" in _note(row) and "0 securities whole" in _note(row), _note(row)
    _same_as_whole(monkeypatch, row)


def test_a_split_that_goes_ex_today_restates_the_factors_and_the_caps(live, monkeypatch):
    day = date(2026, 10, 28)
    rows = [{**r, "ajexdi": 2.0} if r["gvkey"] == "001690" else r for r in _secd_rows(2026)]
    _write_secd(2026, rows + [_aapl_day(day, 101.0, ajexdi=1.0, cshoc=30e9)])
    _clock(day)
    row = _serve()
    assert _q("SELECT 1 FROM corporate_action WHERE security_id = ? AND ex_date = ? AND action = 'split'",
              [AAPL, day])
    _same_as_whole(monkeypatch, row)


def test_an_older_year_rewritten_by_a_refresh_is_reloaded(live, monkeypatch):
    rows = _secd_rows(2025)
    rows = [{**r, "prccd": r["prccd"] + 1.0} if r["datadate"] == date(2025, 6, 3) else r for r in rows]
    _write_secd(2025, rows)
    row = _serve()
    assert _q("SELECT close FROM price_bar_daily WHERE security_id = ? AND session_date = '2025-06-03'",
              [AAPL])[0]["close"] == 203.0
    _same_as_whole(monkeypatch, row)


def test_a_name_that_joins_the_universe_gets_its_whole_history(live, monkeypatch):
    """OLDCO's bars are from 2024, long before anything changed in Parquet: only the spine says it joined."""
    _respine(SPINE + [{"ticker": "OLD-A", "cik": "5550001", "name": "OLD CO", "exchange": "NYSE", "tier": "A",
                          "security_type": "common", "gvkey": "099999", "iid": "01", "composite_figi": None,
                          "cusip": "680000101"}])
    row = _serve()
    assert len(_q("SELECT 1 FROM price_bar_daily WHERE security_id = ?", [OLDCO])) == 2
    _same_as_whole(monkeypatch, row)


def test_a_name_that_leaves_the_universe_is_removed(live, monkeypatch):
    _respine([r for r in SPINE if r["ticker"] != "AAPL"])
    row = base.run_request({"dataset": prices_serving.NAME, "params": {"allow_shrink": True}})
    assert row["result"] != "failed", row
    assert not _q("SELECT 1 FROM price_bar_daily WHERE security_id = ?", [AAPL])
    assert "built on the live file" in _note(row), _note(row)
    got = _snapshot()
    monkeypatch.setenv(dirty.WHOLE_ENV, "1")
    assert base.run_request({"dataset": prices_serving.NAME, "params": {"allow_shrink": True}})["result"] != "failed"
    assert _snapshot() == got


def test_a_late_cover_restates_the_caps_from_its_day(live, monkeypatch):
    """A cover whose as-of is long before anything else changed: the caps after it change, and only after it."""
    from RevenueChaser.rcdata.build.prices import cover_shares
    path = cover_shares.partition_path(2026)
    rows = stage.read_rows(path) + [{
        "cik": "320193", "accession": "0000320193-25-000001", "form": "10-Q", "filed": date(2026, 10, 27),
        "period_end": date(2025, 3, 29), "class_member": "", "symbol": "AAPL", "shares": 15_100_000_000.0,
        "as_of": date(2025, 4, 18), "source_as_of": date(2026, 10, 27), "fetched_at": 9.0, "run_id": "c-late"}]
    stage.write_rows(rows, cover_shares.SCHEMA, path)
    row = _serve()
    _same_as_whole(monkeypatch, row)


def test_a_new_dividend_in_sec_divid_is_applied(live, monkeypatch):
    rows = stage.read_rows(wrds_history.partition_path("sec_divid", 2026))
    assert rows
    rows.append({**rows[-1], "datadate": date(2026, 10, 26), "divd": 0.3, "div": None, "divsp": None})
    schema = wrds_history.DIVID + wrds_history.PROVENANCE
    stage.write_rows(rows, schema, wrds_history.partition_path("sec_divid", 2026))
    row = _serve()
    assert _q("SELECT 1 FROM corporate_action WHERE security_id = ? AND ex_date = '2026-10-26'", [AAPL])
    _same_as_whole(monkeypatch, row)


def test_the_wrds_difference_report_is_kept_on_the_live_copy(live, monkeypatch):
    report = _report()
    day = date(2026, 10, 28)
    _write_secd(2026, _secd_rows(2026) + [_aapl_day(day, 203.0)])
    _clock(day)
    row = _serve()
    assert _report() == report
    assert "wrds_price_diff kept" in _note(row), _note(row)


def test_a_changed_alpaca_action_replaces_the_one_served(live, monkeypatch):
    from RevenueChaser.rcdata.build.prices import actions
    path = actions.partition_path(2026)
    rows = stage.read_rows(path)
    assert rows
    stage.write_rows([{**r, "rate": 0.27} for r in rows], actions.SCHEMA, path)
    row = _serve()
    assert [r["amount"] for r in _q("SELECT amount FROM corporate_action WHERE source = ?",
                                    [prices_serving.ALPACA_ACTIONS])] == [0.27]
    _same_as_whole(monkeypatch, row)


# ── Two listed classes of one issuer: the issuer cap reads both ─────────────────────────────────────────────

GOOGL, GOOG = "160329:01", "160329:03"
OCT = [date(2026, 10, 21), date(2026, 10, 22), date(2026, 10, 23), date(2026, 10, 26), date(2026, 10, 27)]


@pytest.fixture
def alphabet(live, monkeypatch):
    from .prices_fixture import ALPHABET
    template = [r for r in _secd_rows(2026) if r["gvkey"] == "001690"][0]
    extra = [{**template, "gvkey": "160329", "iid": iid, "tic": tic, "cusip": None, "datadate": d,
              "prccd": c + i, "prcod": c + i, "prchd": c + i, "prcld": c + i, "cshoc": shares, "divd": None,
              "div": None, "divsp": None, "ajexdi": 1.0, "trfd": 1.0}
             for iid, tic, c, shares in (("01", "GOOGL", 170.0, 5.8e9), ("03", "GOOG", 171.0, 5.45e9))
             for i, d in enumerate(OCT) if iid == "01" or d <= OCT[2]]      # GOOG's last bar: the 23rd
    _write_secd(2026, _secd_rows(2026) + extra)
    _respine(SPINE + ALPHABET)
    monkeypatch.setenv(dirty.WHOLE_ENV, "1")
    _serve()
    monkeypatch.delenv(dirty.WHOLE_ENV)
    assert _serve()["result"] != "failed"
    caps = _q("SELECT market_cap, issuer_market_cap FROM market_cap_daily WHERE security_id = ? "
              "AND session_date = ?", [GOOGL, OCT[-1]])
    assert caps and caps[0]["issuer_market_cap"] > caps[0]["market_cap"]          # GOOG's cap of the 23rd in it
    return live


def test_a_class_whose_issuer_changes_restates_the_other_classes_issuer_cap(alphabet, monkeypatch):
    """GOOG moves to another CIK in the spine: its own caps keep their values, but GOOGL's issuer cap loses GOOG on
    every session, which only the closure over the issuer reaches (GOOGL itself changed nothing)."""
    from .prices_fixture import ALPHABET
    _respine(SPINE + [{**r, "cik": "9999999"} if r["ticker"] == "GOOG" else r for r in ALPHABET])
    row = _serve()
    caps = _q("SELECT market_cap, issuer_market_cap FROM market_cap_daily WHERE security_id = ? "
              "AND session_date = ?", [GOOGL, OCT[0]])
    assert caps[0]["issuer_market_cap"] == caps[0]["market_cap"]
    _same_as_whole(monkeypatch, row)


def test_a_new_day_of_one_class_counts_the_others_last_cap(alphabet, monkeypatch):
    day = date(2026, 10, 28)
    template = [r for r in _secd_rows(2026) if r["gvkey"] == "160329" and r["iid"] == "01"][-1]
    _write_secd(2026, _secd_rows(2026) + [{**template, "datadate": day, "source_as_of": day}])
    _clock(day)
    row = _serve()
    caps = _q("SELECT market_cap, issuer_market_cap FROM market_cap_daily WHERE security_id = ? "
              "AND session_date = ?", [GOOGL, day])
    assert caps[0]["issuer_market_cap"] > caps[0]["market_cap"]                    # GOOG's cap of the 27th
    _same_as_whole(monkeypatch, row)


def test_a_rebuilt_day_counts_a_class_whose_last_cap_is_older(alphabet, monkeypatch):
    """A cover of GOOGL as of the 26th rebuilds GOOGL and GOOG from that day only. GOOGL's issuer cap of the 26th
    still counts GOOG at its cap of the 23rd, a bar the rebuild does not reload."""
    from RevenueChaser.rcdata.build.prices import cover_shares
    path = cover_shares.partition_path(2026)
    rows = stage.read_rows(path) + [{
        "cik": "1652044", "accession": "0001652044-26-000099", "form": "8-K", "filed": date(2026, 10, 27),
        "period_end": date(2026, 9, 30), "class_member": "us-gaap:CommonClassAMember", "symbol": "GOOGL",
        "shares": 5_790_000_000.0, "as_of": OCT[3], "source_as_of": date(2026, 10, 27), "fetched_at": 9.0,
        "run_id": "c-googl"}]
    stage.write_rows(rows, cover_shares.SCHEMA, path)
    row = _serve()
    assert "rebuilt from 2026-10-28" in _note(row), _note(row)
    caps = _q("SELECT market_cap, issuer_market_cap FROM market_cap_daily WHERE security_id = ? "
              "AND session_date = ?", [GOOGL, OCT[3]])
    assert caps[0]["issuer_market_cap"] > caps[0]["market_cap"]
    _same_as_whole(monkeypatch, row)


# ── When a whole build is still forced ───────────────────────────────────────────────────────────────────────

def test_a_changed_exchange_history_forces_a_whole_build(live, monkeypatch):
    from RevenueChaser.rcdata.build.spine import wrds
    target = wrds.parquet_dir("wrds/sec_idhist") / "snapshot=2026-10-01" / "sec_idhist.parquet"
    stage.write_rows([{"gvkey": "001690", "iid": "01", "item": "EXCHG", "itemvalue": "14",
                       "efffrom": date(2000, 1, 3), "effthru": None, "source_as_of": date(2026, 10, 1),
                       "fetched_at": 1.0, "run_id": "x"}],
                     [("gvkey", "TEXT"), ("iid", "TEXT"), ("item", "TEXT"), ("itemvalue", "TEXT"),
                      ("efffrom", "DATE"), ("effthru", "DATE"), ("source_as_of", "DATE"), ("fetched_at", "DOUBLE"),
                      ("run_id", "TEXT")], target)
    row = _serve()
    assert "built whole" in _note(row) and "sec_idhist" in _note(row), _note(row)


def test_a_new_split_snapshot_the_monthly_refresh_forces_a_whole_build(live):
    rows = [{"gvkey": "001690", "iid": "01", "datadate": date(2025, 6, 4), "split": 2.0,
             "source_as_of": date(2026, 10, 28), "fetched_at": 2.0, "run_id": "monthly"}]
    stage.write_rows(rows, wrds_history.SPLIT + wrds_history.PROVENANCE,
                     wrds_history.snapshot_path("sec_split", date(2026, 10, 28)))
    row = _serve()
    assert "built whole" in _note(row) and "monthly WRDS refresh" in _note(row), _note(row)


def test_every_prices_dataset_declares_a_dirty_set():
    assert dirty.incremental(base.kind_datasets("prices"))
