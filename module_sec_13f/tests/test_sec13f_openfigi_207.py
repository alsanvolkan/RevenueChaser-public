"""The 13F CUSIP map (#207 F1b): no name match, the map from ``identity_cusip``, the dollar guards (13F-6) and the
map identity (13F-5). Store only; the OpenFIGI client is faked in ``app/tests/test_sec13f_map_207.py``."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from RevenueChaser.core import history, identity_cusip, rawstore
from RevenueChaser.module_sec_13f import engine
from RevenueChaser.module_sec_13f import history as sec13f_history

RUN_AT = datetime(2026, 10, 14, 21, 0, tzinfo=timezone.utc).timestamp()
Q1, Q2 = "2026-03-31", "2026-06-30"


@pytest.fixture
def store(rawstore_tmp_db):
    return rawstore_tmp_db


def _seed(holdings, names=None):
    """``holdings``: ``[(accession, cik, period, cusip, shares, value)]``, one 13F-HR per accession."""
    subs = {}
    for acc, cik, period, *_ in holdings:
        subs[acc] = {"accession": acc, "cik": cik, "period": period, "filing_date": "2026-08-01", "type": "13F-HR"}
    rawstore.write_rows("sec13f_submissions", list(subs.values()))
    rawstore.write_rows("sec13f_holdings", [{"accession": a, "cik": c, "cusip": cu, "shares": sh, "value": v,
                                             "line_no": i + 1, "sshprnamttype": "SH", "putcall": ""}
                                            for i, (a, c, _p, cu, sh, v) in enumerate(holdings)])
    if names:
        rawstore.write_rows("sec13f_cusip_names", [{"cusip": c, "name_norm": engine.normalize_issuer_name(n),
                                                    "name_raw": n, "count": 1} for c, n in names.items()])


def _answer(ticker, share_class_figi=None):
    return {"status": "present", "ticker": ticker, "figi": "F-" + ticker, "share_class_figi": share_class_figi,
            "share_class": None, "name": ticker, "exch_code": "US", "security_type": "Common Stock",
            "fetched_at": RUN_AT}


ABSENT = {"status": "absent", "ticker": None, "figi": None, "share_class_figi": None, "share_class": None,
          "name": None, "exch_code": None, "security_type": None, "fetched_at": RUN_AT}


class TestNoNameMatch:
    def test_a_name_matched_cusip_is_never_written_to_cusip_ticker(self, store):
        """O+E review §10 rule 1: the name seed and the issuer names are there, and still nothing is written."""
        _seed([("a1", "1000", Q2, "037833100", 100, 1000)], names={"037833100": "APPLE INC"})
        rawstore.write_rows("sec13f_ticker_names", [{"ticker": "AAPL", "entry": {"name": "APPLE INC"}}])
        assert engine.recompute_totals() == {}           # nothing mapped: nothing to derive, nothing kept
        assert engine.load_cusip_ticker() == {}

    def test_the_stored_map_is_used_as_is_and_not_grown(self, store):
        _seed([("a1", "1000", Q2, "037833100", 100, 1000), ("a1", "1000", Q2, "594918104", 50, 500)],
              names={"037833100": "APPLE INC", "594918104": "MICROSOFT CORP"})
        rawstore.write_rows("sec13f_ticker_names", [{"ticker": "MSFT", "entry": {"name": "MICROSOFT CORP"}}])
        engine.save_cusip_ticker({"037833100": "AAPL"})
        assert set(engine.recompute_totals()) == {"AAPL"}
        assert engine.load_cusip_ticker() == {"037833100": "AAPL"}

    def test_the_name_seed_readers_are_gone(self):
        for name in ("load_ticker_names", "save_ticker_names", "build_ticker_name_index",
                     "build_cusip_ticker_by_name_db"):
            assert not hasattr(engine, name), name


class TestMapFromIdentity:
    def test_dual_class_cusips_derive_two_tickers(self, store):
        _seed([("a1", "1000", Q2, "02079K305", 100, 10_000), ("a1", "1000", Q2, "02079K107", 40, 4_000),
               ("a2", "2000", Q2, "02079K305", 10, 1_000)],
              names={"02079K305": "ALPHABET INC CL A", "02079K107": "ALPHABET INC CL C"})
        identity_cusip.record({"02079K305": _answer("GOOGL", "BBG009S39JY5"),
                               "02079K107": _answer("GOOG", "BBG009S3NB21")}, run_id="job-1", run_at=RUN_AT)
        totals = engine.recompute_totals(identity_cusip.current_map())
        assert totals["GOOGL"][Q2]["shares"] == 110 and totals["GOOGL"][Q2]["holders"] == 2
        assert totals["GOOG"][Q2]["shares"] == 40 and totals["GOOG"][Q2]["holders"] == 1

    def test_an_accepted_derive_rebuilds_cusip_ticker_and_deletes_the_old_rows(self, store):
        _seed([("a1", "1000", Q2, "02079K305", 100, 10_000)])
        engine.save_cusip_ticker({"02079K305": "GOOGL", "02079K107": "GOOGL", "OLDCUSIP1": "GONE"})
        identity_cusip.record({"02079K305": _answer("GOOGL", "BBG009S39JY5")}, run_id="job-1", run_at=RUN_AT)
        engine.recompute_totals(identity_cusip.current_map())
        stored = engine.load_cusip_ticker()
        assert set(stored) == {"02079K305"}
        assert stored["02079K305"]["ticker"] == "GOOGL" and stored["02079K305"]["source"] == "openfigi"
        assert stored["02079K305"]["share_class_figi"] == "BBG009S39JY5"

    def test_no_holdings_leave_the_stored_map_alone(self, store):
        engine.save_cusip_ticker({"OLDCUSIP1": "OLD"})
        identity_cusip.record({"02079K305": _answer("GOOGL")}, run_id="job-1", run_at=RUN_AT)
        assert engine.recompute_totals(identity_cusip.current_map()) == {}
        assert engine.load_cusip_ticker() == {"OLDCUSIP1": "OLD"}


def _ten_plus_one(big_value=1_000.0):
    """11 tickers in Q2: T0..T9 with value 10 each, BIG with ``big_value``; one CUSIP each."""
    rows = [("a1", "1000", Q2, f"C{i:08d}", 1, 10.0) for i in range(10)]
    rows.append(("a1", "1000", Q2, "BIGCUSIP1", 1, big_value))
    _seed(rows)
    mapping = {f"C{i:08d}": f"T{i}" for i in range(10)}
    mapping["BIGCUSIP1"] = "BIG"
    engine.save_cusip_ticker(mapping)
    engine.recompute_totals()
    return mapping


class TestDollarGuard:
    def test_a_derive_dropping_the_newest_quarters_value_is_refused_though_the_ticker_count_passes(self, store):
        mapping = _ten_plus_one()
        new_map = {c: t for c, t in mapping.items() if c != "BIGCUSIP1"}   # 10 of 11 tickers: the count passes
        with pytest.raises(engine.DeriveRefused, match="mapped value"):
            engine.recompute_totals(new_map)
        assert "BIG" in engine.load_totals()
        assert engine.load_cusip_ticker() == mapping                        # the stored map is kept too

    def test_a_rising_unmapped_share_is_refused_though_the_value_ratio_passes(self, store, monkeypatch):
        mapping = _ten_plus_one(big_value=100.0)                            # held 200: BIG is half of it
        monkeypatch.setattr(engine, "_VALUE_RATIO", 0.0)                    # only the share rule can refuse
        two_less = {c: t for c, t in mapping.items() if c not in ("C00000000", "C00000001")}   # 0% -> 10%
        with pytest.raises(engine.DeriveRefused, match="unmapped share"):
            engine.recompute_totals(two_less)
        one_less = {c: t for c, t in mapping.items() if c != "C00000000"}  # 0% -> 5%: not more than 5 points
        assert "T0" not in engine.recompute_totals(one_less)

    def test_the_share_rule_counts_value_held_in_cusips_no_map_ever_had(self, store, monkeypatch):
        mapping = _ten_plus_one(big_value=100.0)
        _seed([("a9", "9000", Q2, "NEVERMAP1", 1, 800.0)])                  # held 1000, mapped 200
        monkeypatch.setattr(engine, "_VALUE_RATIO", 0.0)
        new_map = {c: t for c, t in mapping.items() if c not in ("C00000000", "C00000001", "C00000002",
                                                                  "C00000003")}   # -40 of 1000: 4 points
        assert "T5" in engine.recompute_totals(new_map)

    def test_force_overrides_the_dollar_guard(self, store):
        mapping = _ten_plus_one()
        new_map = {c: t for c, t in mapping.items() if c != "BIGCUSIP1"}
        assert "BIG" not in engine.recompute_totals(new_map, force=True)
        assert "BIGCUSIP1" not in engine.load_cusip_ticker()

    def test_a_small_drop_passes(self, store):
        mapping = _ten_plus_one()
        new_map = {c: t for c, t in mapping.items() if c != "C00000000"}    # -10 of 1100
        assert "T0" not in engine.recompute_totals(new_map)

    def test_the_guard_reads_the_newest_stored_quarter(self, store):
        _seed([("q1", "1000", Q1, f"C{i:08d}", 1, 1.0) for i in range(11)])
        mapping = _ten_plus_one()
        new_map = {c: t for c, t in mapping.items() if c != "BIGCUSIP1"}
        with pytest.raises(engine.DeriveRefused, match=Q2):
            engine.recompute_totals(new_map)

    def test_the_guard_settings_are_in_the_history_config(self):
        cfg = sec13f_history.effective_config()
        assert cfg["VALUE_RATIO"] == engine._VALUE_RATIO and cfg["UNMATCHED_RISE"] == engine._UNMATCHED_RISE


class TestCandidates:
    def test_held_cusips_with_no_identity_row_newest_period_and_largest_value_first(self, store):
        _seed([("a1", "1000", Q2, "SMALL0001", 1, 10.0), ("a1", "1000", Q2, "LARGE0001", 1, 900.0),
               ("q1", "1000", Q1, "OLDONLY01", 1, 5_000.0), ("a1", "1000", Q2, "ASKEDYES1", 1, 50.0),
               ("a1", "1000", Q2, "ASKEDNO01", 1, 60.0), ("old", "1000", "2024-12-31", "TOOOLD001", 1, 1.0)])
        identity_cusip.record({"ASKEDYES1": _answer("YES"), "ASKEDNO01": ABSENT}, run_id="job-1", run_at=RUN_AT)
        assert engine.map_candidates() == ["LARGE0001", "SMALL0001", "OLDONLY01"]

    def test_superseded_filings_and_rows_without_a_cik_are_not_candidates(self, store):
        rawstore.write_rows("sec13f_submissions", [
            {"accession": "orig", "cik": "1000", "period": Q2, "filing_date": "2026-08-01", "type": "13F-HR"},
            {"accession": "amend", "cik": "1000", "period": Q2, "filing_date": "2026-08-09", "type": "13F-HR/A"}])
        rawstore.write_rows("sec13f_holdings", [
            {"accession": "orig", "line_no": 1, "cik": "1000", "cusip": "ORIGONLY1", "shares": 1, "value": 1.0,
             "sshprnamttype": "SH", "putcall": ""},
            {"accession": "amend", "line_no": 1, "cik": "1000", "cusip": "AMENDED01", "shares": 1, "value": 1.0,
             "sshprnamttype": "SH", "putcall": ""},
            {"accession": "amend", "line_no": 2, "cik": "", "cusip": "NOCIK0001", "shares": 1, "value": 1.0,
             "sshprnamttype": "SH", "putcall": ""}])
        assert engine.map_candidates() == ["AMENDED01"]

    def test_an_empty_store_has_no_candidates(self, store):
        assert engine.map_candidates() == []


class TestMapIdentity:
    def test_count_by_source_hash_and_unmatched(self):
        legacy = {"111111111": "AAPL", "222222222": {"ticker": "MSFT"}}
        figi = {"333333333": {"ticker": "GOOGL", "source": "openfigi"}, "444444444": {"ticker": ""}}
        ident = engine.map_identity({**legacy, **figi}, {"cusips": 7, "value": 1.5e9})
        assert ident["count"] == 3 and ident["by_source"] == {"legacy": 2, "openfigi": 1}
        assert ident["unmatched_cusips"] == 7 and ident["unmatched_value"] == 1.5e9
        assert len(ident["hash"]) == 16

    def test_the_hash_follows_the_pairs_not_the_entry_shape(self):
        a = engine.map_identity({"111111111": "AAPL"})
        assert a["hash"] == engine.map_identity({"111111111": {"ticker": "aapl", "source": "openfigi"}})["hash"]
        assert a["hash"] != engine.map_identity({"111111111": "AAPL2"})["hash"]
        assert a["unmatched_cusips"] is None

    def test_the_history_header_carries_the_map_identity(self, store):
        _seed([("a1", "1000", Q2, "037833100", 100, 1000)])
        engine.save_cusip_ticker({"037833100": {"ticker": "AAPL", "source": "openfigi"}})
        engine.recompute_totals()
        ident = engine.map_identity(engine.load_cusip_ticker(), {"cusips": 0, "value": 0.0})
        sec13f_history.record({"totals": engine.load_totals(), "meta": {}, "enrichment": {}},
                              run_id="job-1", run_at=RUN_AT, map_identity=ident, map_note="CUSIP map: test note")
        head = history.last_run(sec13f_history.BOARD)
        assert head["context"]["map"] == ident
        assert "CUSIP map: test note" in head["warnings"]

    def test_without_an_identity_the_header_reads_the_stored_map(self, store):
        _seed([("a1", "1000", Q2, "037833100", 100, 1000)])
        engine.save_cusip_ticker({"037833100": "AAPL"})
        engine.recompute_totals()
        sec13f_history.record({"totals": engine.load_totals(), "meta": {}, "enrichment": {}},
                              run_id="job-1", run_at=RUN_AT)
        assert history.last_run(sec13f_history.BOARD)["context"]["map"]["by_source"] == {"legacy": 1}


def _figi(**tickers):
    """``{cusip: current_map entry}`` the way ``identity_cusip.current_map`` returns it."""
    return {c: {"ticker": t, "figi": "F-" + t, "share_class_figi": None, "source": "openfigi", "run_at": RUN_AT}
            for c, t in tickers.items()}


class TestLegacyPrefixFallback:
    """avo's option B (#207 decision card, 2026-10-15) minus 9x (card "9x'leri at", 2026-10-15): a legacy CUSIP
    OpenFIGI does not map keeps its legacy ticker only when OpenFIGI maps another CUSIP of the same issuer prefix
    (first 6 characters) to that same ticker and its issue number is not 90-99 (option-like)."""

    def test_a_prefix_confirmed_by_the_same_ticker_keeps_the_legacy_ticker(self):
        figi = _figi(**{"02079K305": "GOOGL", "097023105": "BA"})
        legacy = {"02079K305": "GOOGL", "02079K503": "GOOGL", "097023204": {"ticker": "ba"}}
        cmap, out = engine.legacy_fallback(figi, legacy)
        assert out["kept"] == ["02079K503", "097023204"]
        assert cmap["02079K503"] == {"ticker": "GOOGL", "source": "legacy"}
        assert cmap["097023204"] == {"ticker": "BA", "source": "legacy"}
        assert cmap["02079K305"] == figi["02079K305"]                       # OpenFIGI's own entry is untouched

    def test_an_option_like_issue_number_is_dropped_by_default(self):
        """02079K909 / 097023956: issue 90 / 95, the usual call / put CUSIPs in 13F filings."""
        figi = _figi(**{"02079K305": "GOOGL", "097023105": "BA"})
        cmap, out = engine.legacy_fallback(figi, {"02079K909": "GOOGL", "097023956": "BA", "097023204": "BA"})
        assert out["option_like"] == ["02079K909", "097023956"] and out["kept"] == ["097023204"]
        assert "02079K909" not in cmap and "097023956" not in cmap

    def test_a_multi_ticker_prefix_is_kept_by_default(self):
        """GOOGL's classes share 02079K: the default (unlike B-strict) keeps a confirmed legacy CUSIP there."""
        figi = _figi(**{"02079K305": "GOOGL", "02079K107": "GOOG"})
        assert engine.legacy_fallback(figi, {"02079K503": "GOOGL"})[1]["kept"] == ["02079K503"]

    def test_a_prefix_that_belongs_to_another_ticker_drops_the_legacy_ticker(self):
        """The fund-CUSIP glue: 25434V609 legacy DFAT while OpenFIGI maps the prefix to DEHP/DEXC/DFAE."""
        figi = _figi(**{"25434V708": "DEHP", "25434V807": "DEXC", "25434V880": "DFAE",
                        "74347B300": "DOG", "74347B409": "EFAD"})
        cmap, out = engine.legacy_fallback(figi, {"25434V609": "DFAT", "74347B268": "QQQ"})
        assert out == {"kept": [], "other_ticker": ["25434V609", "74347B268"], "unknown_prefix": [],
                       "option_like": [], "strict_dropped": []}
        assert "25434V609" not in cmap and "74347B268" not in cmap

    def test_a_prefix_unknown_to_openfigi_drops_the_legacy_ticker(self):
        """G54950103 is Linde, the legacy map said HEI: no OpenFIGI answer covers the prefix."""
        cmap, out = engine.legacy_fallback(_figi(**{"422806109": "HEI"}), {"G54950103": "HEI"})
        assert out == {"kept": [], "other_ticker": [], "unknown_prefix": ["G54950103"], "option_like": [],
                       "strict_dropped": []}
        assert set(cmap) == {"422806109"}

    def test_openfigis_answer_wins_over_the_legacy_ticker(self):
        figi = _figi(**{"02079K305": "GOOGL", "02079K107": "GOOG"})
        cmap, out = engine.legacy_fallback(figi, {"02079K107": "GOOGL"})       # the legacy map merged the classes
        assert cmap["02079K107"]["ticker"] == "GOOG" and out["kept"] == []

    def test_a_cusip_openfigi_once_mapped_is_never_revived(self):
        figi = _figi(**{"02079K305": "GOOGL"})
        cmap, out = engine.legacy_fallback(figi, {"02079K503": {"ticker": "GOOGL", "source": "openfigi"}},
                                           ever_mapped={"02079K503"})
        assert "02079K503" not in cmap and out == {"kept": [], "other_ticker": [], "unknown_prefix": [],
                                                   "option_like": [], "strict_dropped": []}

    def test_the_fallback_is_stable_on_its_own_output(self):
        figi = _figi(**{"02079K305": "GOOGL"})
        cmap, _ = engine.legacy_fallback(figi, {"02079K503": "GOOGL", "02079K909": "GOOGL", "G54950103": "HEI"})
        again, out = engine.legacy_fallback(figi, cmap, ever_mapped={"02079K305"})
        assert again == cmap and out["kept"] == ["02079K503"]

    def test_entries_without_a_ticker_are_ignored_and_no_openfigi_map_keeps_nothing(self):
        cmap, out = engine.legacy_fallback(_figi(**{"02079K305": "GOOGL"}), {"02079K909": "", "02079K808": {}})
        assert set(cmap) == {"02079K305"} and out["kept"] == []
        assert engine.legacy_fallback({}, {"02079K909": "GOOGL"})[0] == {}

    def test_the_map_identity_counts_kept_entries_as_legacy(self):
        cmap, _ = engine.legacy_fallback(_figi(**{"02079K305": "GOOGL"}), {"02079K503": "GOOGL"})
        assert engine.map_identity(cmap)["by_source"] == {"legacy": 1, "openfigi": 1}

    def test_a_derive_with_the_fallback_map_keeps_the_confirmed_legacy_value(self, store):
        _seed([("a1", "1000", Q2, "02079K305", 100, 10_000), ("a1", "1000", Q2, "02079K503", 5, 500),
               ("a1", "1000", Q2, "25434V609", 7, 700), ("a1", "1000", Q2, "G54950103", 9, 900)])
        engine.save_cusip_ticker({"02079K503": "GOOGL", "25434V609": "DFAT", "G54950103": "HEI"})
        identity_cusip.record({"02079K305": _answer("GOOGL"), "25434V708": _answer("DEHP"),
                               "02079K503": ABSENT, "25434V609": ABSENT, "G54950103": ABSENT},
                              run_id="job-1", run_at=RUN_AT)
        cmap, _ = engine.legacy_fallback(identity_cusip.current_map(), engine.load_cusip_ticker(),
                                         identity_cusip.ever_mapped())
        totals = engine.recompute_totals(cmap)
        assert set(totals) == {"GOOGL"} and totals["GOOGL"][Q2]["shares"] == 105
        assert engine.load_cusip_ticker()["02079K503"] == {"ticker": "GOOGL", "source": "legacy"}


