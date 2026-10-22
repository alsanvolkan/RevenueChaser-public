"""#207 F1b: the CUSIP map step in the 13F Fetch and the 13F map job. Offline: temp stores, a fake OpenFIGI, fake UW.

- the Fetch asks OpenFIGI only once the map is seeded; the first map runs through the map job;
- the map job makes no SEC or UW call;
- a complete map rebuilds ``cusip_ticker`` when the derive passes; an incomplete one keeps the stored map;
- no key: the step is skipped with a job note and the derive uses the stored map;
- the history header carries the map identity.
"""
from __future__ import annotations

import time

import pytest

from RevenueChaser.app import runners
from RevenueChaser.app.tests.test_sec13f_job import GOOD, _zip, fake_uw, unmapped_store  # noqa: F401
from RevenueChaser.core import history, identity_cusip, ledger, rawstore
from RevenueChaser.core.providers import openfigi, sec_edgar
from RevenueChaser.module_sec_13f import engine as sec_13f
from RevenueChaser.module_sec_13f import history as sec13f_history

KEY = "figi-test-key-0123456789"
FIGI = {"111111111": "AAPL", "222222222": "GOOGL", "333333333": "GOOG"}


class _Resp:
    def __init__(self, status_code=200, body=None, headers=None):
        self.status_code, self._body, self.headers = status_code, body, headers or {}

    def json(self):
        return self._body


class _OpenFigi:
    """A fake ``openfigi._post``: answers from ``FIGI`` (unknown CUSIPs get the no-match warning), after ``throttle``
    429s; records the CUSIPs of every request."""

    def __init__(self, throttle=0):
        self.throttle = throttle
        self.asked: list[list[str]] = []

    def __call__(self, url, jobs, headers):
        assert headers["X-OPENFIGI-APIKEY"] == KEY
        self.asked.append([j["idValue"] for j in jobs])
        if self.throttle:
            self.throttle -= 1
            return _Resp(429, None, {"ratelimit-reset": "0"})
        return _Resp(200, [{"data": [{"ticker": FIGI[j["idValue"]], "figi": "F-" + j["idValue"],
                                      "shareClassFIGI": "S-" + FIGI[j["idValue"]], "exchCode": "US",
                                      "name": FIGI[j["idValue"]], "securityType": "Common Stock"}]}
                           if j["idValue"] in FIGI else {"warning": "No identifier found."} for j in jobs])

    @property
    def requests(self):
        return len(self.asked)


@pytest.fixture
def figi(monkeypatch):
    fake = _OpenFigi()
    monkeypatch.setattr(openfigi, "_post", fake)
    monkeypatch.setattr(openfigi, "_sleep", lambda s: None)
    monkeypatch.setenv("OPENFIGI_API_KEY", KEY)
    return fake


@pytest.fixture
def fetch(unmapped_store, fake_uw, figi, monkeypatch):  # noqa: F811
    """The ``unmapped_store`` fixture's one AAPL CUSIP (300 shares, 2026-03-31) with its legacy map row, plus a
    dual-class pair the legacy map merged onto GOOGL."""
    ledger.take_notes()
    monkeypatch.setattr(runners, "run_id", lambda: "job-13f")
    _zip(unmapped_store / "2026q1b_form13f.zip", [("b1", "3000", "222222222", "ALPHABET INC CL A", 70),
                                                  ("b1", "3000", "333333333", "ALPHABET INC CL C", 30),
                                                  ("b2", "4000", "999999999", "NOBODY KNOWS CORP", 5)])
    sec_13f.ingest_zips(sorted(unmapped_store.glob("*_form13f.zip")))
    sec_13f.save_cusip_ticker({"111111111": "AAPL", "222222222": "GOOGL", "333333333": "GOOGL"})
    sec_13f.recompute_totals()
    sec_13f.save_sec13f_meta({t: GOOD for t in ("AAPL", "GOOGL", "GOOG")})   # meta complete: no UW name step
    return unmapped_store


