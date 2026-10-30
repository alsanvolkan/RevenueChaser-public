"""The control board's model (design v28): ``control.build`` arranges what ``newdata.collect`` read and what the code
records, and opens nothing itself."""
from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest

from RevenueChaser.core.tests.board_fixtures import newdata_fixture
from RevenueChaser.module_settings import control, newdata
from RevenueChaser.rcdata import scaffold, serving
from RevenueChaser.rcdata.api import screen

NOW = datetime(2026, 10, 30, 17, 31, tzinfo=timezone.utc)


def _card(name, status="fresh", **kw):
    base = copy.deepcopy(newdata_fixture()["datasets"][0])
    base.update({"dataset": name, "label": screen.DATASETS[name].label, "status": status, "status_reason": None,
                 "checked_on": "2026-10-29", "issues": [], "issues_open": 0, "attended": False,
                 "attended_command": None, "recent": [], "samples": [],
                 "schedule": {"paused": False, "paused_by": None, "paused_at": None}})
    base.update(kw)
    return base


def control_nd() -> dict:
    nd = newdata_fixture()
    nd["datasets"] = [
        _card("alpaca/bars", "late", issues_open=3, coverage=0.97, coverage_target=0.99),
        _card("sec/13f", "never", checked_on=None),
        _card("wrds/prices", "never", checked_on=None, attended=True,
              attended_command="python -m RevenueChaser.rcdata.build backfill wrds/prices"),
        _card("spine/security", "fresh", issues_open=7, cadence="weekly", checked_on="2026-10-26"),
        _card("market_series", "stale"),
    ]
    nd["verified"]["silent_minutes"] = 24
    nd["universe"] = {"counts": {"A": 5655, "E": 558, "R": 1457, "C": 2577, "D": 183}, "total": 10430,
                      "tickers": {"E": ["SPY", "QQQ"], "R": ["XYZW"]}}
    nd["sessions"] = {"2026-10-27": 7400, "2026-10-28": 7380}
    nd["identity"] = {"as_of": "2026-10-28"}
    nd["storage"] = {"roots": {"raw": "/r", "parquet": "/p", "ssd": "/s"},
                     "files": {"prices": {"path": "/s/prices.duckdb", "bytes": 1_200_000_000}},
                     "datasets": {"alpaca/bars": {"raw": {"path": "/r/alpaca/bars", "files": 52, "bytes": 31_000_000,
                                                          "capped": False},
                                                  "parquet": {"path": "/p/alpaca/bars", "files": 52,
                                                              "bytes": 9_000_000, "capped": False}}}}
    nd["tables"] = {"price_bar_daily": {"rows": 18_412_904, "newest": "2026-10-28"}}
    nd["spend"]["providers"] = [{"provider": "alphavantage", "cap": 25, "today": 18, "week": 30},
                                {"provider": "sec", "cap": 40000, "today": 20100, "week": 30},
                                {"provider": "uw", "cap": 150000, "today": 4512, "week": 30}]
    return nd


def _build(nd=None, **kw):
    return control.build(control_nd() if nd is None else nd, now=NOW, **kw)


def test_the_board_menu_is_the_dashboards():
    from RevenueChaser.app import dashboard
    menu, group = [], None
    for t in dashboard.TABS:
        if t["type"] == "header":
            group = (t["label"], [])
            menu.append(group)
        else:
            group[1].append((t["key"], t["label"]))
    assert [(g, tuple(tabs)) for g, tabs in menu] == [(g, tabs) for g, tabs in control.BOARD_MENU]


