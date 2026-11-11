"""Grok "X saatlik özet" mail rows -> News rows (avo, 10 Nov 2026 22:27-22:28 Amsterdam). The bodies below are the
three shapes seen in real mails (4-10 Nov), trimmed: rows run together, rows on their own lines, Markdown-wrapped.
"""
from __future__ import annotations

import json

import pytest

from RevenueChaser.core import config, rawstore
from RevenueChaser.module_news import headlines, report, x_feed

NOW = 1794345669.0

RUN_TOGETHER = (
    "22:12 Amsterdam Pencere: 21:12–22:12 Amsterdam [RC-TW] SAATLİK | NRGV CRWV SPY KAYITLAR "
    "2026-11-10 22:11 | @zohmbastic | 2120263144924692750 | NN | news | - | $NN NextNav Q3 EPS $(0.04) Beats "
    "2026-11-10 21:24 | @kiantrades | 2120251362529419593 | SPY | option | SPY 759C 11/20exp $1.8M premium yön belirsiz"
    " | $SPY 759C 11/20exp $1.8M premium #flowseidon ÖZET\n\n* NRGV backlog https://x.com/zohmbastic/ ...\n\n"
    "| Continue reading[](https://grok.com/chat/x) |"
)

OWN_LINES = (
    "[RC-TW] SAATLİK | RKT W TOST\n\nBÖLÜM 1, KAYITLAR:\n\n"
    "2026-11-04 17:26 | @zohmbastic | 2118017069270884385 | RKT | level | - | $RKT 15 soon\n\n"
    "2026-11-04 17:38 | @zohmbastic | 2118019958378479720 | - | macro | - | crude big pull\n\n"
    "2026-11-04 17:58 | @zohmbastic | ...\n\n| Continue reading[](https://grok.com/chat/y) |"
)

MARKDOWN = (
    "*23:13 Amsterdam*\n*22:13–23:13 Amsterdam*\n\n[RC-TW] SAATLİK | ASTS BW ABCL\n\n*KAYITLAR*\n"
    "2026-11-09 22:31 | @zohmbastic | 2119905646090858554 | ASTS | news | - | $ASTS Third quarter revenue of "
    "$42.3 million driven by gateway deliveries ...\n\n| Continue reading[](https://grok.com/chat/z) |"
)


def _ids(rows):
    return [r["headline_id"] for r in rows]


def test_status_time_is_the_ids_own_creation_time():
    # 2120263144924692750 was posted 2026-11-10 22:11:40 Amsterdam (21:11:40 UTC).
    assert int(x_feed.status_time(2120263144924692750)) == 1794345100


def test_rows_that_run_together_split_on_the_row_pattern():
    rows, refused = x_feed.parse(RUN_TOGETHER, NOW, "r1")
    assert refused == []
    assert _ids(rows) == ["x:2120263144924692750", "x:2120251362529419593"]
    news, flow = rows
    assert news["headline"] == "$NN NextNav Q3 EPS $(0.04) Beats"
    assert news["tickers"] == ["NN"] and news["kind"] == "news" and news["option"] == ""
    assert flow["option"] == "SPY 759C 11/20exp $1.8M premium yön belirsiz"
    assert flow["headline"] == "$SPY 759C 11/20exp $1.8M premium #flowseidon"
    assert flow["url"] == "https://x.com/kiantrades/status/2120251362529419593"
    assert flow["source"] == "@kiantrades" and flow["provider"] == "grok-x"
    assert x_feed.published_local(flow) == "2026-11-10 21:24"


def test_rows_on_own_lines_and_a_cut_last_row_is_dropped():
    rows, refused = x_feed.parse(OWN_LINES, NOW, "r1")
    assert _ids(rows) == ["x:2118017069270884385", "x:2118019958378479720"]
    assert rows[0]["headline"] == "$RKT 15 soon"
    assert rows[1]["tickers"] == [] and rows[1]["kind"] == "macro"
    assert refused == []


def test_markdown_wrapped_row_keeps_its_text_until_the_footer():
    rows, _ = x_feed.parse(MARKDOWN, NOW, "r1")
    assert len(rows) == 1
    assert rows[0]["headline"].startswith("$ASTS Third quarter revenue")
    assert "Continue" not in rows[0]["headline"]


def test_an_id_that_does_not_match_its_time_is_refused():
    body = "2026-11-10 09:00 | @zohmbastic | 2120263144924692750 | NN | news | - | made up"
    rows, refused = x_feed.parse(body, NOW, "r1")
    assert rows == []
    assert refused[0]["status_id"] == "2120263144924692750"
    assert "is not 2026-11-10 09:00" in refused[0]["reason"]


