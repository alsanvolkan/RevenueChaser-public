"""The control board's model (design v28): ``control.build`` arranges what ``newdata.collect`` read and what the code
records, and opens nothing itself."""
from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest

from RevenueChaser.core.tests.board_fixtures import newdata_fixture
from RevenueChaser.module_settings import control, newdata
from RevenueChaser.rcdata import scaffold, serving
from RevenueChaser.rcdata.needs import screen

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
        _card("alpaca/bars", "late", issues_open=3, coverage=0.97, coverage_target=0.99,
              cost={"provider": "alpaca", "calls_per_partition": 1, "remaining": None, "cap": None}),
        _card("sec/13f", "never", checked_on=None,
              cost={"provider": "sec", "calls_per_partition": 1, "remaining": 19_900, "cap": 40_000}),
        _card("wrds/prices", "never", checked_on=None, attended=True,
              attended_command="python -m RevenueChaser.rcdata.build backfill wrds/prices"),
        _card("spine/security", "fresh", issues_open=7, cadence="weekly", checked_on="2026-10-26"),
        _card("market_series", "stale"),
    ]
    nd["verified"]["silent_minutes"] = 24
    nd["universe"] = {"counts": {"A": 5655, "E": 558, "R": 1457, "C": 2577, "D": 183}, "total": 10430,
                      "tickers": {"E": ["SPY", "QQQ"], "R": ["XYZW"]}}
    nd["sessions"] = {"2026-10-27": {"A": 5600, "E": 500, "R": 1300, "C": 40},
                      "2026-10-28": {"A": 5600, "E": 480, "R": 1300, "C": 40, "-": 2}}
    nd["identity"] = {"as_of": "2026-10-28"}
    nd["storage"] = {"roots": {"raw": "/r", "parquet": "/p", "ssd": "/s"},
                     "files": {"prices": {"path": "/s/prices.duckdb", "bytes": 1_200_000_000}},
                     "datasets": {"alpaca/bars": {"raw": {"path": "/r/alpaca/bars", "files": 52, "bytes": 31_000_000,
                                                          "capped": False},
                                                  "parquet": {"path": "/p/alpaca/bars", "files": 52,
                                                              "bytes": 9_000_000, "capped": False}}},
                     "walked_at": "2026-10-28T07:30:00+00:00"}
    nd["tables"] = {"price_bar_daily": {"rows": 18_412_904, "newest": "2026-10-28"}}
    nd["spend"]["providers"] = [{"provider": "alphavantage", "cap": 25, "today": 18, "week": 30},
                                {"provider": "sec", "cap": None, "today": 20100, "week": 30},
                                {"provider": "uw", "cap": 100000, "today": 60_000, "week": 30}]
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


def test_the_health_strip_has_its_fourteen_tiles_counted_from_the_cards():
    h = _build()["health"]
    keys = [t["key"] for t in h["tiles"]]
    assert keys == ["fresh", "late", "stale", "never", "problems", "session", "queue", "timer", "builder", "cap",
                    "universe", "longest", "backup"]
    tile = {t["key"]: t for t in h["tiles"]}
    assert (tile["fresh"]["n"], tile["late"]["n"], tile["stale"]["n"], tile["never"]["n"]) == ("1", "1", "1", "2")
    assert tile["problems"]["n"] == "10"
    assert tile["session"]["n"] == "28 Oct"
    assert f"{tile['universe']['n']} of 7,670 names" in tile["session"]["label"], "the universe's own filled count"
    assert tile["session"]["cls"] == "warn", "wrds/prices serves the bars and never ran, so the session is not complete"
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
    assert u["filled"] == 7380, "the picked sets' names only: C's bars and those off the spine are not the universe's"


