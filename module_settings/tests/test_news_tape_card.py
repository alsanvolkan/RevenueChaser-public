"""The IBKR news feeds card on Settings / Data health (#727; avo, 12 Nov 2026: "Evet bir kart ekleyelim")."""
from __future__ import annotations

from datetime import datetime, timezone

from RevenueChaser.module_news import tape_state
from RevenueChaser.module_settings import inventory, report
from RevenueChaser.rcdata.tests import universe_store
from RevenueChaser.module_settings.tests.test_report import _payload

T0 = datetime(2026, 11, 11, 9, 36, 54, tzinfo=timezone.utc).timestamp()


def _state(tmp_path) -> dict:
    t = [T0]
    rec = tape_state.Recorder(path=tmp_path / "state.json", clock=lambda: t[0])
    rec.connected("127.0.0.1:4001")
    rec.asked("BRFG"), rec.asked("DJNL")
    t[0] = T0 + 100
    rec.headline("BRFG")
    t[0] = T0 + 174
    rec.refused("DJNL", 10197, "No market data during <competing> live session", t[0] + 1800)
    return tape_state.collect(names=["BRFG", "DJNL"], path=tmp_path / "state.json",
                              now=datetime.fromtimestamp(T0 + 240, timezone.utc))


def _card(html: str) -> str:
    start = html.index('<section id="news-tape">')
    return html[start:html.index("</section>", start)]


def test_without_the_key_there_is_no_card():
    assert 'id="news-tape"' not in report.render_settings_dashboard(_payload())


def test_each_feed_has_its_own_block_with_its_state(tmp_path):
    card = _card(report.render_settings_dashboard(dict(_payload(), news_tape_state=_state(tmp_path))))
    assert 'id="news-tape-feed-brfg" data-state="live"' in card
    assert 'id="news-tape-feed-djnl" data-state="refused"' in card
    assert '<span class="st-bad">Refused - IBKR error 10197' in card
    assert "Since 2026-11-11 09:39 UTC" in card and "Asked again at 10:09 UTC." in card
    assert "Connected to 127.0.0.1:4001" in card
    assert card.count("another session of this IBKR user") == 1, "the 10197 explanation is said once"


def test_the_gateway_s_words_are_escaped_and_the_card_says_it_opens_no_connection(tmp_path):
    card = _card(report.render_settings_dashboard(dict(_payload(), news_tape_state=_state(tmp_path))))
    assert "&lt;competing&gt;" in card and "<competing>" not in card
    assert "this page opens no connection" in card


def test_the_card_sits_on_the_health_tab_after_the_gateway_login_state(tmp_path):
    html = report.render_settings_dashboard(dict(_payload(), news_tape_state=_state(tmp_path),
                                                 ibkr_login_state={"state": "up", "text": "Logged in"}))
    assert 0 < html.index('id="ibkr-state"') < html.index('id="news-tape"')


def test_no_record_yet_is_said_in_words_not_drawn_as_a_refusal(tmp_path):
    state = tape_state.collect(names=["BRFG", "DJNL"], path=tmp_path / "none.json")
    card = _card(report.render_settings_dashboard(dict(_payload(), news_tape_state=state)))
    assert "No record yet" in card and "news-tape-feed-" not in card and "st-bad" not in card


def test_a_card_s_text_stays_light_on_its_dark_panel_under_the_light_theme():
    # Measured in Chromium, 12 Nov 2026: with data-theme="light" the heading of every .bcard on the Health tab was
    # rgb(29,36,48) on the panel's rgb(18,22,30). The panel is dark in every theme, so its ink is pinned.
    html = report.render_settings_dashboard(_payload())
    assert ':root[data-theme="light"] .bcard{color:var(--text)}' in html
    # auto AND a page whose data-theme is not set yet (it is set from JavaScript): needs.css falls back to light there
    assert '@media (prefers-color-scheme: light){:root:not([data-theme="dark"]) .bcard{color:var(--text)}}' in html


def test_the_card_opens_on_the_worst_feed_state_not_on_the_connected_gateway(tmp_path):
    # 11 Nov 2026: the gateway was up all day and the feeds were refused. The first line must be the red one.
    card = _card(report.render_settings_dashboard(dict(_payload(), news_tape_state=_state(tmp_path))))
    lead = 'id="news-tape-lead"><span class="st-bad">1 of 2 feeds refused: DJNL</span>'
    assert lead in card and card.index(lead) < card.index('id="news-tape-gateway"')


def test_a_stale_record_leads_with_a_warning_not_with_a_colour_it_can_no_longer_vouch_for(tmp_path):
    _state(tmp_path)
    state = tape_state.collect(names=["BRFG", "DJNL"], path=tmp_path / "state.json",
                               now=datetime.fromtimestamp(T0 + 3 * 3600, timezone.utc))
    card = _card(report.render_settings_dashboard(dict(_payload(), news_tape_state=state)))
    assert 'id="news-tape-lead"><span class="st-warn">Last known, ' in card
    assert "1 of 2 feeds refused: DJNL" in card


def test_provider_text_can_wrap_so_a_long_token_never_widens_a_phone_page():
    html = report.render_settings_dashboard(_payload())
    assert ".bcard .kv{color:var(--muted);font-size:12.5px;margin:2px 0;overflow-wrap:anywhere}" in html


# -- the payload: the line that carries the record into the page ------------------------------------------------

NOW = datetime.fromtimestamp(T0 + 240, timezone.utc)


def test_collect_carries_the_news_tape_card(rawstore_tmp_db, tmp_path, monkeypatch):
    _state(tmp_path)
    monkeypatch.setattr(tape_state, "state_path", lambda: tmp_path / "state.json")
    monkeypatch.setenv("RC_NEWS_TAPE_FEEDS", "BRFG,DJNL")
    universe_store.put([{"ticker": "AAPL", "market_cap": 3.0e9, "name": "Apple"}])
    card = inventory.collect(now=NOW)["news_tape_state"]
    assert card["state"] == "refused"
    assert [f["feed"] for f in card["feeds"]] == ["BRFG", "DJNL"]


def test_a_failing_tape_read_is_recorded_and_the_rest_of_the_payload_survives(rawstore_tmp_db, monkeypatch):
    def boom(**kw):
        raise RuntimeError("tape read exploded")

    monkeypatch.setattr(tape_state, "collect", boom)
    universe_store.put([{"ticker": "AAPL", "market_cap": 3.0e9, "name": "Apple"}])
    payload = inventory.collect(now=NOW)
    assert payload["news_tape_state"] is None
    assert {"table": "ibkr", "where": "_news_tape_state", "error": "tape read exploded"} in payload["errors"]
    assert {r["key"] for r in payload["rows"]}, "the rest of the inventory is still built"
    html = report.render_settings_dashboard(payload)
    assert "tape read exploded" in html and 'id="news-tape-card"' not in html