def _no_sec(monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("the map job reached the SEC")
    monkeypatch.setattr(sec_edgar, "get_text", refuse)
    monkeypatch.setattr(sec_edgar, "get_bytes", refuse)
    monkeypatch.setattr(sec_13f, "download_missing_zips", refuse)


class TestMapJob:
    def test_the_first_map_splits_the_classes_and_rebuilds_cusip_ticker(self, fetch, figi, fake_uw, monkeypatch):  # noqa: F811
        _no_sec(monkeypatch)
        with pytest.raises(runners.JobMessage, match="4 of 4 CUSIP.*3 mapped, 1 unknown to OpenFIGI"):
            runners.run_sec13f_map(None)
        totals = sec_13f.load_totals()
        assert totals["GOOGL"]["2026-03-31"]["shares"] == 70 and totals["GOOG"]["2026-03-31"]["shares"] == 30
        stored = sec_13f.load_cusip_ticker()
        assert {c: e["ticker"] for c, e in stored.items()} == FIGI                  # legacy rows replaced
        assert all(e["source"] == "openfigi" for e in stored.values())
        assert fake_uw.built == 0                                                   # no UW client
        assert figi.requests == 1

    def test_answers_are_recorded_in_identity_cusip_unknowns_as_absent(self, fetch, figi):
        with pytest.raises(runners.JobMessage):
            runners.run_sec13f_map(None)
        latest = identity_cusip.latest()
        assert {c: r["status"] for c, r in latest.items()} == {
            "111111111": "present", "222222222": "present", "333333333": "present", "999999999": "absent"}
        assert {r["run_id"] for r in latest.values()} == {"job-13f"}

    def test_a_429_then_200_is_mapped(self, fetch, figi):
        figi.throttle = 1
        with pytest.raises(runners.JobMessage, match=r"2 OpenFIGI request\(s\) \(1 rate-limited\)"):
            runners.run_sec13f_map(None)
        assert sec_13f.load_cusip_ticker()["333333333"]["ticker"] == "GOOG"

    def test_a_second_run_asks_nothing(self, fetch, figi):
        with pytest.raises(runners.JobMessage):
            runners.run_sec13f_map(None)
        with pytest.raises(runners.JobMessage, match="0 of 0 CUSIP"):
            runners.run_sec13f_map(None)
        assert figi.requests == 1

    def test_no_key_fails_the_job_and_keeps_the_stored_map(self, fetch, figi, monkeypatch):
        monkeypatch.delenv("OPENFIGI_API_KEY")
        with pytest.raises(RuntimeError, match="no OpenFIGI key"):
            runners.run_sec13f_map(None)
        assert figi.requests == 0
        assert sec_13f.load_cusip_ticker()["333333333"] == "GOOGL"

    def test_a_capped_run_keeps_the_stored_map_and_totals(self, fetch, figi, monkeypatch):
        figi.throttle = 99
        monkeypatch.setattr(openfigi, "MAX_ATTEMPTS", 2)
        with pytest.raises(RuntimeError, match="incomplete"):
            runners.run_sec13f_map(None)
        assert sec_13f.load_cusip_ticker()["333333333"] == "GOOGL"
        assert "GOOG" not in sec_13f.load_totals()

    def test_a_refused_derive_keeps_the_stored_map(self, fetch, figi, monkeypatch):
        def refuse(before, totals, factors=None):
            raise sec_13f.DeriveRefused("the derive would cut the mapped value")
        monkeypatch.setattr(sec_13f, "guard", refuse)
        with pytest.raises(sec_13f.DeriveRefused):
            runners.run_sec13f_map(None)
        assert sec_13f.load_cusip_ticker()["333333333"] == "GOOGL"
        assert identity_cusip.has_rows()                     # the answers are facts and stay

    def test_the_job_records_no_history(self, fetch, figi):
        with pytest.raises(runners.JobMessage):
            runners.run_sec13f_map(None)
        assert not rawstore.history_db_path().exists() or history.last_run(sec13f_history.BOARD) is None


class TestPrefixFallback:
    """avo's option B minus 9x (#207, 2026-10-15): the map step keeps a legacy ticker OpenFIGI does not map only when
    OpenFIGI maps another CUSIP of the same issuer prefix to that ticker and the issue number is not 90-99."""

    @pytest.fixture
    def legacy(self, fetch):
        _zip(fetch / "2026q1d_form13f.zip", [("d1", "6000", "222222888", "ALPHABET INC CL A OLD", 11),
                                            ("d1", "6000", "222222900", "ALPHABET INC CALL", 5),
                                            ("d1", "6000", "333333999", "FUND TRUST SERIES", 13),
                                            ("d1", "6000", "G54950103", "LINDE PLC", 17)])
        sec_13f.ingest_zips(sorted(fetch.glob("*_form13f.zip")))
        sec_13f.save_cusip_ticker({"111111111": "AAPL", "222222222": "GOOGL", "333333333": "GOOGL",
                                   "222222888": "GOOGL",        # prefix 222222: OpenFIGI says GOOGL -> kept
                                   "222222900": "GOOGL",        # same prefix, issue 90 (option-like) -> dropped
                                   "333333999": "QQQ",          # prefix 333333: OpenFIGI says GOOG -> dropped
                                   "G54950103": "HEI",          # prefix unknown to OpenFIGI -> dropped
                                   "N0000000X": "XYZ"})         # not held, prefix unknown -> dropped
        return fetch

    def test_the_map_job_keeps_only_the_prefix_confirmed_legacy_ticker(self, legacy, figi, monkeypatch):
        _no_sec(monkeypatch)
        with pytest.raises(runners.JobMessage,
                           match=r"kept 1 legacy CUSIP.* dropped 4 \(1 prefix of another ticker, 2 prefix "
                                 r"unknown to OpenFIGI, 1 option-like issue number"):
            runners.run_sec13f_map(None)
        stored = sec_13f.load_cusip_ticker()
        assert stored["222222888"] == {"ticker": "GOOGL", "source": "legacy"}
        assert "333333999" not in stored and "G54950103" not in stored and "222222900" not in stored
        totals = sec_13f.load_totals()
        assert totals["GOOGL"]["2026-03-31"]["shares"] == 70 + 11
        assert "QQQ" not in totals and "HEI" not in totals

    def test_the_step_counts_the_fallback(self, legacy, figi):
        cmap, note, stats = runners._13f_map_step("run-1", 0.0, first_run=True)
        assert note is None and set(cmap) == set(FIGI) | {"222222888"}
        assert (stats["legacy_kept"], stats["legacy_other_ticker"], stats["legacy_unknown_prefix"],
                stats["legacy_option_like"]) == (1, 1, 2, 1)

    def test_strict_mode_reaches_the_step(self, legacy, figi, monkeypatch):
        identity_cusip.record({"222222333": {"status": "present", "ticker": "GOOGX", "fetched_at": 0.0}},
                              run_id="seed", run_at=0.0)               # prefix 222222 now maps to two tickers
        cmap, _note, stats = runners._13f_map_step("run-1", 0.0, first_run=True)
        assert cmap["222222888"] == {"ticker": "GOOGL", "source": "legacy"}              # the default keeps it
        monkeypatch.setattr(sec_13f, "_FALLBACK_STRICT", True)
        cmap, _note, stats = runners._13f_map_step("run-2", 0.0, first_run=True)
        assert "222222888" not in cmap and stats["legacy_kept"] == 0 and stats["legacy_strict_dropped"] == 1

    def test_no_openfigi_ticker_at_all_is_no_map(self, legacy, figi):
        saved = dict(FIGI)
        FIGI.clear()                                           # every CUSIP answered 'absent'
        try:
            cmap, note, stats = runners._13f_map_step("run-1", 0.0, first_run=True)
        finally:
            FIGI.update(saved)
        assert cmap is None and note is None and stats["absent"] == 8 and stats["legacy_kept"] == 0

    def test_a_cusip_openfigi_mapped_and_then_dropped_is_not_kept_by_the_fallback(self, legacy, figi):
        with pytest.raises(runners.JobMessage):
            runners.run_sec13f_map(None)
        assert sec_13f.load_cusip_ticker()["222222888"]["source"] == "legacy"
        later = time.time() + 60
        identity_cusip.record({"222222888": {"status": "present", "ticker": "GOOGL", "fetched_at": later}},
                              run_id="r2", run_at=later)
        identity_cusip.record({"222222888": {"status": "absent", "fetched_at": later + 60}}, run_id="r3",
                              run_at=later + 60)
        cmap, _note, stats = runners._13f_map_step("run-4", later + 120)
        assert "222222888" not in cmap and stats["legacy_kept"] == 0      # OpenFIGI's later answer stands

    def test_a_later_fetch_keeps_the_kept_entry_and_revives_nothing(self, legacy, figi):
        with pytest.raises(runners.JobMessage):
            runners.run_sec13f_map(None)
        first = sec_13f.load_cusip_ticker()
        try:
            runners.build_sec13f(None)
        except runners.JobMessage:
            pass
        assert sec_13f.load_cusip_ticker() == first


class TestFetchStep:
    def test_before_the_map_is_seeded_a_fetch_asks_nothing_and_says_so(self, fetch, figi):
        with pytest.raises(runners.JobMessage, match="has not mapped the 13F holdings yet"):
            runners.build_sec13f(None)
        assert figi.requests == 0
        assert sec_13f.load_cusip_ticker()["333333333"] == "GOOGL"

    def test_once_seeded_a_fetch_asks_only_the_new_cusips(self, fetch, figi):
        with pytest.raises(runners.JobMessage):
            runners.run_sec13f_map(None)
        _zip(fetch / "2026q1c_form13f.zip", [("c1", "5000", "444444444", "NEW CO", 9)])
        FIGI["444444444"] = "NEWC"
        try:
            runners.build_sec13f(None)
        except runners.JobMessage:
            pass
        finally:
            del FIGI["444444444"]
        assert figi.asked[-1] == ["444444444"]
        assert sec_13f.load_cusip_ticker()["444444444"]["ticker"] == "NEWC"
        assert sec_13f.load_totals()["NEWC"]["2026-03-31"]["shares"] == 9

    def test_no_key_skips_the_step_with_a_job_note_and_the_derive_uses_the_stored_map(self, fetch, figi,
                                                                                      monkeypatch):
        identity_cusip.record({"999999999": {"status": "absent", "fetched_at": 0.0}}, run_id="seed", run_at=0.0)
        monkeypatch.delenv("OPENFIGI_API_KEY")
        with pytest.raises(runners.JobMessage, match="no OpenFIGI key"):
            runners.build_sec13f(None)
        assert figi.requests == 0
        assert sec_13f.load_totals()["GOOGL"]["2026-03-31"]["shares"] == 100       # the stored (legacy) map
        assert "GOOG" not in sec_13f.load_totals()

    def test_the_history_header_carries_the_map_identity(self, fetch, figi):
        with pytest.raises(runners.JobMessage):
            runners.run_sec13f_map(None)
        try:
            runners.build_sec13f(None)
        except runners.JobMessage:
            pass
        head = history.last_run(sec13f_history.BOARD)
        ident = head["context"]["map"]
        assert ident["count"] == 3 and ident["by_source"] == {"openfigi": 3}
        assert ident["hash"] == sec_13f.map_identity(sec_13f.load_cusip_ticker())["hash"]
        assert ident["unmatched_cusips"] == 1 and ident["unmatched_value"] == 1000.0

    def test_a_map_step_note_is_a_history_warning(self, fetch, figi):
        with pytest.raises(runners.JobMessage):
            runners.build_sec13f(None)
        head = history.last_run(sec13f_history.BOARD)
        assert any("has not mapped the 13F holdings yet" in w for w in head["warnings"])

    def test_a_refused_new_map_falls_back_to_the_stored_map_and_says_so(self, fetch, figi, monkeypatch):
        real_guard = sec_13f.guard

        def refuse_the_split(before, totals, factors=None):
            if "GOOG" in totals:
                raise sec_13f.DeriveRefused("the derive would cut the mapped value of 2026-03-31")
            real_guard(before, totals, factors)
        monkeypatch.setattr(sec_13f, "guard", refuse_the_split)
        with pytest.raises(sec_13f.DeriveRefused):
            runners.run_sec13f_map(None)                      # seeded, but the stored map stays the legacy one
        with pytest.raises(runners.JobMessage, match="new map was not applied"):
            runners.build_sec13f(None)                        # not a failed Fetch, and not every Fetch after
        assert sec_13f.load_totals()["GOOGL"]["2026-03-31"]["shares"] == 100
        assert sec_13f.load_cusip_ticker()["333333333"] == "GOOGL"
        head = history.last_run(sec13f_history.BOARD)
        assert head["error"] is None and any("new map was not applied" in w for w in head["warnings"])

    def test_a_raising_step_never_fails_the_fetch(self, fetch, figi, monkeypatch):
        def boom():
            raise RuntimeError("store hiccup")
        monkeypatch.setattr(sec_13f, "map_candidates", boom)
        with pytest.raises(runners.JobMessage, match="CUSIP map step failed"):
            runners.build_sec13f(None)
        assert "GOOGL" in sec_13f.load_totals()


class TestRoute:
    def test_the_map_job_is_registered_without_uw(self):
        from RevenueChaser.app import jobs
        import RevenueChaser.app.dashboard  # noqa: F401 - registers the jobs
        assert jobs._RUNNERS["sec13f_map"] is runners.run_sec13f_map
        assert jobs._REQUIRES_UW["sec13f_map"] is False
        assert jobs._PRODUCES["sec13f_map"] == ["module_sec_13f"]

    def test_the_route_submits_the_map_job(self, monkeypatch):
        import RevenueChaser.app.dashboard as d
        seen = []
        monkeypatch.setattr(d.jobs, "submit", lambda module, params: seen.append(module) or "stubjob")
        c = d.app.test_client()
        r = c.post("/run/module_sec_13f-map", headers={"X-RC-Panel": "cli"})
        assert r.status_code == 202 and seen == ["sec13f_map"]
        assert c.post("/run/module_sec_13f-map").status_code == 403       # #231: a write needs the panel header
