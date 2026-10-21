"""#198: names the gates exclude are no longer scored, so the board lists them (and why) in the
filter summary instead of as full cards."""
from RevenueChaser.module_earnings_radar.report import export_html


def _stats(excluded):
    return {"total": 30, "passed": 20, "bypassed": 0, "filtered": len(excluded),
            "by_reason": {"OI": 2, "cap": 1}, "excluded": excluded}


def test_excluded_names_and_reasons_are_listed(tmp_path):
    p = export_html([], [], {}, filename=str(tmp_path / "e.html"),
                    filter_stats=_stats([{"ticker": "AAA", "reasons": ["OI", "$Vol"]},
                                         {"ticker": "BBB", "reasons": ["cap"]}]))
    html = p.read_text(encoding="utf-8")
    assert "Not scored" in html
    assert "AAA" in html and "OI, $Vol" in html and "BBB" in html


def test_the_list_is_capped_and_says_how_many_more(tmp_path):
    ex = [{"ticker": f"T{i:03d}", "reasons": ["OI"]} for i in range(120)]
    html = export_html([], [], {}, filename=str(tmp_path / "e.html"), filter_stats=_stats(ex)).read_text(encoding="utf-8")
    assert "T049" in html and "T050" not in html
    assert "+70 more" in html


def test_provider_strings_are_escaped(tmp_path):
    ex = [{"ticker": '"><script>alert(1)</script>', "reasons": ["<b>OI</b>"]}]
    html = export_html([], [], {}, filename=str(tmp_path / "e.html"), filter_stats=_stats(ex)).read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_no_excluded_means_no_line(tmp_path):
    html = export_html([], [], {}, filename=str(tmp_path / "e.html"), filter_stats=_stats([])).read_text(encoding="utf-8")
    assert "Not scored" not in html
