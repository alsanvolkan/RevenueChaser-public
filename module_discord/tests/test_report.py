"""#74 S2: the board's HTML. Payloads are built by hand; the data layer has its own tests."""
from __future__ import annotations

import re

from RevenueChaser.module_discord import report

CH = {"channel_id": "10", "guild_id": "1", "guild_name": "Example Server", "name": "swing-setups",
      "category": "Ideas", "enabled": True, "message_count": 6, "last_ts": "2026-10-01T11:00:00.000000Z"}


def _msg(i, **kw):
    m = {"message_id": str(1000 + i), "channel_id": "10", "channel_name": "swing-setups",
         "author_name": "Alpha", "is_bot": False, "ts": "2026-10-01T10:00:00.000000Z",
         "ts_label": "2026-10-01 10:00", "content": f"message {i}", "kind": "Default",
         "is_pinned": False, "reply_to_id": None, "reply_preview": None, "reference_kind": None,
         "attachments": [], "embeds": [], "reactions": []}
    m.update(kw)
    return m


def _payload(**kw):
    p = {"as_of": "2026-10-02T08:00:00Z", "last_sync": "2026-10-02T07:55:00Z",
         "channels": [CH], "filters": {"channel": "", "q": "", "date_from": "", "date_to": ""},
         "messages": [_msg(1)], "total": 1, "page": 1, "pages": 1, "page_size": 200}
    p.update(kw)
    return p


def test_topbar_has_title_subtitle_and_last_sync_and_no_regime_badge():
    html = report.render_discord_board(_payload())
    assert "<h1>Discord</h1>" in html
    assert "Last sync: Friday, October 02, 2026 07:55 UTC" in html
    assert "2026-10-02T07:55:00Z" not in html            # no raw ISO timestamp (AGENTS.md section 6)
    assert "regime" not in html.lower()


def test_last_sync_says_never_when_there_is_none():
    assert "never" in report.render_discord_board(_payload(last_sync=None)).lower()


def test_an_empty_archive_points_at_discover():
    html = report.render_discord_board(_payload(channels=[], messages=[], total=0))
    assert "Discover" in html and "no channels" in html.lower()


def test_channels_are_grouped_by_guild_and_category_with_counts():
    chans = [CH,
             {**CH, "channel_id": "11", "name": "general", "category": "Chat", "message_count": 2},
             {**CH, "channel_id": "12", "name": "other", "guild_id": "2", "guild_name": "Second Guild",
              "category": "", "message_count": 0, "enabled": False}]
    html = report.render_discord_board(_payload(channels=chans))
    i_g1, i_g2 = html.index("Example Server"), html.index("Second Guild")
    assert i_g1 < i_g2
    assert html.index('class="cat">Ideas') < html.index('class="cat">Chat')
    assert re.search(r'swing-setups.*?<span class="cnt">6</span>', html, re.S)
    assert re.search(r'general.*?<span class="cnt">2</span>', html, re.S)


def test_the_follow_checkbox_reflects_the_flag_and_names_the_channel():
    chans = [CH, {**CH, "channel_id": "11", "name": "off", "enabled": False}]
    html = report.render_discord_board(_payload(channels=chans))
    on = re.search(r'<input[^>]*class="follow"[^>]*data-channel="10"[^>]*>', html).group(0)
    off = re.search(r'<input[^>]*class="follow"[^>]*data-channel="11"[^>]*>', html).group(0)
    assert " checked" in on and " checked" not in off


def test_action_buttons_exist_with_the_routes_as_data_attributes():
    html = report.render_discord_board(_payload())
    assert 'data-run="/run/module_discord_discover"' in html
    assert 'data-run="/run/module_discord"' in html                  # Update all
    assert re.search(r'class="upd"[^>]*data-channel="10"', html)       # per-channel Update
    assert "/api/discord/follow" in html


def test_the_feed_shows_author_time_and_text():
    html = report.render_discord_board(_payload(messages=[_msg(1, content="Watching NVDA")]))
    assert "Alpha" in html and "2026-10-01 10:00" in html and "Watching NVDA" in html
    assert "swing-setups" in html


def test_replies_are_indented_with_a_parent_preview():
    m = _msg(2, kind="Reply", reply_to_id="1001",
             reply_preview={"author": "Beta", "text": "the original"})
    html = report.render_discord_board(_payload(messages=[m]))
    assert re.search(r'class="msg reply"', html)
    assert "the original" in html and "Beta" in html