def test_unknown_kind_is_refused_and_a_repeated_tweet_is_kept_once():
    body = (
        "2026-11-04 17:26 | @zohmbastic | 2118017069270884385 | RKT | rumor | - | $RKT 15 soon "
        "2026-11-04 17:38 | @zohmbastic | 2118019958378479720 | - | macro | - | crude big pull "
        "2026-11-04 17:38 | @zohmbastic | 2118019958378479720 | - | macro | - | crude big pull"
    )
    rows, refused = x_feed.parse(body, NOW, "r1")
    assert _ids(rows) == ["x:2118019958378479720"]
    assert refused == [{"status_id": "2118017069270884385", "reason": "unknown kind 'rumor'"}]


def test_empty_or_no_rows_mail_gives_nothing():
    assert x_feed.parse("X saatlik özet is ready No tweets in window Maddi tweet yok.", NOW, "r1") == ([], [])
    assert x_feed.parse("", NOW, "r1") == ([], [])


NEW_PROMPT = (
    "[RC-TW] SAATLİK | NN SPY\nPENCERE 21:12-22:12 Amsterdam\nKAYIT n=3\n"
    "2026-11-10 22:11 | @zohmbastic | 2120263144924692750 | NN | news | - | $NN NextNav Q3 EPS $(0.04) Beats\n"
    "2026-11-10 21:24 | @kiantrades | 2120251362529419593 | SPY | option | SPY 759C 11/20exp $1.8M yön belirsiz | $SPY 759C\n"
    "SON\n"
)


def test_the_compact_prompt_format_parses_and_declares_its_count():
    rows, refused = x_feed.parse(NEW_PROMPT, NOW, "r1")
    assert refused == [] and len(rows) == 2
    assert rows[1]["headline"] == "$SPY 759C"
    # Three declared, two arrived: the caller sees the mail was cut.
    assert x_feed.declared(NEW_PROMPT) == 3
    assert x_feed.declared(RUN_TOGETHER) is None


def _mail(plain=None, html_body=None):
    from email.message import EmailMessage
    m = EmailMessage()
    m["From"], m["Subject"] = "Grok <noreply@x.ai>", "x"
    if plain is not None:
        m.set_content(plain)
    if html_body is not None:
        (m.add_alternative if plain is not None else m.set_content)(html_body, subtype="html")
    return m.as_bytes()


def test_mail_text_prefers_plain_and_falls_back_to_html_without_tags():
    assert "KAYIT n=3" in x_feed.mail_text(_mail(plain=NEW_PROMPT, html_body="<p>other</p>"))
    assert x_feed.mail_text(_mail(html_body="<p>KAYIT n=0</p><p>SON &amp; end</p>")).split() == [
        "KAYIT", "n=0", "SON", "&", "end"]


class FakeImap:
    def __init__(self, mails):
        self.mails, self.calls = mails, []

    def select(self, box, readonly=False):
        self.calls.append(("select", box, readonly))
        return "OK", [b"3"]

    def uid(self, cmd, *args):
        self.calls.append((cmd, *args))
        if cmd == "SEARCH":
            return "OK", [b" ".join(str(u).encode() for u in self.mails)]
        u = int(args[0])
        return "OK", [(f"{u} (BODY[] {{9}}".encode(), self.mails[u]), b")"]


def test_read_mails_is_read_only_and_starts_after_the_mark():
    imap = FakeImap({7: b"seven", 9: b"nine", 12: b"twelve"})
    assert x_feed.read_mails(imap, x_feed.LABEL, 7) == [(9, b"nine"), (12, b"twelve")]
    assert imap.calls[0] == ("select", '"00_invest/Grok"', True)
    fetches = [c for c in imap.calls if c[0] == "FETCH"]
    assert fetches and all(c[2] == "(BODY.PEEK[])" for c in fetches)
    assert not any(c[0] in ("STORE", "COPY", "MOVE", "EXPUNGE") for c in imap.calls)


# ── the hourly fetcher (system python, no DuckDB) ───────────────────────────────────────────────────────────


class FakeSession(FakeImap):
    """``imaplib.IMAP4_SSL`` as the fetcher uses it: a context manager that logs in."""
    def __init__(self, mails):
        super().__init__(mails)
        self.closed = False

    def __call__(self, host, timeout=None):
        self.calls.append(("connect", host))
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True

    def login(self, user, password):
        self.calls.append(("login", user))


