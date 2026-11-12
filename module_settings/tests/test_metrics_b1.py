"""#527 plan B1: ``GET /metrics`` for Prometheus and ``GET /api/build/datasets`` for the Grafana buttons' allow-list.

The scrape reads only the ops store and small files: it never opens the serving DuckDB files, so it is exempt from
the dashboard's per-request store session. A part that cannot be read leaves its family empty, never the scrape.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from RevenueChaser.core import config
from RevenueChaser.core.tests.panel_client import panel_test_client
from RevenueChaser.module_settings import control, metrics
from RevenueChaser.rcdata import ops

NOW = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)          # a Monday, New York session open
LINE = re.compile(r'^[a-z_:][a-z0-9_:]*(\{([a-z_]+="([^"\\]|\\.)*",?)*\})? -?[0-9.e+-]+$')


@pytest.fixture(autouse=True)
def _market(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MARKET_DIR", tmp_path / "market")
    monkeypatch.setattr(config, "MARKET_RAW_DIR", tmp_path / "market_raw")
    monkeypatch.setattr(ops, "read_log", ops.ReadLog(process="test"))


@pytest.fixture
def client():
    from RevenueChaser.app.dashboard import app
    return panel_test_client(app)


def _families(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for line in text.splitlines():
        if line.startswith("# TYPE "):
            out.setdefault(line.split()[2], [])
        elif line and not line.startswith("#"):
            out.setdefault(re.split(r"[{ ]", line, maxsplit=1)[0], []).append(line)
    return out


DS = {"market_series": SimpleNamespace(kind="market", providers=("fred",), attended=False),
      "wrds/prices": SimpleNamespace(kind="prices", providers=("wrds",), attended=True)}


def _collect(**kw):
    kw.setdefault("datasets", DS)
    kw.setdefault("timer", {"state": "on"})
    kw.setdefault("ibkr", lambda: {"state": "up"})
    return metrics.collect(NOW, **kw)


def test_every_line_is_valid_exposition_and_every_family_has_its_type():
    text = metrics.render(_collect(), 0.01)
    types = [l.split()[2] for l in text.splitlines() if l.startswith("# TYPE ")]
    assert types == list(metrics.HELP), "one # TYPE per family, in a fixed order"
    for line in text.splitlines():
        assert line.startswith("# ") or LINE.match(line), f"not exposition text: {line!r}"
    assert "rc_metrics_build_seconds 0.01" in text


def test_a_dataset_reports_its_state_from_its_newest_check():
    ops.record_check("market_series", "changed", checked_on="2026-11-09", source_as_of="2026-11-06")
    ops.record_check("wrds/prices", "failed", checked_on="2026-11-09")
    fam = _families(metrics.render(_collect()))
    state = {l for l in fam["rc_dataset_state"] if l.endswith(" 1")}
    assert state == {'rc_dataset_state{dataset="market_series",kind="market",state="fresh"} 1',
                     'rc_dataset_state{dataset="wrds/prices",kind="prices",state="stale"} 1'}
    assert len(fam["rc_dataset_state"]) == 2 * len(metrics.STATES), "every state present, 0 or 1"
    assert 'rc_dataset_last_check_result{dataset="wrds/prices",kind="prices"} 2' in fam["rc_dataset_last_check_result"]
    assert 'rc_dataset_last_check_result{dataset="market_series",kind="market"} 0' in fam["rc_dataset_last_check_result"]


def test_a_dataset_never_checked_reads_never_and_has_no_result():
    fam = _families(metrics.render(_collect()))
    assert 'rc_dataset_state{dataset="market_series",kind="market",state="never"} 1' in fam["rc_dataset_state"]
    assert fam["rc_dataset_last_check_result"] == []


def test_provider_calls_and_the_day_cap():
    ops.count_calls("uw", 7, failures=1)                   # counted on today's New York day, so read at today
    fam = _families(metrics.render(metrics.collect(datetime.now(timezone.utc), datasets=DS, timer={"state": "on"},
                                                   ibkr=lambda: None)))
    assert 'rc_provider_calls_today{provider="uw"} 7' in fam["rc_provider_calls_today"]
    assert 'rc_provider_failures_today{provider="uw"} 1' in fam["rc_provider_failures_today"]
    assert 'rc_provider_rate_limited_today{provider="uw"} 0' in fam["rc_provider_rate_limited_today"]


def test_a_provider_s_429_answers_today_are_exported():
    """#715: the provider limits card leaves the page; a 429 was on that card and on no metric."""
    ops.count_calls("fmp", 3, failures=2, http_429=2)
    fam = _families(metrics.render(metrics.collect(datetime.now(timezone.utc), datasets=DS, timer={"state": "on"},
                                                   ibkr=lambda: None)))
    assert 'rc_provider_rate_limited_today{provider="fmp"} 2' in fam["rc_provider_rate_limited_today"]
    assert not any(l.startswith('rc_provider_day_cap{provider="uw"} ') for l in fam["rc_provider_day_cap"])  # no UW cap
    assert any(l.startswith('rc_provider_day_cap{provider="alphavantage"} ') for l in fam["rc_provider_day_cap"])


