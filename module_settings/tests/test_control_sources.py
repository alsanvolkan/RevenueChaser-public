"""Where each example column comes from (``s["pv"]``) and the selection funnel (``view["universe"]["funnel"]``),
issue #492 PR (i): every key from the code's own records, never a guess."""
from __future__ import annotations

import json
from types import SimpleNamespace

from RevenueChaser.module_settings import control, control_sources
from RevenueChaser.rcdata import scaffold
from RevenueChaser.rcdata.build.spine import my_universe


def _ds(kind, tables, providers):
    return SimpleNamespace(kind=kind, tables=tuple(tables), providers=tuple(providers))


def _sample(table, rows=()):
    cols = next(c for t in scaffold.SCHEMA_OF_KIND.values() for n, c in t.items() if n == table)
    return {"table": table, "columns": [{"name": n, "type": str(ty)} for n, ty in cols], "rows": list(rows)}


def _pv(sample, datasets=None):
    from RevenueChaser.rcdata.build import DATASETS
    return dict(zip([c["name"] for c in sample["columns"]],
                    control_sources.columns_by_source(sample, DATASETS if datasets is None else datasets)))


def test_the_labels_are_re_exported_and_name_every_key():
    assert control.PV_LABELS is control_sources.PV_LABELS
    assert control.PV_LABELS["sec"] == "SEC" and control.PV_LABELS["build"] == "the build"
    assert set(control.PV_LABELS) == {"build", "rule", "sec", "wrds", "figi", "alpaca", "uw", "yf", "finra", "ibkr",
                                      "exch", "av", "issuer", "macro", "mix"}


def test_a_source_word_becomes_its_key():
    key = control_sources.source_key
    assert key("sec/company_tickers_mf") == "sec" and key("edgar") == "sec"
    assert key("wrds/co_hgic") == "wrds" and key("compustat_tpci(EQ)") == "wrds" and key("crsp") == "wrds"
    assert key("openfigi/map (ticker)") == "figi" and key("yfinance/float") == "yf"
    assert key("uw") == "uw" and key("unusual whales") == "uw" and key("alphavantage") == "av"
    assert key("nasdaqtrader") == "exch" and key("ishares") == "issuer" and key("fred") == "macro"
    assert key("length_rule_v1") == "rule" and key("ibkr") == "ibkr" and key("finra") == "finra"
    assert key("tic") is None and key(None) is None and key("") is None


def test_the_fund_columns_follow_the_sec_file_the_rule_and_the_build():
    pv = _pv(_sample("fund", [["SPY", "SPY", "1", "S1", "C1", True, "length_rule_v1", None, None, None]]))
    assert [pv[c] for c in ("ticker", "ticker_raw", "cik", "series_id", "class_id")] == ["sec"] * 5
    assert pv["is_etf"] == pv["is_etf_source"] == "rule"
    assert pv["source_as_of"] == pv["fetched_at"] == pv["run_id"] == "build"


def test_one_key_per_column_in_column_order():
    sample = _sample("fund")
    assert len(control_sources.columns_by_source(sample, {})) == len(sample["columns"])


def test_the_security_columns_follow_their_source_siblings():
    s = _sample("security")
    names = [c["name"] for c in s["columns"]]
    row = dict.fromkeys(names)
    row.update(figi_source="openfigi/map", gics_source="wrds/co_hgic", tier="A")
    other = dict(row, figi_source="openfigi/map (ticker)")
    s["rows"] = [[row[n] for n in names], [other[n] for n in names]]
    pv = _pv(s)
    for c in ("figi", "composite_figi", "share_class_figi", "exch_code", "figi_ticker", "security_description",
              "figi_source"):
        assert pv[c] == "figi", c
    for c in ("gsector", "ggroup", "gind", "gsubind", "gics_from", "gics_source"):
        assert pv[c] == "wrds", c
    assert pv["tier"] == pv["tier_rule"] == "rule" and pv["source"] == "build"


def test_a_sibling_naming_two_sources_is_mix():
    s = _sample("security")
    names = [c["name"] for c in s["columns"]]
    rows = []
    for g in ("wrds/co_hgic", "sec/whatever"):
        r = dict.fromkeys(names)
        r["gics_source"] = g
        rows.append([r[n] for n in names])
    s["rows"] = rows
    assert _pv(s)["gsector"] == "mix"


