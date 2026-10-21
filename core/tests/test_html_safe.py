"""The board HTML trust boundary (ADR-X1, #144): ``core.html_safe``.

Server data reaches a page only through ``esc`` (markup or attribute),
``script_json`` (inside ``<script>``) or a one-pass ``fill``; client data reaches
``innerHTML`` only through the shared JS ``rcEsc``.
"""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from RevenueChaser.core import html_safe

_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")


def _node_eval(js: str):
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True,
                         encoding="utf-8", check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


class TestEsc:
    def test_escapes_markup_and_both_quotes(self):
        assert html_safe.esc("<a href=\"x\" title='y'>&") == (
            "&lt;a href=&quot;x&quot; title=&#x27;y&#x27;&gt;&amp;")

    def test_none_is_empty_and_numbers_are_text(self):
        assert html_safe.esc(None) == ""
        assert html_safe.esc(0) == "0"
        assert html_safe.esc(1.5) == "1.5"


class TestScriptJson:
    def test_comment_and_script_openers_cannot_reach_the_tokenizer(self):
        s = html_safe.script_json({"name": "<!--<script></script>\u2028\u2029&"})
        assert "<" not in s and ">" not in s and "&" not in s
        assert "\u2028" not in s and "\u2029" not in s

    def test_round_trips_to_the_same_value(self):
        v = {"name": "<!--<script></script>\u2028\u2029& AT&T <1%", "n": 3, "ok": None}
        assert json.loads(html_safe.script_json(v)) == v

    def test_non_json_values_fall_back_to_str(self):
        from datetime import date
        assert html_safe.script_json({"d": date(2026, 1, 2)}) == '{"d": "2026-01-02"}'


class TestFill:
    def test_a_value_spelling_another_placeholder_is_substituted_once(self):
        out = html_safe.fill("var A = __A__; var B = __B__;",
                             {"__A__": '"__B__"', "__B__": "[1]"})
        assert out == 'var A = "__B__"; var B = [1];'

    def test_unknown_placeholders_are_left_alone(self):
        assert html_safe.fill("__A__ __Z__", {"__A__": "1"}) == "1 __Z__"

    def test_backslashes_in_values_are_literal(self):
        assert html_safe.fill("x=__A__", {"__A__": r'"<\n"'}) == r'x="<\n"'


@_node
def test_esc_js_matches_the_python_escaper_on_markup():
    out = _node_eval(html_safe.ESC_JS + """
console.log(JSON.stringify([rcEsc("<img src=x onerror='1'>\\"&"), rcEsc(null), rcEsc(0)]));
""")
    assert out == ["&lt;img src=x onerror=&#39;1&#39;&gt;&quot;&amp;", "", "0"]