def test_the_build_opens_no_store(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("the model opened a serving file")
    monkeypatch.setattr(serving, "open_read", refuse)
    view = _build()
    assert view["error"] is None and view["files"]


def test_a_collect_error_is_carried_not_raised():
    assert _build({"error": "RuntimeError: x"})["error"] == "RuntimeError: x"


def test_the_health_strip_has_its_twelve_tiles_counted_from_the_cards():
    h = _build()["health"]
    keys = [t["key"] for t in h["tiles"]]
    assert keys == ["fresh", "late", "stale", "never", "problems", "session", "queue", "builder", "cap", "universe",
                    "gateway", "backup"]
    tile = {t["key"]: t for t in h["tiles"]}
    assert (tile["fresh"]["n"], tile["late"]["n"], tile["stale"]["n"], tile["never"]["n"]) == ("1", "1", "1", "2")
    assert tile["problems"]["n"] == "10"
    assert tile["session"]["n"] == "28 Oct" and "7,380 names" in tile["session"]["label"]
    assert tile["session"]["cls"] == "warn", "alpaca/bars is late, so the newest session is not complete"
    assert tile["cap"]["n"] == "72%" and "Alpha Vantage" in tile["cap"]["label"] and tile["cap"]["cls"] == "bad"
    assert tile["builder"]["n"] == "24 min" and tile["builder"]["cls"] == "ok"
    assert tile["queue"]["n"] == "1"


def test_a_silent_builder_turns_its_tile_red():
    nd = control_nd()
    nd["verified"]["silent_minutes"] = control.BUILDER_SILENT_MINUTES + 1
    assert {t["key"]: t for t in _build(nd)["health"]["tiles"]}["builder"]["cls"] == "bad"


def test_the_default_universe_is_a_and_b_and_asks_no_scope():
    v = _build()
    u = v["universe"]
    assert [(s["key"], s["on"]) for s in u["sets"]] == [("A", True), ("E", True), ("R", True), ("C", False),
                                                        ("D", False)]
    assert u["size"] == 5655 + 558 + 1457 and u["default"] is True
    assert v["scope"] is None, "the default universe is the daily run: no scope, it takes them all"
    assert u["filled"] == 7380


def test_a_narrowed_universe_asks_its_tiers_and_the_names_of_a_part_of_b():
    only_a = _build(applied={"UNIVERSE_SETS": 1, "UNIVERSE_INCLUDE": 7})
    assert only_a["scope"] == {"tiers": ["A"], "names": [], "include": ["anchors", "watchlist", "cover_classes"]}
    funds = _build(applied={"UNIVERSE_SETS": 1 | 2, "UNIVERSE_INCLUDE": 0})
    assert funds["scope"] == {"tiers": ["A"], "names": ["SPY", "QQQ"], "include": []}
    assert funds["universe"]["size"] == 5655 + 558


def test_c_and_d_are_never_in_the_universe_whatever_is_saved():
    v = _build(applied={"UNIVERSE_SETS": 255})
    assert not any(s["on"] for s in v["universe"]["sets"] if not s["offered"])


def test_the_files_come_in_build_order_with_the_columns_of_the_code():
    files = _build()["files"]
    assert [f["kind"] for f in files] == list(screen.FILES)
    prices = next(f for f in files if f["kind"] == "prices")
    assert [t["name"] for t in prices["tables"]] == list(scaffold.SCHEMA_OF_KIND["prices"])
    bars = next(t for t in prices["tables"] if t["name"] == "price_bar_daily")
    assert bars["columns"] == [(c, str(t)) for c, t in scaffold.SCHEMA_OF_KIND["prices"]["price_bar_daily"]]
    assert bars["rows"] == 18_412_904
    assert [d["dataset"] for d in prices["datasets"]] == ["alpaca/bars", "wrds/prices"]
    assert prices["bytes"] == 1_200_000_000


def test_a_declared_read_is_solid_and_a_planned_one_is_marked():
    files = {f["kind"]: f for f in _build()["files"]}
    series = next(t for t in files["market"]["tables"] if t["name"] == "market_series")
    radar = next(r for r in series["readers"] if r["key"] == "module_earnings_radar")
    assert radar["declared"] is True and "macro.dgs10" in radar["needs"]
    bars = next(t for t in files["prices"]["tables"] if t["name"] == "price_bar_daily")
    comov = next(r for r in bars["readers"] if r["key"] == "module_comovement")
    assert comov["declared"] is False


def test_the_fill_row_follows_the_scope_the_dataset_honours():
    rows = {d["dataset"]: d for f in _build()["files"] for d in f["datasets"]}
    cells = rows["alpaca/bars"]["fill"]["cells"]
    assert [c["key"] for c in cells] == ["A", "E", "R"], "C and D are not offered: no cell"
    assert rows["sec/13f"]["fill"]["cells"] == [] and rows["sec/13f"]["fill"]["why"] == screen.NO_TIER_WORDS["ownership"]
    assert rows["wrds/prices"]["fill"]["why"] == screen.NO_TIER_ATTENDED


def test_the_fill_buttons_are_off_unless_the_server_says_so(monkeypatch):
    monkeypatch.delenv(screen.FILL_ENV, raising=False)
    assert _build()["fill_enabled"] is False
    monkeypatch.setenv(screen.FILL_ENV, "1")
    assert _build()["fill_enabled"] is True


def test_the_providers_are_sorted_nearest_their_cap_first():
    rows = _build()["providers"]
    assert [r["id"] for r in rows[:3]] == ["alphavantage", "sec", "uw"]
    assert rows[0]["cls"] == "bad" and rows[1]["cls"] == "warn" and rows[2]["cls"] == ""
    assert {r["id"] for r in rows} == set(screen.PROVIDERS)
    assert next(r for r in rows if r["id"] == "cboe")["cap"] is None


def test_every_line_of_the_flow_diagram_ends_at_a_box_it_draws():
    flow = _build()["flow"]
    sources = {f"{g['group']}|{i['name']}" for g in flow["sourceGroups"] for i in g["items"]}
    steps = {s["id"] for s in flow["steps"]}
    tables = {t for f in flow["files"] for t in f["tables"]}
    boards = {b["name"] for g in flow["boardGroups"] for b in g["boards"]}
    assert all(s in sources and t in steps for s, t in flow["edges"]["srcStep"])
    assert all(s in steps and t in tables for s, t in flow["edges"]["stepTab"])
    assert all(e[0] in tables and e[1] in boards for e in flow["edges"]["tabBoard"])
    assert ["market_series", "Earnings Radar"] in flow["edges"]["tabBoard"]
    assert ["price_bar_daily", "Co-movement", "planned"] in flow["edges"]["tabBoard"]
    assert flow["steps"][1]["label"] == "2 · Tiers · A 5,655 · B 2,015 · C 2,577 · D 183"


def test_the_flow_sources_are_the_datasets_providers():
    flow = _build()["flow"]
    prices = next(g for g in flow["sourceGroups"] if g["group"] == "Prices")
    assert {"Alpaca", "WRDS", "SEC", "yfinance", "UW"} <= {i["name"] for i in prices["items"]}
    assert all(i["icon"] for g in flow["sourceGroups"] for i in g["items"])


def test_the_bars_calendar_reads_the_sessions_and_the_page_size():
    from RevenueChaser.rcdata.build.prices import bars
    cal = _build()["calendars"]["alpaca/bars"]
    assert cal["names_per_session"] == {"2026-10-27": 7400, "2026-10-28": 7380}
    assert cal["page"] == bars.LIMIT and cal["universe"] == 5655 + 558 + 1457 and cal["groups"] is True