def test_a_narrowed_universe_asks_its_tiers_and_the_names_of_a_part_of_b():
    only_a = _build(applied={"UNIVERSE_SETS": 1, "UNIVERSE_INCLUDE": 7})
    assert only_a["scope"] == {"tiers": ["A"], "names": [], "include": ["anchors", "watchlist", "cover_classes"]}
    funds = _build(applied={"UNIVERSE_SETS": 1 | 2, "UNIVERSE_INCLUDE": 0})
    assert funds["scope"] == {"tiers": ["A"], "names": ["SPY", "QQQ"], "include": []}
    assert funds["universe"]["size"] == 5655 + 558
    only_funds = _build(applied={"UNIVERSE_SETS": 2, "UNIVERSE_INCLUDE": 7})["universe"]
    assert (only_funds["size"], only_funds["filled"]) == (558, 480), \
        "review M1: a fund-only universe is not filled by Tier A's bars"


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
    assert [d["dataset"] for d in prices["datasets"]] == ["wrds/prices", "alpaca/bars"]
    assert prices["bytes"] == 1_200_000_000


def test_a_declared_read_is_solid_and_a_planned_one_is_marked():
    files = {f["kind"]: f for f in _build()["files"]}
    series = next(t for t in files["market"]["tables"] if t["name"] == "market_series")
    radar = next(r for r in series["readers"] if r["key"] == "module_earnings_radar")
    assert radar["declared"] is True and "macro.dgs10" in radar["needs"]
    bars = next(t for t in files["prices"]["tables"] if t["name"] == "price_bar_daily")
    assert next(r for r in bars["readers"] if r["key"] == "module_comovement")["declared"] is True   # #642 PR 6
    swing = next(r for r in bars["readers"] if r["key"] == "module_swing_potential")
    assert swing["declared"] is False


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
    assert rows[0]["id"] == "alphavantage" and rows[0]["cls"] == "bad"   # the only day cap (caps.DAY_CAPS)
    assert all(r["cap"] is None and r["cls"] == "" for r in rows[1:])  # sec's 20,100 calls: a window, no day cap
    assert next(r for r in rows if r["id"] == "uw")["cap"] is None     # UW's cap removed, avo 2026-11-10
    assert {r["id"] for r in rows} == set(screen.PROVIDERS)
    assert next(r for r in rows if r["id"] == "cboe")["cap"] is None


def test_every_line_of_the_flow_diagram_ends_at_a_box_it_draws():
    flow = _build()["flow"]
    sources = {f"{g['group']}|{i['name']}" for g in flow["sourceGroups"] for i in g["items"]}
    tables = {t for f in flow["files"] for t in f["tables"]}
    boards = {b["name"] for g in flow["boardGroups"] for b in g["boards"]}
    bfiles = {f["name"] for f in flow["boardFiles"]}
    assert all(s in sources and t in tables for s, t in flow["edges"]["srcTab"])
    assert all(e[0] in tables and e[1] in boards for e in flow["edges"]["tabBoard"])
    assert all(b in boards and f in bfiles for b, f in flow["edges"]["boardOwn"] + flow["edges"]["boardRead"])
    assert ["market_series", "Earnings Radar"] in flow["edges"]["tabBoard"]
    assert ["price_bar_daily", "Co-movement"] in flow["edges"]["tabBoard"]               # declared since #642 PR 6
    assert ["price_bar_daily", "Swing Potential", "planned"] in flow["edges"]["tabBoard"]
    # the plan step column is gone (avo, 10 Nov 2026); its tiers sit under the spine's box
    spine = next(f for f in flow["files"] if f["name"] == "spine.duckdb")
    assert spine["note"].endswith("A 5,655 · B 2,015 · C 2,577 · D 183")


def test_the_flow_sources_are_the_datasets_providers():
    flow = _build()["flow"]
    prices = next(g for g in flow["sourceGroups"] if g["group"] == "Prices")
    assert {"Alpaca", "WRDS", "SEC", "yfinance", "Unusual Whales"} <= {i["name"] for i in prices["items"]}
    assert all(i["icon"] for g in flow["sourceGroups"] for i in g["items"])


def test_the_bars_calendar_reads_the_sessions_and_the_page_size():
    from RevenueChaser.rcdata.build.prices import bars
    cal = _build()["calendars"]["alpaca/bars"]
    assert cal["names_per_session"] == control_nd()["sessions"], "by set: the page sums the sets the universe holds"
    assert cal["page"] == bars.LIMIT and cal["universe"] == 5655 + 558 + 1457 and cal["groups"] is True


def test_the_fetch_line_names_its_provider_in_words():
    rows = {d["dataset"]: d for f in _build()["files"] for d in f["datasets"]}
    assert rows["market_series"]["fetch"]["provider"] == screen.PROVIDERS["uw"].name == "Unusual Whales"
    assert rows["sec/13f"]["fetch"]["provider"] == screen.PROVIDERS["sec"].name


