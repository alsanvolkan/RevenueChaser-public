""""Our universe" on Settings' "New data system" section: how many names we keep, what let each one in, the rules
the next build applies, avo's manual lines, and what entered or left on the last build day (avo, 4 Nov 2026,
decision 9: "data.settings boarda bu isin yapildigini da gostermemiz gerek").
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest

from RevenueChaser.core import calendar
from RevenueChaser.module_settings import newdata, report
from RevenueChaser.rcdata.build.spine import my_universe
from RevenueChaser.rcdata.tests.conftest import http_mock, market_roots  # noqa: F401 - market roots per test
from RevenueChaser.rcdata.tests.test_spine_security import built, run_all  # noqa: F401 - the 10 names

NOW = datetime(2026, 10, 28, 3, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(calendar, "_now", lambda: NOW)


def _text(card: dict) -> str:
    return re.sub(r"<[^>]+>", " ", report._nd_my_universe(card))


def test_without_a_spine_the_card_still_states_the_rules_the_next_build_applies():
    card = newdata.collect(NOW)["my_universe"]
    assert card["built"] is False and card["total"] is None
    assert "tier A or D" in card["rule_text"]["stocks"]
    assert "100M USD" in card["rule_text"]["etfs"] and "10M USD" in card["rule_text"]["etfs"]
    text = _text(card)
    assert "has not built the list yet" in text and "100M USD" in text


@pytest.mark.usefixtures("built")
def test_the_card_counts_the_list_the_spine_built_and_what_let_each_name_in():
    card = newdata.collect(NOW)["my_universe"]
    assert card["built"] is True
    assert card["kinds"]["stock"] == card["total"] and card["total"] > 0
    assert card["rules"]["stock"] == card["total"]              # this fixture's ETFs are all under the AUM floor
    assert card["as_of"] is not None and card["last_change"] is not None
    assert card["entered"] == card["total"] and card["left"] == 0


@pytest.mark.usefixtures("built")
def test_the_rendered_card_names_the_counts_the_rules_and_the_manual_lines():
    card = newdata.collect(NOW)["my_universe"]
    text = _text(card)
    assert "Our universe" in text and "Stocks" in text and "ETFs" in text
    assert "Entered" in text and "Left" in text
    assert "Added or removed by hand ( 0 )".replace(" ", "") in text.replace(" ", "")
    assert "None." in text


def test_the_manual_lines_reach_the_card_and_are_escaped(monkeypatch):
    monkeypatch.setattr(my_universe, "load_manual",
                        lambda *a, **k: ([{"ticker": "MSGS", "reason": "<script>x</script>"}],
                                         [{"ticker": "DMLP", "reason": "a royalty trust"}]))
    card = newdata.collect(NOW)["my_universe"]
    assert card["manual"] == {"add": [{"ticker": "MSGS", "reason": "<script>x</script>"}],
                              "remove": [{"ticker": "DMLP", "reason": "a royalty trust"}]}
    html = report._nd_my_universe(card)
    assert "<script>" not in html and "&lt;script&gt;" in html
