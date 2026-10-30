"""The Settings / Data control board's two writes and one read (design v28, 30 Oct 2026).

**Apply** saves the universe the board's presses ask for: a press on a tier set only drafts, and Apply posts the
draft to the boards' settings route (Karar 86 A), which stores it and re-renders the Settings card store-only. It
saves a choice and fetches nothing. **Refresh the example rows** (Karar 90 A) reads five rows of one serving table
again, read-only, and calls no provider.
"""
from __future__ import annotations

import pytest

from RevenueChaser.core import board_settings as bs
from RevenueChaser.core import rawstore
from RevenueChaser.core.tests.panel_client import panel_test_client


@pytest.fixture
def client(rawstore_tmp_db, monkeypatch):
    from RevenueChaser.app import dashboard
    submitted = []
    monkeypatch.setattr(dashboard.jobs, "submit",
                        lambda module, params=None: submitted.append((module, params)) or "job7")
    dashboard.app.config["TESTING"] = True
    c = panel_test_client(dashboard.app)
    c.submitted = submitted
    return c


def test_apply_stores_the_universe_and_re_renders_the_settings_card_store_only(client):
    r = client.post("/api/settings/board/module_settings", json={"changes": {"UNIVERSE_SETS": 3,
                                                                             "UNIVERSE_INCLUDE": 5}})
    assert r.status_code == 202, r.get_json()
    assert r.get_json()["values"]["values"] == {"UNIVERSE_SETS": 3, "UNIVERSE_INCLUDE": 5}
    assert client.submitted == [("settings_refresh", {})]
    assert bs.get("module_settings", "UNIVERSE_SETS") == 3


def test_an_empty_universe_is_refused_and_nothing_is_written(client):
    r = client.post("/api/settings/board/module_settings", json={"changes": {"UNIVERSE_SETS": 0}})
    assert r.status_code == 400
    assert rawstore.count("board_settings") == 0 and client.submitted == []


# --- Refresh the example rows: one read-only SELECT of a serving table, no cache, no provider ------------------------

import duckdb  # noqa: E402

from RevenueChaser.module_settings import newdata  # noqa: E402
from RevenueChaser.rcdata import serving  # noqa: E402
from RevenueChaser.rcdata.build.prices import serving as prices_serving  # noqa: E402
from RevenueChaser.rcdata.tests.conftest import market_roots  # noqa: E402,F401 - market roots per test


def _bars(days):
    path = serving.live_path("prices")
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    try:
        cols = ", ".join(f'"{c}" {t}' for c, t in prices_serving.SCHEMAS["price_bar_daily"])
        con.execute(f"CREATE TABLE price_bar_daily ({cols})")
        for i, day in enumerate(days):
            con.execute("INSERT INTO price_bar_daily (security_id, session_date, close, source) VALUES "
                        "(?, ?, ?, 'alpaca/bars')", [f"S{i}", day, float(i)])
    finally:
        con.close()


@pytest.fixture
def reads(monkeypatch):
    """Every serving file the route opens."""
    opened = []
    real = serving.open_read
    monkeypatch.setattr(serving, "open_read", lambda kind: opened.append(kind) or real(kind))
    newdata.clear_sample_cache()
    yield opened
    newdata.clear_sample_cache()


def _get(client, table):
    """GET the sample route with every DuckDB file it opens held to read-only."""
    connect = duckdb.connect

    def guarded(path=":memory:", read_only=False, **kw):
        assert read_only, f"the sample route opened {path} for writing"
        return connect(path, read_only=read_only, **kw)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(duckdb, "connect", guarded)
        return client.get(f"/api/settings/sample/{table}")


def test_refresh_reads_the_newest_five_rows_read_only(client, market_roots, reads):
    days = [f"2026-10-{d:02d}" for d in range(20, 29)]
    _bars(days)
    r = _get(client, "price_bar_daily")
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["ok"] is True and body["table"] == "price_bar_daily"
    names = [c["name"] for c in body["columns"]]
    assert names == [c for c, _ in prices_serving.SCHEMAS["price_bar_daily"]]
    day = names.index("session_date")
    assert [row[day] for row in body["rows"]] == sorted(days, reverse=True)[:newdata.SAMPLE_ROWS]
    assert reads == ["prices"], "one read-only handle on the table's own file, nothing else"
    assert client.submitted == [], "a read queues no job"


def test_refresh_reads_again_rather_than_answering_from_the_hourly_cache(client, market_roots, reads, monkeypatch):
    _bars(["2026-10-27", "2026-10-28"])
    monkeypatch.setattr(newdata, "_sample_key", lambda table, now: "this hour")
    stale = {"columns": [{"name": "close", "type": "DOUBLE"}], "rows": [[0.5]]}
    assert newdata._cached_sample("price_bar_daily", None, lambda t: stale) is stale
    fresh = _get(client, "price_bar_daily").get_json()
    assert len(fresh["rows"]) == 2, "the press reads the file, not the page's hourly sample"
    assert newdata._cached_sample("price_bar_daily", None, lambda t: stale)["rows"] == fresh["rows"], \
        "and the next render shows what the press read"


def test_an_unknown_table_is_404_and_opens_nothing(client, market_roots, reads):
    for name in ("no_such_table", "price_bar_daily; DROP TABLE x", "board_settings"):
        r = _get(client, name)
        assert r.status_code == 404 and r.get_json()["ok"] is False, name
    assert reads == []


def test_a_file_never_built_is_409_and_says_so(client, market_roots, reads):
    r = _get(client, "price_bar_daily")
    assert r.status_code == 409
    assert r.get_json() == {"ok": False, "error": "the prices file has not been built yet"}


def test_a_table_missing_from_its_built_file_is_409(client, market_roots, reads):
    path = serving.live_path("prices")
    path.parent.mkdir(parents=True, exist_ok=True)
    duckdb.connect(str(path)).close()
    r = _get(client, "price_bar_daily")
    assert r.status_code == 409 and "price_bar_daily" in r.get_json()["error"]


def test_a_file_that_cannot_be_read_right_now_is_503_not_a_crash(client, market_roots, reads, monkeypatch):
    """Review S5: a press during a serving swap (the file locked or half-written) says so; it is not a 500."""
    _bars(["2026-10-28"])

    def locked(kind):
        raise duckdb.IOException("Could not set lock on file")
    monkeypatch.setattr(serving, "open_read", locked)
    r = _get(client, "price_bar_daily")
    assert r.status_code == 503
    body = r.get_json()
    assert body["ok"] is False and "could not be read right now" in body["error"] and "lock" in body["error"]