def test_a_plain_message_is_not_marked_as_a_reply():
    assert 'class="msg reply"' not in report.render_discord_board(_payload())


def test_image_attachments_are_thumbnails_through_the_media_route():
    atts = [{"file_name": "chart.png", "size_bytes": 20480, "media_rel": "10/chart AB.png", "is_image": True},
            {"file_name": "notes.pdf", "size_bytes": 10, "media_rel": "10/notes.pdf", "is_image": False}]
    html = report.render_discord_board(_payload(messages=[_msg(1, attachments=atts)]))
    assert re.search(r'<img class="thumb" src="/discord/media/10/chart%20AB\.png"', html)
    assert 'src="/discord/media/10/notes.pdf"' not in html
    assert 'href="/discord/media/10/notes.pdf"' in html and "notes.pdf" in html


def test_an_attachment_outside_the_media_tree_shows_its_name_but_no_link():
    atts = [{"file_name": "gone.png", "size_bytes": 1, "media_rel": None, "is_image": True}]
    html = report.render_discord_board(_payload(messages=[_msg(1, attachments=atts)]))
    assert "gone.png" in html and "/discord/media/" not in html


def test_embeds_reactions_and_pins_are_shown():
    emb = [{"title": "Daily recap", "url": "https://news.example.invalid/r", "description": "SPX +0.4%",
            "color": "#3498DB", "author": "Recap Bot", "footer": "feed",
            "fields": [{"name": "SPX", "value": "+0.4%", "inline": True}],
            "images": [], "thumbnail": None}]
    m = _msg(1, embeds=emb, reactions=[{"emoji": "\U0001f44d", "count": 3}], is_pinned=True)
    html = report.render_discord_board(_payload(messages=[m]))
    assert "Daily recap" in html and "SPX +0.4%" in html and "+0.4%" in html
    assert "\U0001f44d" in html and (">3<" in html or "3</span>" in html)
    assert "pinned" in html.lower()


def test_embed_links_are_only_http_or_https():
    emb = [{"title": "x", "url": "javascript:alert(1)", "description": "d", "fields": [], "images": []}]
    html = report.render_discord_board(_payload(messages=[_msg(1, embeds=emb)]))
    assert "javascript:" not in html


def test_the_filter_form_keeps_the_current_filters():
    f = {"channel": "10", "q": 'say "hi"', "date_from": "2026-09-30", "date_to": "2026-10-01"}
    html = report.render_discord_board(_payload(filters=f))
    assert 'name="q"' in html and 'value="say &quot;hi&quot;"' in html
    assert 'name="date_from"' in html and 'value="2026-09-30"' in html
    assert re.search(r'<option value="10" selected', html)


def test_pagination_links_keep_the_filters():
    f = {"channel": "10", "q": "nvda", "date_from": "", "date_to": ""}
    html = report.render_discord_board(_payload(filters=f, page=2, pages=3, total=450))
    assert "page 2 of 3" in html.lower() and "450" in html
    prev = re.search(r'<a [^>]*rel="prev"[^>]*href="([^"]+)"', html) or re.search(r'href="([^"]+)"[^>]*rel="prev"', html)
    nxt = re.search(r'href="([^"]+)"[^>]*rel="next"', html)
    assert prev and "page=1" in prev.group(1) and "q=nvda" in prev.group(1) and "channel=10" in prev.group(1)
    assert nxt and "page=3" in nxt.group(1)
    assert "&amp;" in nxt.group(1)                        # the href itself is escaped


def test_no_pagination_for_a_single_page():
    assert 'rel="next"' not in report.render_discord_board(_payload())


def test_an_empty_filter_result_says_so():
    html = report.render_discord_board(_payload(messages=[], total=0,
                                                filters={"channel": "", "q": "zzz", "date_from": "", "date_to": ""}))
    assert "No messages match" in html


def test_message_text_is_escaped_and_keeps_line_breaks_as_text():
    m = _msg(1, content="<b>bold</b> & more\nsecond line")
    html = report.render_discord_board(_payload(messages=[m]))
    assert "<b>bold</b>" not in html and "&lt;b&gt;bold&lt;/b&gt; &amp; more" in html
    assert "white-space:pre-wrap" in html.replace(" ", "")


def test_the_page_has_one_balanced_set_of_script_tags():
    html = report.render_discord_board(_payload()).lower()
    assert html.count("<script") == html.count("</script")
