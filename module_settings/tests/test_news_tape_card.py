"""The IBKR news feeds card on Settings / Data health (#727; avo, 12 Nov 2026: "Evet bir kart ekleyelim")."""
from __future__ import annotations

from datetime import datetime, timezone

from RevenueChaser.module_news import tape_state
from RevenueChaser.module_settings import report
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
    assert '@media (prefers-color-scheme: light){:root[data-theme="auto"] .bcard{color:var(--text)}}' in html