class TestStrictFallback:
    """B-strict (opt-in, #427 review MUST-2): on top of the default (9x dropped), keep a legacy ticker only when
    OpenFIGI maps the prefix to exactly one ticker."""

    def test_the_default_is_not_strict(self):
        assert engine._FALLBACK_STRICT is False

    def test_strict_also_drops_an_option_like_issue_number(self):
        cmap, out = engine.legacy_fallback(_figi(**{"097023105": "BA"}), {"097023956": "BA", "097023204": "BA"},
                                           strict=True)
        assert out["kept"] == ["097023204"] and out["option_like"] == ["097023956"] and out["strict_dropped"] == []
        assert "097023956" not in cmap and cmap["097023204"] == {"ticker": "BA", "source": "legacy"}

    def test_strict_drops_a_prefix_that_maps_to_more_than_one_ticker(self):
        figi = _figi(**{"464287200": "IVV", "464287655": "IWM"})
        cmap, out = engine.legacy_fallback(figi, {"464287101": "IVV"}, strict=True)
        assert out["kept"] == [] and out["strict_dropped"] == ["464287101"] and "464287101" not in cmap
        assert engine.legacy_fallback(figi, {"464287101": "IVV"})[1]["kept"] == ["464287101"]  # default keeps

    def test_strict_never_keeps_what_b_drops(self):
        figi = _figi(**{"25434V708": "DEHP", "422806109": "HEI"})
        _, out = engine.legacy_fallback(figi, {"25434V609": "DFAT", "G54950103": "HEI"}, strict=True)
        assert out == {"kept": [], "other_ticker": ["25434V609"], "unknown_prefix": ["G54950103"],
                       "option_like": [], "strict_dropped": []}

    def test_option_like_reads_the_issue_number(self):
        assert engine.option_like("02079K909") and engine.option_like("097023956")
        assert not engine.option_like("02079K305") and not engine.option_like("G54950103")
        assert not engine.option_like("02079K")

    def test_the_mode_is_in_the_history_config(self):
        assert sec13f_history.effective_config()["FALLBACK_STRICT"] is False