def test_the_requests_answered_per_need_today_are_exported():
    """#715 (avo, 12 Nov 2026): the Logs tab starts with daily counters. The read log keeps a day, a process and a
    need, so the count is per need and day, every process together; a name that is not a need's is left out."""
    for need, n in (("prices.daily", 3), ("fundamentals.income", 1), ('bad"name', 2)):
        for _ in range(n):
            ops.read_log.bump(f"need:{need}")
    ops.read_log.bump("some_table")
    ops.read_log.flush()
    fam = _families(metrics.render(metrics.collect(datetime.now(timezone.utc), datasets=DS, timer={"state": "on"},
                                                   ibkr=lambda: None)))
    assert fam["rc_need_reads_today"] == ['rc_need_reads_today{need="fundamentals.income"} 1',
                                          'rc_need_reads_today{need="prices.daily"} 3']


def test_the_timer_unknown_leaves_its_family_empty_not_zero():
    assert _families(metrics.render(_collect(timer={"state": None})))["rc_builder_timer_on"] == []
    assert _families(metrics.render(_collect(timer={"state": "off"})))["rc_builder_timer_on"] == ["rc_builder_timer_on 0"]


@pytest.mark.parametrize("state, label", [("not set", "not_set"), ("silent", "silent"), ("up", "up"),
                                          ("down", "down"), ("garbage from the file", "unknown"), (None, "unknown")])
def test_ibkr_state_is_one_of_a_closed_set(state, label):
    """The review of #539: a hung gateway is "silent", and the record is a file, so other text must not become a
    label value. Every state is always present, 0 or 1, so an alert on one of them never sees a gap."""
    fam = _families(metrics.render(_collect(ibkr=lambda: {"state": state})))
    assert [l for l in fam["rc_ibkr_state"] if l.endswith(" 1")] == [f'rc_ibkr_state{{state="{label}"}} 1']
    assert len(fam["rc_ibkr_state"]) == len(metrics.IBKR_STATES)


def _tape(feeds, written_at="2026-11-11T09:40:00+00:00"):
    return lambda: {"feeds": feeds, "written_at": written_at}


