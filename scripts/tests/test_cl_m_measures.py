"""Classification CL-M measurement scripts (#387, #388, #379): synthetic stores only.

Every script reads a backup snapshot read-only and offline; the file names start with a digit, so the modules are
loaded by path. The stores here are tiny DuckDB files built from the MANIFEST column lists; nothing touches a real
store or the network.
"""
from __future__ import annotations

import gzip
import hashlib
import importlib.util
import json
import socket
import zlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pytest

from RevenueChaser.core import rawstore

RESEARCH = Path(__file__).resolve().parents[1] / "research"


def _load(name: str):
    spec = importlib.util.spec_from_file_location("clm_" + name, RESEARCH / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def cm():
    return _load("_cl_m_common")


def _store(path: Path, tables: dict[str, list[dict]]) -> Path:
    con = duckdb.connect(str(path))
    for name, rows in tables.items():
        cols = rawstore.MANIFEST[name]["columns"]
        con.execute(f"CREATE TABLE {name} ({', '.join(cols)})")
        for r in rows:
            keys = list(r)
            con.execute(f"INSERT INTO {name} ({', '.join(keys)}) VALUES ({', '.join('?' * len(keys))})",
                        [json.dumps(r[k]) if isinstance(r[k], (list, dict)) else r[k] for k in keys])
    con.close()
    return path


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _universe_rows(tickers, cap=1e9):
    return [{"ticker": t, "market_cap": cap, "name": t} for t in tickers]


# ── the guards ───────────────────────────────────────────────────────────────
def test_live_store_paths_are_refused(cm, tmp_path):
    live = tmp_path / ".store"
    live.mkdir()
    for p in (live / "raw_20261014_000000_000000.duckdb", tmp_path / "raw.duckdb", tmp_path / "edgar.duckdb"):
        assert cm.refusal(p), p
    assert cm.refusal(tmp_path / "raw_20261014_000000_000000.duckdb") is None


def test_script_exits_2_on_a_live_store_and_leaves_it_unchanged(tmp_path, capsys):
    mod = _load("388_pending_split")
    live = _store(tmp_path / "raw.duckdb", {"relatedness_profiles": []})
    before = _digest(live)
    assert mod.cm.run_main(mod.main, ["--raw", str(live), "--out", str(tmp_path / "out")]) == 2
    assert "refused" in capsys.readouterr().err
    assert _digest(live) == before
    assert not (tmp_path / "out").exists()


def test_backups_dir_picks_the_newest_snapshot(cm, tmp_path):
    for stamp in ("20261001_040000_000000", "20261014_040000_000000"):
        _store(tmp_path / f"raw_{stamp}.duckdb", {"relatedness_profiles": []})

    class A:
        backups, raw = tmp_path, None

    assert cm.resolve_store(A, "raw").name == "raw_20261014_040000_000000.duckdb"
    with pytest.raises(cm.Refused):
        cm.resolve_store(A, "edgar")


def test_universe_drops_excluded_types_and_the_floor(cm, tmp_path):
    raw = _store(tmp_path / "raw_1.duckdb", {
        "equity_universe": _universe_rows(["OPCO", "FUND", "UNTYPED"]) + [{"ticker": "SMALL", "market_cap": 1e6, "name": "S"}],
        "security_master": [{"ticker": "OPCO", "security_type": "common"}, {"ticker": "FUND", "security_type": "etp"}]})
    con = duckdb.connect(str(raw), read_only=True)
    assert set(cm.classification_universe(con)) == {"OPCO", "UNTYPED"}
    assert cm.excluded_tickers(con) == {"FUND"}


def test_a_network_call_is_refused(cm):
    with cm.offline_guard():
        with pytest.raises(cm.OfflineError):
            socket.create_connection(("example.com", 80))


# ── 388_percentile_shift ─────────────────────────────────────────────────────
def _attr(t, products):
    return {"ticker": t, "field": "products", "value": products}


def test_percentile_shift_counts_zero_pairs(tmp_path):
    mod = _load("388_percentile_shift")
    raw = _store(tmp_path / "raw_20261014_040000_000000.duckdb", {
        "equity_universe": _universe_rows("ABCD"),
        "relatedness_profiles": [{"ticker": t, "status": "ok"} for t in "ABCD"],
        "relatedness_attributes": [_attr("A", ["x", "y"]), _attr("B", ["x", "y"]), _attr("C", ["x"]), _attr("D", ["z"])],
    })
    before = _digest(raw)
    res = mod.cm.run_main(mod.main, ["--raw", str(raw), "--out", str(tmp_path / "out")])
    assert res == 0
    out = json.loads((tmp_path / "out" / "percentile_shift.json").read_text())
    assert out["profiles_used"] == 4
    assert out["nonzero_pairs"] == 3 and out["zero_pairs"] == 3 and out["new_population"] == 6
    assert out["old_population"] == 2 * 3 + 4                      # each pair twice + the diagonal
    assert out["zero_pair_share"] == 0.5
    assert out["shown_peers"] > 0 and out["median_abs_dp"] is not None
    assert (tmp_path / "out" / "percentile_shift.txt").read_text().startswith("# 388 percentile shift")
    assert "classification universe (4 names)" in out["scope"]
    assert "scope: profiles of the classification universe" in (tmp_path / "out" / "percentile_shift.txt").read_text()
    assert _digest(raw) == before


def test_percentile_new_matches_the_formula(tmp_path):
    mod = _load("388_percentile_shift")
    profiles = {"A": {"products": ["x", "y"]}, "B": {"products": ["x", "y"]}, "C": {"products": ["x"]},
                "D": {"products": ["z"]}}
    res = mod.measure(profiles)
    ac = [m for m in res["biggest_movers"] if {m["ticker"], m["peer"]} == {"A", "C"}]
    # score 0.7071: the 3 zero pairs are lower, no non-zero pair is (the engine ranks the 4-decimal score): 3 / 6 pairs
    assert ac and ac[0]["new_p"] == 50.0


# ── 388_pending_split ────────────────────────────────────────────────────────
def test_pending_split_separates_no_text(tmp_path):
    mod = _load("388_pending_split")
    raw = _store(tmp_path / "raw_20261014_040000_000000.duckdb", {
        "equity_universe": _universe_rows(["U1", "U2", "U3", "U4"]) + [{"ticker": "LOW", "market_cap": 1e6, "name": "LOW"}],
        "relatedness_profiles": [{"ticker": "U1", "status": "ok"}, {"ticker": "U2", "status": "ok"},
                                 {"ticker": "U3", "status": "no_text"}, {"ticker": "LOW", "status": "ok"}],
        "relatedness_attributes": [_attr("U1", ["a"]), _attr("U2", ["b"]), _attr("LOW", ["c"])],
    })
    assert mod.cm.run_main(mod.main, ["--raw", str(raw), "--out", str(tmp_path / "o")]) == 0
    r = json.loads((tmp_path / "o" / "pending_split.json").read_text())
    assert r["universe"] == 4 and r["usable_in_universe"] == 2 and r["no_text_in_universe"] == 1
    assert r["pending_today"] == 2 and r["pending_d4"] == 1 and r["pending_delta"] == 1
    assert r["status_in_universe"] == {"ok": 2, "no_text": 1}
    assert r["no_profile_row_in_universe"] == 1


# ── 388_offpeak_overlap ──────────────────────────────────────────────────────
def _log(path: Path, runs):
    lines = []
    for start, end, rc in runs:
        lines.append(f"=== {start.isoformat(timespec='seconds')} start {path.stem}")
        lines.append("some output")
        if end:
            lines.append(f"=== {end.isoformat(timespec='seconds')} exit {rc}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_offpeak_overlap_finds_and_clears(tmp_path):
    mod = _load("388_offpeak_overlap")
    t = datetime(2026, 10, 13, 10, 30, tzinfo=timezone.utc)
    logs = tmp_path / "logs"
    logs.mkdir()
    _log(logs / "RC-Relatedness-OffPeak.log", [(t, t + timedelta(minutes=45), 0)])
    _log(logs / "RC-ValueChain-Sync.log", [(t + timedelta(minutes=30), t + timedelta(minutes=50), 0)])
    r = mod.measure(logs)
    assert r["overlap_count"] == 1 and r["overlaps"][0]["overlap_minutes"] == 15.0
    assert r["verdict"].startswith("overlap")
    _log(logs / "RC-ValueChain-Sync.log", [(t + timedelta(minutes=60), t + timedelta(minutes=80), 0)])
    r = mod.measure(logs)
    assert r["overlap_count"] == 0 and r["verdict"].startswith("no overlap")
    assert r["max_minutes"]["RC-Relatedness-OffPeak"] == 45.0


def test_offpeak_open_run_ends_at_next_start_and_stays_out_of_the_verdict(tmp_path):
    mod = _load("388_offpeak_overlap")
    logs = tmp_path / "logs"
    logs.mkdir()
    assert mod.measure(logs)["verdict"].startswith("no data")
    t = datetime(2026, 10, 13, 10, 30, tzinfo=timezone.utc)
    _log(logs / "RC-Relatedness-OffPeak.log", [(t, None, None), (t + timedelta(days=1), t + timedelta(days=1, minutes=10), 0)])
    _log(logs / "RC-ValueChain-Sync.log", [(t + timedelta(minutes=30), t + timedelta(minutes=50), 0)])
    r = mod.measure(logs, now=t + timedelta(days=3))
    assert r["open_runs"] == {"RC-Relatedness-OffPeak": 1, "RC-ValueChain-Sync": 0}
    assert r["overlap_count"] == 0 and r["open_run_overlaps"] == 1             # listed, not decisive
    assert r["verdict"].startswith("no overlap")
    first = r["runs"]["RC-Relatedness-OffPeak"][0]
    assert first["open"] is True and first["end"] == (t + timedelta(days=1)).isoformat()


def test_offpeak_reads_the_rotated_log(tmp_path):
    mod = _load("388_offpeak_overlap")
    logs = tmp_path / "logs"
    logs.mkdir()
    t = datetime(2026, 10, 12, 10, 30, tzinfo=timezone.utc)
    (logs / "RC-Relatedness-OffPeak.log.1").write_text(
        f"=== {t.isoformat(timespec='seconds')} start x\n=== {(t + timedelta(minutes=40)).isoformat(timespec='seconds')} exit 0\n")
    _log(logs / "RC-Relatedness-OffPeak.log", [(t + timedelta(days=1), t + timedelta(days=1, minutes=5), 0)])
    _log(logs / "RC-ValueChain-Sync.log", [(t + timedelta(minutes=30), t + timedelta(minutes=50), 0)])
    r = mod.measure(logs)
    assert r["run_counts"]["RC-Relatedness-OffPeak"] == 2
    assert r["overlap_count"] == 1 and r["overlaps"][0]["overlap_minutes"] == 10.0


# ── 387_share_edges / 387_mentions_by_relation ───────────────────────────────
def _filing(t, text, filed="2026-02-01", quality="ok"):
    return {"ticker": t, "cik": "1", "form": "10-K", "filed": filed, "quality": quality,
            "item_text": zlib.compress(text.encode())}


def test_share_edges_counts_parsed_shares_and_lists_the_ticker(tmp_path):
    mod = _load("387_share_edges")
    text = "Apple Inc. accounted for approximately 29.8% of our net sales. We also mention Nvidia in passing."
    edgar = _store(tmp_path / "edgar_20261014_040000_000000.duckdb", {
        "chain_filings": [_filing("ZZZ", text), _filing("AAPL", "Direct sales to Qualcomm and Intel accounted for "
                                                        "11.1% and 5% of our revenues, respectively."),
                          _filing("QQQ", "Nothing about concentration here."),
                          _filing("MIS", "Apple Inc. accounted for 50% of our net sales.", quality="missing")]})
    before = _digest(edgar)
    assert mod.cm.run_main(mod.main, ["--edgar", str(edgar), "--out", str(tmp_path / "o")]) == 0
    r = json.loads((tmp_path / "o" / "share_edges.json").read_text())
    assert r["filings"] == 3 and r["filings_with_edges"] == 2 and r["filings_with_share"] == 2
    assert r["edges"] == 3 and r["edges_with_share"] == 3
    assert r["by_relation"]["customer"] == {"edges": 3, "with_share": 3}
    assert {e["dst_ticker"]: e["share_pct"] for e in r["ticker_edges"]} == {"QCOM": 11.1, "INTC": 5.0}
    # AAPL is also named by ZZZ's filing (incoming); the quality == "missing" filing is skipped like build_edges does
    assert [(e["src"], e["share_pct"]) for e in r["ticker_incoming_edges"]] == [("ZZZ", 29.8)]
    assert _digest(edgar) == before


def test_share_edges_drops_the_issuers_own_name(tmp_path):
    mod = _load("387_share_edges")
    edgar = _store(tmp_path / "edgar_1.duckdb", {"chain_filings": [
        _filing("AAPL", "Apple Inc. accounted for approximately 10% of our net sales.")]})
    assert mod.cm.run_main(mod.main, ["--edgar", str(edgar), "--out", str(tmp_path / "o")]) == 0
    assert json.loads((tmp_path / "o" / "share_edges.json").read_text())["edges"] == 0


def test_mentions_by_relation_misses_and_pending(tmp_path):
    mod = _load("387_mentions_by_relation")
    edgar = _store(tmp_path / "edgar_20261014_040000_000000.duckdb", {
        "chain_mentions": [{"src": "F1", "dst_name": "x", "dst_ticker": "X", "relation": "supplier", "snippet": "", "filed": ""},
                           {"src": "F1", "dst_name": "y", "dst_ticker": "Y", "relation": "customer", "snippet": "", "filed": ""},
                           {"src": "OUT", "dst_name": "y", "dst_ticker": "Y", "relation": "customer", "snippet": "", "filed": ""}],
        # LOW is a synced filing below the $300M floor; ETF is an excluded type: both are rows the engine counts or drops
        "chain_filings": [_filing("F1", "text"), _filing("OUT", "text"), _filing("LOW", "text"), _filing("ETF", "text")],
        "chain_meta": [{"key": "miss:M40", "value": json.dumps({"reason": "form_40f"})},
                       {"key": "miss:MNF", "value": json.dumps({"reason": "no_filing"})},
                       {"key": "miss:OLD40", "value": json.dumps({"reason": "form_40f"})},     # a miss outside the unfiled set
                       {"key": "other", "value": "{}"}]})
    raw = _store(tmp_path / "raw_20261014_040000_000000.duckdb", {
        "equity_universe": _universe_rows(["F1", "M40", "MNF", "NOCIK", "PEND", "ETF"])
        + [{"ticker": "LOW", "market_cap": 1e6, "name": "LOW"}],
        "security_master": [{"ticker": "ETF", "security_type": "etp"}],
        "sec_company_tickers": [{"ticker": t, "cik": "1", "name": t} for t in ("F1", "M40", "MNF", "PEND", "ZZZ")]})
    payload = tmp_path / "value_chain_latest.json"
    payload.write_text(json.dumps({"pending": 7, "fetch": {"pending": 99}}))
    assert mod.cm.run_main(mod.main, ["--edgar", str(edgar), "--raw", str(raw), "--vc-payload", str(payload),
                                      "--out", str(tmp_path / "o")]) == 0
    r = json.loads((tmp_path / "o" / "mentions_by_relation.json").read_text())
    assert r["mentions_by_relation"] == {"supplier": 1, "customer": 2}
    assert r["mentions_by_relation_universe_src"] == {"supplier": 1, "customer": 1}
    assert r["mentions_outside_universe"] == 1
    assert r["misses_by_reason"] == {"form_40f": 2, "no_filing": 1}
    assert r["form_40f_N"] == 1 and r["misses_among_unfiled_by_reason"] == {"form_40f": 1, "no_filing": 1}
    assert r["universe_total"] == 5                                              # ETF is excluded, LOW is below the floor
    assert r["filing_rows"] == 4 and r["filing_rows_non_excluded"] == 3          # ETF dropped like the engine's filter
    assert r["filing_rows_in_universe"] == 1 and r["filing_rows_outside_universe"] == 2   # OUT and the below-floor LOW
    assert r["unfiled_in_universe"] == 4 and r["no_cik"] == 1 and r["misses_in_unfiled"] == 2
    assert r["pending_unfiled"] == 1                                             # PEND
    assert r["pending_engine_d4"] == 0                                           # max(0, 5 - 3 - 1 - 2): the old rows eat it
    assert r["pending_today"] == 7 and r["pending_today_source"] == "--vc-payload pending"
    assert r["pending_delta_today_vs_unfiled"] == 6


def test_mentions_pending_today_is_unknown_without_a_payload(tmp_path):
    mod = _load("387_mentions_by_relation")
    edgar = _store(tmp_path / "edgar_1.duckdb", {"chain_mentions": [], "chain_filings": [], "chain_meta": []})
    raw = _store(tmp_path / "raw_1.duckdb", {"equity_universe": _universe_rows(["A"])})
    assert mod.cm.run_main(mod.main, ["--edgar", str(edgar), "--raw", str(raw), "--out", str(tmp_path / "o")]) == 0
    r = json.loads((tmp_path / "o" / "mentions_by_relation.json").read_text())
    assert r["pending_today"] is None and r["pending_unfiled"] == 1


# ── 388_payload_fields / 379_payload_shape ───────────────────────────────────
def test_payload_fields_measures_keys_and_the_rel4_saving(tmp_path):
    mod = _load("388_payload_fields")
    cos = [{"ticker": f"T{i}", "name": f"Name {i}", "weights": {f"term{j}": 1.0 for j in range(30)},
            "profile": {"products": [f"p{j}" for j in range(30)]}} for i in range(20)]
    payload = tmp_path / "relatedness_latest.json"
    payload.write_text(json.dumps({"companies": cos, "peers": {"T0": [{"peer": "T1", "score": 0.5}]}}), encoding="utf-8")
    assert mod.cm.run_main(mod.main, ["--payload", str(payload), "--out", str(tmp_path / "o")]) == 0
    r = json.loads((tmp_path / "o" / "payload_fields.json").read_text())
    assert r["companies"] == 20 and set(r["per_company_key"]) == {"ticker", "name", "weights", "profile"}
    assert r["per_company_key"]["weights"]["raw"] > r["per_company_key"]["name"]["raw"]
    assert r["without_weights_profile"]["gzip"] < r["whole_payload"]["gzip"]
    assert r["dropped_keys"] == ["weights", "profile"]


def test_payload_shape_saves_the_repeated_cluster_fields(tmp_path):
    mod = _load("379_payload_shape")
    clusters = {"0": [{"id": cid, "label": f"Group number {cid}", "n": 12, "mean_corr": 0.51, "baseline_r": 0.2,
                       "significant": True, "persist": 0.8, "last_residual_pct": 1.2,
                       "members": [{"ticker": f"C{cid}M{i}", "name": "n", "sector": "Technology", "industry": "x"}
                                   for i in range(12)]} for cid in range(5)]}
    payload = tmp_path / "comovement_latest.json"
    payload.write_text(json.dumps({"cuts": ["0"], "clusters": clusters}), encoding="utf-8")
    assert mod.cm.run_main(mod.main, ["--payload", str(payload), "--out", str(tmp_path / "o")]) == 0
    r = json.loads((tmp_path / "o" / "payload_shape.json").read_text())
    assert r["rows"] == 60 and r["clusters"] == 5
    assert r["data_after"]["raw"] < r["data_before"]["raw"] and r["saving_raw"] > 0
    assert set(r["repeated_fields"]) >= {"cluster_label", "mean_corr"}


# ── 379_d01_vs_swing_ohlc ────────────────────────────────────────────────────
def _weekdays(n, start=date(2026, 6, 1)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _epoch(day, hour=12):
    return datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc).timestamp()


def _bar(t, d, close, fetched=1.0):
    return {"ticker": t, "session_date": d, "close": close, "open": close, "high": close, "low": close,
            "volume": 1.0, "fetched_at": fetched}


def test_d01_return_gap_is_the_headline_and_each_cause_is_named(tmp_path):
    mod = _load("379_d01_vs_swing_ohlc")
    days = _weekdays(40)
    cache_ok = _epoch(days[-1] + timedelta(days=2))          # written after the last session closed
    cache_partial = _epoch(days[-1], hour=12)               # written 08:00 New York on the last day: before the close
    swing, bars, actions = [], [], []

    def add(t, closes, cached=cache_ok):
        for i, (d, c) in enumerate(zip(days, closes)):
            swing.append({"ticker": t, "ts": d.isoformat() + ("T00:00:00-04:00" if i % 2 else ""), "close": c,
                          "cached_at": cached if i == len(days) - 1 else _epoch(days[0])})
    flat = [100.0] * 40
    add("SAME", flat)
    add("OFF", flat)                                               # D01 has one outlier day: a RETURN gap
    add("DIV", flat)                                               # a dividend after the cache day: a LEVEL offset only
    add("SPL", [200.0] * 20 + [100.0] * 20)                        # swing missed a split: the jump is on the swing side
    add("PART", flat[:-1] + [110.0], cached=cache_partial)         # swing's last bar is unfinished
    add("NOD01", flat)
    for t in ("SAME", "DIV", "PART"):
        bars += [_bar(t, d, 100.0) for d in days]
    bars += [_bar("OFF", d, 101.0 if i == 10 else 100.0) for i, d in enumerate(days)]
    bars += [_bar("SPL", d, 200.0 if i < 20 else 100.0) for i, d in enumerate(days)]
    actions.append({"ticker": "DIV", "ex_date": days[-1] + timedelta(days=10), "action": "dividend", "value": 2.0, "fetched_at": 1.0})
    actions.append({"ticker": "SPL", "ex_date": days[20], "action": "split", "value": 2.0, "fetched_at": 1.0})
    raw = _store(tmp_path / "raw_20261014_040000_000000.duckdb", {"swing_ohlc": swing})
    prices = _store(tmp_path / "prices_20261014_040000_000000.duckdb", {"price_bars_daily": bars, "price_actions": actions})
    digests = (_digest(raw), _digest(prices))
    assert mod.cm.run_main(mod.main, ["--raw", str(raw), "--prices", str(prices), "--out", str(tmp_path / "o")]) == 0
    r = json.loads((tmp_path / "o" / "d01_vs_swing_ohlc.json").read_text())
    assert r["swing_tickers"] == 6 and r["compared"] == 5 and r["no_d01_sample"] == ["NOD01"]
    assert r["over_return_threshold"] == 2                         # OFF and SPL; DIV's 2% level gap is NOT a return gap
    assert r["over_level_raw_threshold"] >= 2                      # DIV (and the split's raw level)
    assert r["causes"] == {"return_gap": 1, "split_like": 1, "offset_after_cache": 1, "partial_last_bar": 1}
    assert r["split_like_sides"] == {"swing": 1}
    assert r["partial_last_bar_tickers"] == 1
    by = {p["ticker"]: p for p in csv_rows(tmp_path / "o" / "d01_vs_swing_ohlc.csv")}
    assert by["DIV"]["cause"] == "offset_after_cache" and float(by["DIV"]["return_gap"]) < 1e-9
    assert float(by["DIV"]["level_gap_raw"]) > 0.015 and float(by["DIV"]["level_gap_rebased"]) < 1e-9
    assert by["PART"]["cause"] == "partial_last_bar" and by["PART"]["partial_last_bar"] == "True"
    assert float(by["PART"]["return_gap"]) < 1e-9                  # the unfinished bar is left out of the metric
    assert by["SPL"]["side"] == "swing" and by["SPL"]["worst_date"] == days[20].isoformat()
    assert by["OFF"]["cause"] == "return_gap" and by["OFF"]["worst_date"] in {days[10].isoformat(), days[11].isoformat()}
    assert r["worst_date_histogram"][days[20].isoformat()] == 1
    assert r["tickers_over_per_date"][days[20].isoformat()] == 1
    assert r["swing_last_day_histogram"] == {days[-1].isoformat(): 6}
    assert (tmp_path / "o" / "d01_vs_swing_ohlc_dates.csv").read_text().startswith("date,tickers_over")
    assert (_digest(raw), _digest(prices)) == digests


def test_d01_split_on_the_d01_side_is_named(tmp_path):
    mod = _load("379_d01_vs_swing_ohlc")
    days = _weekdays(40)
    swing = [{"ticker": "S", "ts": d.isoformat(), "close": 100.0, "cached_at": _epoch(days[-1] + timedelta(days=2))} for d in days]
    # D01 nominal bars show 100 -> 50 on day 20 with NO split action: the jump is D01's
    bars = [_bar("S", d, 100.0 if i < 20 else 50.0) for i, d in enumerate(days)]
    raw = _store(tmp_path / "raw_1.duckdb", {"swing_ohlc": swing})
    prices = _store(tmp_path / "prices_1.duckdb", {"price_bars_daily": bars, "price_actions": []})
    r = mod.measure(duckdb.connect(str(raw), read_only=True), duckdb.connect(str(prices), read_only=True))
    assert r["causes"] == {"split_like": 1} and r["split_like_sides"] == {"d01": 1}


def test_d01_uses_the_last_n_sessions(tmp_path):
    mod = _load("379_d01_vs_swing_ohlc")
    days = [date(2026, 1, 1) + timedelta(days=i) for i in range(60)]
    swing = [{"ticker": "T", "ts": d.isoformat(), "close": 100.0} for d in days]
    bars = [_bar("T", d, 100.0 if i >= 30 else 150.0) for i, d in enumerate(days)]
    raw = _store(tmp_path / "raw_1.duckdb", {"swing_ohlc": swing})
    prices = _store(tmp_path / "prices_1.duckdb", {"price_bars_daily": bars, "price_actions": []})
    rcon, pcon = duckdb.connect(str(raw), read_only=True), duckdb.connect(str(prices), read_only=True)
    assert mod.measure(rcon, pcon, sessions=30)["over_return_threshold"] == 0   # the old jump is outside the window
    assert mod.measure(rcon, pcon, sessions=60)["over_return_threshold"] == 1


def csv_rows(path):
    import csv
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# ── 388_reprofile_delta (D5) ─────────────────────────────────────────────────
def _scores(top: dict[str, list[str]]) -> list[dict]:
    return [{"ticker": t, "peer": p, "score": 1.0 - i / 10} for t, peers in top.items() for i, p in enumerate(peers)]


def test_reprofile_delta_counts_changed_top_five(tmp_path):
    mod = _load("388_reprofile_delta")
    old = {"A": list("BCDEF"), "B": list("ACDEF"), "C": list("ABDEF"), "D": list("ABCEF")}
    new = {"A": list("BCDEG"), "B": list("ACDEF"), "C": list("GHIJK"), "D": list("ABCEF")}
    new["B"] = ["A", "C", "D", "F", "E"]                             # same set, new order: not a change
    before = _store(tmp_path / "raw_20261015_100000_000000.duckdb", {
        "relatedness_scores": _scores(old) + [{"ticker": "A", "peer": "Z", "score": 0.01}],   # 6th peer: ignored
        "relatedness_profiles": [{"ticker": t, "prompt_version": "v1", "status": "ok"} for t in "ABCD"]})
    after = _store(tmp_path / "raw_20261016_100000_000000.duckdb", {
        "relatedness_scores": _scores(new),
        "relatedness_profiles": [{"ticker": t, "prompt_version": "v2" if t != "D" else "v1", "status": "ok"}
                                 for t in "ABCD"]})
    digests = (_digest(before), _digest(after))
    res = mod.cm.run_main(mod.main, ["--before", str(before), "--after", str(after), "--version", "v2",
                                     "--out", str(tmp_path / "out")])
    assert res == 0
    out = json.loads((tmp_path / "out" / "reprofile_delta.json").read_text())
    assert out["reprofiled"] == {"names": 3, "changed": 2, "changed_share": 0.6667, "mean_overlap": 3.0,
                                 "overlap_of": 5}                    # A 4/5, B 5/5, C 0/5
    assert out["all_scored"]["names"] == 4 and out["all_scored"]["changed"] == 2
    assert out["smallest_overlap"][0]["ticker"] == "C"
    assert (_digest(before), _digest(after)) == digests


def test_reprofile_delta_refuses_a_live_store(tmp_path):
    mod = _load("388_reprofile_delta")
    live = tmp_path / ".store"
    live.mkdir()
    raw = _store(live / "raw.duckdb", {"relatedness_scores": [], "relatedness_profiles": []})
    assert mod.cm.run_main(mod.main, ["--before", str(raw), "--after", str(raw)]) == 2