def test_the_spine_file_lists_the_etf_datasets_in_build_order_and_the_etf_table():
    """#346: the two halves of the ETF list are step 1 rows of spine.duckdb, and ``etf`` is one of its tables,
    written by ``spine/security`` (the Data tab, "Data, file by file")."""
    nd = control_nd()
    nd["datasets"] += [_card("alphavantage/listing_status", "never", checked_on=None),
                       _card("sec/ncen", "never", checked_on=None)]
    nd["tables"]["etf"] = {"rows": 6_200, "newest": "2026-11-02"}
    spine = next(f for f in _build(nd)["files"] if f["kind"] == "spine")
    rows = [(d["dataset"], d["step"], d["role"]) for d in spine["datasets"]]
    assert rows[:2] == [("alphavantage/listing_status", 1, "first"), ("sec/ncen", 1, "fills")]
    assert rows[-1][0] == "spine/security"
    av = next(d for d in spine["datasets"] if d["dataset"] == "alphavantage/listing_status")
    assert av["providers"] == ("alphavantage",) and av["fetch"]["self"] is True
    etf = next(t for t in spine["tables"] if t["name"] == "etf")
    assert etf["writers"] == ["spine/security"] and (etf["rows"], etf["newest"]) == (6_200, "2026-11-02")
    assert [c for c, _ in etf["columns"]][:5] == ["ticker", "name", "series_id", "cik", "source"]


def test_the_providers_card_names_alpha_vantage_with_its_cap_and_the_sec_with_n_cen():
    providers = {p["id"]: p for p in _build()["providers"]}
    assert providers["alphavantage"]["cap"] == 25 and "listing status" in providers["alphavantage"]["what"]
    assert providers["alphavantage"]["icon"] and "N-CEN" in providers["sec"]["what"]


def test_ncens_monthly_cadence_is_the_datasets_own_not_a_second_copy_on_its_card():
    """Review N6 of PR 349: the dataset carries ``cadence = "monthly"``; the screen's card does not repeat it."""
    from RevenueChaser.module_settings import newdata as nd_mod
    assert screen.DATASETS["sec/ncen"].cadence == "daily"           # the card's default, not read for this dataset
    assert nd_mod._cadence("sec/ncen", None) == "monthly"


# ── the journey table's three folder states (#434) ──

def test_the_folder_line_tells_not_walked_from_no_folder_from_counted():
    """#434: the counts come from the storage-picture job, so "I have not looked" is a third state. Before it, a
    board with no picture rendered every dataset as "no folder yet" -- the same words as a dataset whose folder
    really is missing."""
    from RevenueChaser.module_settings import control_render

    walked = control_render.render_data(_build())
    assert "52 files" in walked and "not walked yet" not in walked

    nd = control_nd()
    nd["storage"] = {**nd["storage"], "datasets": {}, "walked_at": None}
    never = control_render.render_data(_build(nd))
    assert "not walked yet" in never and "no folder yet" not in never, "no picture is not an empty folder"

    nd = control_nd()
    nd["storage"] = {**nd["storage"],
                     "datasets": {"alpaca/bars": {"raw": {"files": None, "bytes": None, "capped": False},
                                                  "parquet": {"files": None, "bytes": None, "capped": False}}}}
    missing = control_render.render_data(_build(nd))
    assert "no folder yet" in missing, "a dataset whose folder really is missing still says so"


def test_the_roots_line_says_when_the_picture_was_taken_and_offers_the_control():
    from RevenueChaser.module_settings import control_render

    assert control_render._walked(None) == "not walked yet"
    fresh = control_render._walked("2026-11-05T12:00:00+00:00", datetime(2026, 11, 5, 13, 0, tzinfo=timezone.utc))
    assert fresh.startswith("walked ") and "h ago" not in fresh, fresh
    old = control_render._walked("2026-11-01T12:00:00+00:00", datetime(2026, 11, 5, 13, 0, tzinfo=timezone.utc))
    assert "h ago" in old, old
    assert control_render.ROUTES["picture"] == "/run/storage-picture"