class FakeResponse:
    status = 202

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _keys(monkeypatch):
    monkeypatch.setenv("GMAIL_IMAP_USER", "user@example.invalid")
    monkeypatch.setenv("GMAIL_IMAP_APP_PASSWORD", "app-password-value")


def test_the_fetcher_saves_new_mails_moves_its_mark_and_queues_the_board_job(tmp_path, monkeypatch, capsys):
    _keys(monkeypatch)
    imap, posts = FakeSession({5: _mail(plain=NEW_PROMPT), 6: _mail(plain="GÜNLÜK")}), []

    def urlopen(req, timeout=None):
        posts.append((req.full_url, req.get_method(), req.get_header("X-rc-panel")))
        return FakeResponse()

    assert x_feed.run(imap_factory=imap, folder=tmp_path, now=NOW, urlopen=urlopen) == 0
    lines = [json.loads(x) for x in (tmp_path / "2026-11-10.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [x["uid"] for x in lines] == [5, 6] and "KAYIT n=3" in lines[0]["text"] and lines[0]["subject"] == "x"
    assert x_feed.uid_mark(tmp_path) == 6 and imap.closed
    assert posts == [("http://127.0.0.1:8000/run/news_headlines", "POST", "cli")]
    out = capsys.readouterr()
    assert "2 new mails" in out.out and "queued" in out.out and "app-password-value" not in out.out + out.err
    # The next pass asks only after the mark, and queues nothing when nothing is new.
    imap.mails = {}
    assert x_feed.run(imap_factory=imap, folder=tmp_path, now=NOW + 3600, urlopen=urlopen) == 0
    assert ("SEARCH", None, "UID 7:*", "FROM", '"noreply@x.ai"') in imap.calls and len(posts) == 1


def test_the_fetcher_without_its_keys_stops_before_connecting(tmp_path, monkeypatch):
    monkeypatch.delenv("GMAIL_IMAP_USER", raising=False)
    monkeypatch.delenv("GMAIL_IMAP_APP_PASSWORD", raising=False)
    imap = FakeSession({})
    assert x_feed.run(imap_factory=imap, folder=tmp_path, now=NOW) == 2
    assert imap.calls == [] and not list(tmp_path.iterdir())


# ── the dashboard's loader and the board's X lane ────────────────────────────────────────────────────────────


def _saved(*bodies, now=NOW):
    folder = x_feed.x_dir(config.MARKET_RAW_DIR)
    x_feed.save([(i + 1, _mail(plain=b)) for i, b in enumerate(bodies)], folder, now)


def _heads():
    return {r["headline_id"]: r for r in rawstore.query("SELECT * FROM news_headlines", db="news")}


def test_load_x_stores_each_tweet_once_and_counts_a_cut_mail():
    _saved(NEW_PROMPT, RUN_TOGETHER)
    got = headlines.load_x(now=NOW, run_id="r1")
    assert got == {"mails": 2, "stored": 2, "refused": 0, "cut": 1}     # NEW_PROMPT declared 3 and carried 2
    row = _heads()["x:2120251362529419593"]
    assert row["provider"] == "grok-x" and row["kind"] == "option" and json.loads(row["tickers"]) == ["SPY"]
    assert row["option"].startswith("SPY 759C") and row["published_at"] == pytest.approx(
        x_feed.status_time(2120251362529419593))
    assert headlines.load_x(now=NOW + 60)["mails"] == 0                  # the mark moved
    _saved(OWN_LINES, now=NOW + 3600)
    assert headlines.load_x(now=NOW + 3600)["stored"] == 2


def test_the_board_shows_tweets_in_their_own_x_lane_with_kind_and_option(monkeypatch):
    _saved(NEW_PROMPT)
    headlines.load_x(now=NOW, run_id="r1")
    page = report.render_html(now=NOW + 60)
    x_lane = page.index('id="heads-x"')
    assert "X · son 24 saat" in page and x_lane < page.index("$NN NextNav")
    assert page.index('id="heads-market"') < page.index('id="heads-stocks"') < x_lane
    assert '<span class="xk">opsiyon</span>' in page and "SPY 759C 11/20exp $1.8M yön belirsiz" in page
    assert 'href="https://x.com/zohmbastic/status/2120263144924692750"' in page
    monkeypatch.setattr(report.time, "time", lambda: NOW + 60)          # api_rows' 24 h window is the clock's
    polled = {r["headline_id"]: r for r in report.api_rows(0)}
    flow = polled["x:2120251362529419593"]
    assert flow["lane"] == "x" and flow["kind"] == "opsiyon" and flow["url"].startswith("https://x.com/")