def test_a_news_feed_s_state_is_one_of_a_closed_set_and_every_state_is_always_there():
    """#727: on 11 Nov 2026 both feeds were refused all day behind a green gateway. Each feed carries every state,
    0 or 1, so an alert on ``refused`` never sees a gap; text that is not a state reads ``unknown``."""
    fam = _families(metrics.render(_collect(news=_tape([{"feed": "BRFG", "state": "live"},
                                                        {"feed": "DJNL", "state": "refused"},
                                                        {"feed": "FLY", "state": "words from a file"}]))))
    ones = [l for l in fam["rc_news_feed_state"] if l.endswith(" 1")]
    assert ones == ['rc_news_feed_state{feed="BRFG",state="live"} 1',
                    'rc_news_feed_state{feed="DJNL",state="refused"} 1',
                    'rc_news_feed_state{feed="FLY",state="unknown"} 1']
    assert len(fam["rc_news_feed_state"]) == 3 * len(metrics.NEWS_STATES)


def test_a_feed_name_that_is_not_a_plain_id_never_becomes_a_label():
    fam = _families(metrics.render(_collect(news=_tape([{"feed": 'BR"FG} 1\nrc_x', "state": "live"},
                                                        {"feed": "", "state": "live"}]))))
    assert fam["rc_news_feed_state"] == []


def test_the_listener_s_last_write_is_exported_and_absent_without_a_record():
    fam = _families(metrics.render(_collect(news=_tape([]))))
    assert fam["rc_news_tape_written_timestamp_seconds"] == ["rc_news_tape_written_timestamp_seconds 1794390000.0"]
    fam = _families(metrics.render(_collect(news=_tape([], written_at=None))))
    assert fam["rc_news_tape_written_timestamp_seconds"] == []


def test_a_feed_id_with_a_hyphen_or_a_dot_is_still_reported():
    # IBKR's own provider codes: DJ-N, DJ-RT, BRFUPDN; dropping one silently would hide a refused feed
    fam = _families(metrics.render(_collect(news=_tape([{"feed": "DJ-N", "state": "refused"}]))))
    assert 'rc_news_feed_state{feed="DJ-N",state="refused"} 1' in fam["rc_news_feed_state"]


def test_a_record_the_listener_stopped_writing_reads_unknown_not_its_last_state():
    """Review of #742: a listener that died while its feeds were live would have read live for ever."""
    stale = lambda: {"feeds": [{"feed": "BRFG", "state": "live"}], "stale": True,   # noqa: E731
                     "written_at": "2026-11-11T09:40:00+00:00"}
    fam = _families(metrics.render(_collect(news=stale)))
    assert [l for l in fam["rc_news_feed_state"] if l.endswith(" 1")] == ['rc_news_feed_state{feed="BRFG",state="unknown"} 1']
    assert fam["rc_news_tape_written_timestamp_seconds"], "when it last wrote is still said"


def test_a_record_file_that_is_there_and_broken_is_a_failed_read_and_a_missing_one_is_not(tmp_path, monkeypatch):
    """Review of #742: the reader took a corrupt file for "the listener never started", so the counter never moved."""
    from RevenueChaser.module_news import tape_state
    path = tmp_path / "state.json"
    monkeypatch.setattr(tape_state, "state_path", lambda: path)
    before = metrics._errors["news"]
    assert _families(metrics.render(_collect()))["rc_news_feed_state"] == []
    assert metrics._errors["news"] == before, "no record yet is not an error"
    path.write_text("{not json", encoding="utf-8")
    assert _families(metrics.render(_collect()))["rc_news_feed_state"] == []
    assert metrics._errors["news"] == before + 1
    assert "cannot be read" in metrics.board_head(NOW, ibkr=lambda: None)["news"]["written_text"]


def test_a_news_record_that_cannot_be_read_is_counted_and_the_rest_still_reports():
    def boom():
        raise RuntimeError("tape read exploded")

    before = metrics._errors["news"]
    fam = _families(metrics.render(_collect(news=boom)))
    assert fam["rc_news_feed_state"] == [] and metrics._errors["news"] == before + 1
    assert fam["rc_ibkr_state"], "the other parts are still read"