def test_a_table_with_no_first_source_is_mix():
    datasets = {"a/one": _ds("prices", ["shares_class"], ["sec"]),
                "b/two": _ds("prices", ["shares_class"], ["yfinance"])}
    pv = _pv(_sample("shares_class"), datasets)
    assert pv["as_of"] == "mix" and pv["source"] == "build"


def test_the_first_source_wins_over_the_others(monkeypatch):
    from RevenueChaser.rcdata.needs import screen
    datasets = {"sec/cover_shares": _ds("prices", ["shares_class"], ["sec"]),
                "yfinance/float": _ds("prices", ["shares_class"], ["yfinance"])}
    assert screen.DATASETS["sec/cover_shares"].role == "first"
    assert _pv(_sample("shares_class"), datasets)["as_of"] == "sec"


def test_every_sample_of_the_board_gets_its_keys():
    from RevenueChaser.module_settings.tests.test_control_view import _build, control_nd
    nd = control_nd()
    nd["datasets"][0]["samples"] = [_sample("price_bar_daily")]
    view = _build(nd)
    rows = [r for f in view["files"] for r in f["datasets"] if r["dataset"] == "alpaca/bars"]
    s = rows[0]["samples"][0]
    assert len(s["pv"]) == len(s["columns"]) and "pv" not in nd["datasets"][0]["samples"][0]
    # #484: wrds/prices serves price_bar_daily; alpaca/bars only cross-checks it
    assert dict(zip([c["name"] for c in s["columns"]], s["pv"]))["session_date"] == "wrds"


def test_a_table_no_step_writes_is_mix_never_a_guess():
    s = {"table": "no_such_table", "columns": [{"name": "a"}, {"name": "fetched_at"}], "rows": [["x", "t"]]}
    assert control_sources.columns_by_source(s, {}) == ["mix", "build"]


# ── The selection funnel ──────────────────────────────────────────────────────────────────────────────────────

def _spine_nd():
    return {"identity": {"steps": [{"key": "sec", "count": 10430}, {"key": "listed", "count": 7670},
                                   {"key": "tier_a", "count": 5655}]},
            "my_universe": {"built": True, "total": 6100, "kinds": {"stock": 5200, "etf": 900},
                            "rules": {"stock": 5190, "etf_aum": 700, "etf_adv": 195, "manual": 15}},
            "tables": {"fund": {"rows": 28000}}}


def test_the_funnel_counts_come_from_the_spine_counts():
    f = control_sources.funnel(_spine_nd())
    assert [s["n"] for s in f["stocks"]] == [10430, 7670, 5190, 5200]
    assert [s["n"] for s in f["etfs"]] == [28000, None, 895, 900]
    assert f["added"] == 15
    assert all(set(s) == {"step", "n", "drops"} and isinstance(s["step"], str) and isinstance(s["drops"], str)
               for s in f["stocks"] + f["etfs"])


def test_without_a_spine_nothing_is_counted():
    f = control_sources.funnel({"identity": {"steps": []}, "my_universe": {"built": False}})
    assert all(s["n"] is None for s in f["stocks"] + f["etfs"]) and f["added"] is None


def test_the_rule_words_follow_the_json(tmp_path):
    doc = json.loads(my_universe.RULES_FILE.read_text(encoding="utf-8"))
    doc["stocks"]["tiers"] = ["A"]
    doc["stocks"]["types"] = ["common", "spac"]
    doc["etfs"]["aum_floor_usd"] = 250_000_000
    doc["etfs"]["adv_usd_20d_floor"] = 5_000_000
    doc["etfs"]["exclude_cef"] = False
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    f = control_sources.funnel(_spine_nd(), rules_path=path)
    assert "tier A;" in f["stocks"][2]["step"] and "common, spac" in f["stocks"][2]["step"]
    assert "$250M" in f["etfs"][2]["step"] and "$5M" in f["etfs"][2]["step"]
    assert "ETN" in f["etfs"][1]["step"] and "CEF" not in f["etfs"][1]["step"]
    default = control_sources.funnel(_spine_nd())
    assert "tier A or D" in default["stocks"][2]["step"] and "$100M" in default["etfs"][2]["step"]


def test_the_board_carries_the_funnel():
    from RevenueChaser.module_settings.tests.test_control_view import _build, control_nd
    nd = control_nd()
    nd.update(_spine_nd())
    view = _build(nd)
    assert view["universe"]["funnel"]["stocks"][0]["n"] == 10430