def test_a_running_request_names_who_from_a_closed_set():
    ops.enqueue("market_series", by="settings")
    ops.next_request()
    ops.start_run("r1", "market_series", "queue")
    fam = _families(metrics.render(_collect(cgroup=lambda pid: "0::/system.slice/life-claude-remote-control-b.service",
                                            alive=lambda pid: True)))
    assert fam["rc_queue_requests"] == ['rc_queue_requests{status="queued"} 0', 'rc_queue_requests{status="running"} 1']
    elapsed = fam["rc_run_elapsed_seconds"]
    request = ops.read_queue(finished=0)["active"][0]["id"]
    # The review of #539: under Redis the run row was read from ops.sqlite, so "who" fell to "unknown" with no limit.
    assert len(elapsed) == 1 and elapsed[0].startswith(
        f'rc_run_elapsed_seconds{{id="{request}",dataset="market_series",who="claude"}} ')
    assert fam["rc_run_limit_seconds"] == [
        f'rc_run_limit_seconds{{id="{request}",dataset="market_series"}} {float(control.CLAUDE_SESSION_LIMIT_S)}']


@pytest.mark.parametrize("words, who", [("systemd life-rc-builder", "systemd"), ("Claude session", "claude"),
                                        ("process gone · probably lost", "lost"), ("unknown", "unknown"),
                                        ("anything else", "unknown")])
def test_who_words_map_to_the_enum(words, who):
    assert metrics._who(words) == who


def test_a_failing_part_is_counted_and_the_rest_still_reads(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("ops unreadable")
    monkeypatch.setattr(ops, "read_queue", boom)
    fam = _families(metrics.render(_collect()))
    assert fam["rc_queue_requests"] == []
    assert any(l.startswith('rc_metrics_read_errors_total{part="queue"}') for l in fam["rc_metrics_read_errors_total"])
    assert fam["rc_dataset_state"], "the other parts still read"


def test_one_unreadable_dataset_does_not_drop_the_others(monkeypatch):
    """The review of #539: the loop was one part, so a dataset that raised dropped every dataset after it."""
    real = ops.read_schedule
    monkeypatch.setattr(ops, "read_schedule",
                        lambda name: (_ for _ in ()).throw(RuntimeError("odd row")) if name == "market_series"
                        else real(name))
    before = metrics._errors["datasets"]
    fam = _families(metrics.render(_collect()))
    names = {re.search(r'dataset="([^"]+)"', l).group(1) for l in fam["rc_dataset_state"]}
    assert names == {"wrds/prices"}, "the dataset after the bad one still reports; the bad one is left out whole"
    assert metrics._errors["datasets"] == before + 1


def test_every_part_has_an_error_counter_from_the_start():
    """A counter that appears only at its first error hides that error from increase() and rate()."""
    fam = _families(metrics.render(_collect()))
    parts = {re.search(r'part="([a-z]+)"', l).group(1) for l in fam["rc_metrics_read_errors_total"]}
    assert parts == set(metrics.PARTS)


def test_thresholds_come_from_control():
    fam = _families(metrics.render(_collect()))
    assert f'rc_threshold{{name="builder_silent_minutes"}} {control.BUILDER_SILENT_MINUTES}' in fam["rc_threshold"]
    assert f'rc_threshold{{name="cap_alert_share"}} {control.CAP_ALERT!r}' in fam["rc_threshold"]


def test_a_label_value_is_escaped():
    text = metrics.render({"rc_switch_on": [({"name": 'a"b\\c\nd'}, 1)]})
    assert 'rc_switch_on{name="a\\"b\\\\c\\nd"} 1' in text


def test_the_timer_is_read_in_the_background_at_most_once_a_minute():
    calls, t = [], [0.0]
    timer = metrics._Timer(reader=lambda: calls.append(1) or {"state": "on"}, clock=lambda: t[0])
    assert timer.get(wait=True)["state"] == "on"
    timer.get(wait=True)
    assert len(calls) == 1, "a second scrape within the minute reuses the reading"
    t[0] = metrics.TIMER_EVERY_S
    timer.get(wait=True)
    assert len(calls) == 2


def test_the_route_serves_text_and_opens_no_store_session(client, monkeypatch):
    """No store session, and no DuckDB connection of any kind: a scrape every minute must never take raw.duckdb's
    lock (a session alone opens lazily, so the connect itself is what is refused here)."""
    from RevenueChaser.core import rawstore
    opened = []

    def refuse(*a, **k):
        opened.append(a)
        raise AssertionError("the scrape opened the store")
    monkeypatch.setattr(rawstore, "session", refuse)
    monkeypatch.setattr(rawstore, "connect", refuse)
    import duckdb
    monkeypatch.setattr(duckdb, "connect", refuse)
    monkeypatch.setattr(metrics.TIMER, "get", lambda wait=False: {"state": "on"})
    r = client.get("/metrics")
    assert r.status_code == 200
    assert r.mimetype == "text/plain"
    assert "version=0.0.4" in r.headers["Content-Type"]
    assert "# TYPE rc_dataset_state gauge" in r.get_data(as_text=True)
    assert opened == [], "a scrape must not open raw.duckdb"
    assert 'rc_dataset_state{dataset="market_series"' in r.get_data(as_text=True), \
        "the datasets were read with every DuckDB connect refused"


def test_the_datasets_route_lists_names_kinds_attended_and_providers(client):
    r = client.get("/api/build/datasets")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    names = [d["name"] for d in body["datasets"]]
    assert names == sorted(names) and "market_series" in names
    one = next(d for d in body["datasets"] if d["name"] == "market_series")
    assert set(one) == {"name", "kind", "attended", "providers"} and one["attended"] is False


def test_the_detail_carries_each_datasets_health_and_fetch_settings():
    from RevenueChaser.rcdata import ops
    ops.record_check("market_series", "changed", source_as_of="2026-11-06", rows=12, added=2, removed=1,
                     next_check_due="2026-11-10")
    ops.dataset_mode_set("market_series", "full", by="avo")
    ops.schedule_pause("wrds/prices", by="avo")
    rows = {r["name"]: r for r in metrics.datasets_detail(NOW, datasets=DS, timer={"state": "on", "minute": 5})}
    ms, wp = rows["market_series"], rows["wrds/prices"]
    assert (ms["mode"], ms["mode_own"], ms["paused"]) == ("full", "full", False)
    assert ms["last"]["result"] == "changed" and (ms["last"]["added"], ms["last"]["removed"]) == (2, 1)
    assert ms["runs"] and ms["runs"][0][1] == "changed" and ms["state"] in metrics.STATES
    assert ms["cadence"] and isinstance(ms["next"], str) and ms["open_issues"] == 0
    assert wp["paused"] is True and wp["paused_by"] == "avo" and wp["next"] == "skips until Resume"
    assert wp["last"] is None and wp["runs"] == [] and wp["state"] == "never"


def test_the_detail_route_answers_and_the_plain_route_keeps_its_four_keys(client):
    body = client.get("/api/build/datasets?detail=1").get_json()
    one = next(d for d in body["datasets"] if d["name"] == "market_series")
    assert {"state", "mode", "next", "runs", "paused"} <= set(one)
    plain = next(d for d in client.get("/api/build/datasets").get_json()["datasets"] if d["name"] == "market_series")
    assert set(plain) == {"name", "kind", "attended", "providers"}


def test_the_detail_carries_badges_and_the_runs_in_the_pickers_range():
    """avo, 10 Nov 2026: the RC page's provider icons on the Grafana board, and a heatmap over the picker's range."""
    from RevenueChaser.rcdata import ops
    ops.start_run("r-a", "market_series", "hourly")
    ops.start_run("r-b", "market_series", "hourly")
    ops.finish_run("r-b", "failed")
    first = min(r["started_at"] for r in ops.rows("job_runs2"))
    span = (first, first + 60)
    provs = [{"id": "fred", "name": "FRED"}]
    rows = {r["name"]: r for r in metrics.datasets_detail(NOW, datasets=DS, timer={}, span=span, providers=provs)}
    assert rows["market_series"]["badges"] == [{"id": "fred", "name": "FRED", "icon": metrics.control.ICONS.get("fred", ""),
                                                "color": metrics.control.SOURCE_COLORS[0], "paid": False}]
    from RevenueChaser.rcdata.needs import screen
    assert {p for p, w in screen.PROVIDERS.items() if w.paid} == {"uw"}, "avo, 10 Nov 2026: the paid ones ringed red"
    assert sorted(s for _, s in rows["market_series"]["history"]) == ["failed", "running"]
    before = {r["name"]: r for r in metrics.datasets_detail(NOW, datasets=DS, timer={}, span=(0, first),
                                                            providers=provs)}
    assert before["market_series"]["history"] == []
    assert rows["wrds/prices"]["history"] == []
    plain = {r["name"]: r for r in metrics.datasets_detail(NOW, datasets=DS, timer={}, providers=provs)}
    assert "history" not in plain["market_series"]


def test_the_board_head_says_the_switches_the_timer_and_the_gateway(monkeypatch):
    monkeypatch.setenv("RC_IBKR_LOGIN_URL", "javascript:alert(1)")
    monkeypatch.setattr(metrics.TIMER, "get", lambda wait=False: {"state": "on", "last": NOW, "next": None})
    head = metrics.board_head(NOW, ibkr=lambda: {"state": "up"})
    assert head["switches"] == {"fetch": True, "backup": True}
    assert head["timer"] == "on" and head["timer_fired"] == {"last": metrics.control._clock(NOW), "next": None}
    from RevenueChaser.rcdata import ops
    assert head["files"] == {k: True for k in ops.FILE_KINDS}
    ops.switch_set("file-options", False, by="avo")
    assert metrics.board_head(NOW, ibkr=lambda: None)["files"]["options"] is False
    assert head["gateway"] == {"cls": "ok", "n": "open", "label": "logged in", "login": None}
    monkeypatch.setenv("RC_IBKR_LOGIN_URL", "https://gateway.example/")
    assert metrics.board_head(NOW, ibkr=lambda: None)["gateway"]["login"] == "https://gateway.example/"


def test_the_board_head_says_when_fetch_and_backup_were_last_done(monkeypatch):
    """avo, 12 Nov 2026: "onlarin en son ne zaman oldugunu tam duzgun bir sekilde yaz" (the Grafana board's
    Fetch & Build and Backup switches): the builder's newest run and the newest good backup step, on the page's
    clock; not known is None, never a guess, and a read that raises does not take the head down."""
    from RevenueChaser.module_settings import newdata
    from RevenueChaser.rcdata import ops
    assert metrics.board_head(NOW, ibkr=lambda: None)["last"] == {"fetch": None, "backup": None}
    ran = datetime(2026, 11, 12, 22, 46, tzinfo=timezone.utc)
    monkeypatch.setattr(ops, "last_builder_run", lambda: {"dataset": "prices/daily", "status": "ok",
                                                          "started_at": ran.timestamp() - 60,
                                                          "finished_at": ran.timestamp()})
    monkeypatch.setattr(newdata, "_backups", lambda now: {"steps": [
        {"step": "duckdb", "last_ok": "2026-11-08T02:10:00+00:00"}, {"step": "ops", "last_ok": None},
        {"step": "boards", "last_ok": "2026-11-01T02:10:00+00:00"}]})
    local = metrics.control._local
    last = metrics.board_head(NOW, ibkr=lambda: None)["last"]
    assert last["fetch"] == {"at": f"12 Nov {local(ran):%H:%M}", "dataset": "prices/daily", "status": "ok"}
    assert last["backup"] == {"at": f"8 Nov {local(datetime(2026, 11, 8, 2, 10, tzinfo=timezone.utc)):%H:%M}"}
    monkeypatch.setattr(ops, "last_builder_run", lambda: {"dataset": "a", "status": "running",
                                                          "started_at": ran.timestamp(), "finished_at": None})
    assert metrics.board_head(NOW, ibkr=lambda: None)["last"]["fetch"]["status"] == "running"

    def boom(*a):
        raise RuntimeError("store busy")
    monkeypatch.setattr(ops, "last_builder_run", boom)
    monkeypatch.setattr(newdata, "_backups", boom)
    assert metrics.board_head(NOW, ibkr=lambda: None)["last"] == {"fetch": None, "backup": None}


def test_the_grafana_link_has_no_row_of_its_own():
    """avo, 12 Nov 2026: "Open in Grafana ... cok yer kapliyor": it lies on the frame's first row, left of Grafana's
    pickers, and stays a 44 px target."""
    from RevenueChaser.module_settings import report
    assert ".live-board{position:relative}" in report.LIVE_CSS
    assert ".live-board .livebar{position:absolute;top:11px;left:16px;z-index:1}" in report.LIVE_CSS
    assert "min-height:44px" in report.LIVE_CSS.split(".live-board .livebar a{")[1].split("}")[0]
    assert "margin:12px auto 8px" not in report.LIVE_CSS


def test_the_board_head_carries_the_news_feeds_in_the_listener_s_words():
    """#727: the Grafana board says the feeds beside the gateway; the words come from module_news/tape_state."""
    view = {"state": "refused", "lead": "2 of 2 feeds refused: BRFG, DJNL", "feeds": []}
    assert metrics.board_head(NOW, ibkr=lambda: None, news=lambda: view)["news"] == view

    def boom():
        raise RuntimeError("tape read exploded")

    assert metrics.board_head(NOW, ibkr=lambda: None, news=boom)["news"] is None
    assert metrics.board_head(NOW, ibkr=lambda: None)["news"]["state"] == "unknown", "no record yet reads as unknown"


def test_the_detail_route_takes_the_pickers_range_and_refuses_a_backwards_one(client):
    body = client.get("/api/build/datasets?detail=1&from=1000&to=5000").get_json()
    assert body["ok"] and "head" in body and all("history" in d for d in body["datasets"] if "error" not in d)
    assert client.get("/api/build/datasets?detail=1&from=5000&to=1000").status_code == 400


def test_a_files_switch_is_set_through_the_switch_route(client):
    from RevenueChaser.rcdata import ops
    r = client.post("/api/build/switch/file-options", json={"on": False}, headers={"X-RC-Panel": "grafana"})
    assert r.status_code == 200 and ops.switch_off("file-options") is not None
    assert client.post("/api/build/switch/file-nope", json={"on": False},
                       headers={"X-RC-Panel": "grafana"}).status_code == 400


def test_datasets_list_marks_attended():
    assert metrics.datasets_list(DS) == [
        {"name": "market_series", "kind": "market", "attended": False, "providers": ["fred"]},
        {"name": "wrds/prices", "kind": "prices", "attended": True, "providers": ["wrds"]}]


def test_a_backup_kind_that_exists_in_two_steps_gets_two_distinct_series(monkeypatch):
    """homesrv, 9 Nov 2026: ``prices`` is both a builder DuckDB kind (step duckdb) and a legacy board file (step
    boards), so rc_backup_newest_timestamp_seconds{kind="prices"} was written twice and Prometheus kept one at
    random. A repeated kind now carries its step in the label; a unique kind keeps its plain name."""
    from RevenueChaser.module_settings import newdata
    rows = [{"kind": "prices", "step": "duckdb", "newest": "2026-11-08T03:30:00+00:00"},
            {"kind": "prices", "step": "boards", "newest": "2026-11-01T03:30:00+00:00"},
            {"kind": "market", "step": "duckdb", "newest": "2026-11-08T03:30:00+00:00"},
            {"kind": "ops", "step": "ops", "newest": None}]
    monkeypatch.setattr(newdata, "_backups", lambda now: {"kinds": rows})
    fam = _families(metrics.render(_collect()))["rc_backup_newest_timestamp_seconds"]
    labels = sorted(re.search(r'\{kind="([^"]+)"\}', line).group(1) for line in fam)
    assert labels == ["market", "prices (boards)", "prices (duckdb)"]


def test_the_status_the_page_drew_as_cards_is_served_as_three_parts():
    """#715 decision 8 (avo, 12 Nov 2026: status lives on the Grafana board): provider limits, the cross-check and
    board freshness, each read on its own."""
    spend = {"day": "2026-11-11", "providers": [{"provider": "uw", "today": 7, "cap": None, "http_429_today": 1}]}
    seen = {}

    def cross(used):
        seen["used"] = used
        return {"fields": [{"field": "prices", "differs": 0}]}

    got = metrics.board_status(NOW, spend=lambda: spend, cross=cross, boards=lambda: {"boards": []})
    assert got["providers"] == spend and got["boards"] == {"boards": []}
    assert got["cross_check"]["fields"][0]["field"] == "prices"
    assert seen["used"] == {"uw": 7}, "the cross-check is told what each provider has used today"
    assert got["read_at"].startswith("2026-")


def test_a_status_part_that_cannot_be_read_says_so_and_the_others_still_answer():
    def boom(*_a):
        raise RuntimeError("history file is locked")

    got = metrics.board_status(NOW, spend=lambda: {"providers": []}, cross=boom, boards=boom)
    assert got["providers"] == {"providers": []}
    assert got["cross_check"] == {"error": "RuntimeError: history file is locked"}
    assert got["boards"] == {"error": "RuntimeError: history file is locked"}
    got = metrics.board_status(NOW, spend=boom, cross=lambda used: {"fields": [], "used": used},
                               boards=lambda: {"boards": []})
    assert "error" in got["providers"] and got["cross_check"]["used"] == {}


def test_the_status_route_answers_with_the_three_parts(client):
    ops.count_calls("uw", 4, http_429=1)
    got = client.get("/api/settings/status")
    assert got.status_code == 200
    body = got.get_json()
    assert body["ok"] is True and {"providers", "cross_check", "boards", "read_at"} <= set(body)
    uw = [p for p in body["providers"]["providers"] if p["provider"] == "uw"][0]
    assert uw["today"] == 4 and uw["http_429_today"] == 1


def test_the_requests_route_answers_the_newest_rows_with_this_process_own_first(client):
    """#715: the per-request log for the Grafana Logs board; a read made just now is there without waiting a minute."""
    ops.read_log.bump("market_series")
    ops.log_call("uw", "GET api.test/x", dataset="options/uw", result="HTTP 200", ms=12.4)
    body = client.get("/api/settings/requests?limit=5").get_json()
    assert body["ok"] is True and body["keep"] == ops.REQUEST_LOG_KEEP and body["on"] is True and body["dropped"] == 0
    mine = [(r["kind"], r["what"], r["result"], r["ms"]) for r in body["rows"]
            if r["what"] in ("uw GET api.test/x", "market_series")]
    assert mine == [("call", "uw GET api.test/x", "HTTP 200", 12), ("read", "market_series", None, None)]
    assert all(r["time"].endswith("+00:00") for r in body["rows"])
    times = [r["at"] for r in body["rows"]]
    assert times == sorted(times, reverse=True)
    assert len(client.get("/api/settings/requests?limit=1").get_json()["rows"]) == 1


def test_the_requests_route_refuses_words_for_numbers(client):
    got = client.get("/api/settings/requests?limit=abc")
    assert got.status_code == 400 and got.get_json()["ok"] is False
