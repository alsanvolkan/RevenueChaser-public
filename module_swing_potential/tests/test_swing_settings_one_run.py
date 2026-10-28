"""One Swing run scores, shows and versions with one set of settings (Swing review finding 2, PR 6).

Before, every step resolved the Settings store on its own: the prefilter, the payload, the score rows, the
ledger's model version and the history header each read it again, and a Refresh rendered the last Fetch's payload
with the threshold text of *today's* store. A save after the Fetch (or during it) made the card say "Volume at
least 2.5x average: passed" for a name scored at 1.5x. Now a Fetch resolves the settings once, the payload carries
them, and the card text, the methodology table and the ledger version read the payload's.
"""
from __future__ import annotations

import json

from RevenueChaser.core import board_settings as bs
from RevenueChaser.module_swing_potential import config, freeze, pipeline, report
from RevenueChaser.module_swing_potential import settings as swing_settings
from RevenueChaser.module_swing_potential.tests.test_swing_settings_s4 import _rows

B = "module_swing_potential"


def _gate_spec(page: str) -> dict:
    start = page.index("var GATE_SPEC = ") + len("var GATE_SPEC = ")
    return json.JSONDecoder().raw_decode(page[start:])[0]


def test_the_payload_carries_the_settings_it_scored_with(rawstore_tmp_db):
    bs.save(B, {"VOL_CONFIRM": 1.8})
    payload = pipeline.build_payload(_rows(), top_n=10)
    assert payload["settings"]["vol_confirm"] == 1.8
    assert payload["settings"]["overrides"] == {"VOL_CONFIRM": 1.8}


def test_a_later_save_does_not_rewrite_the_last_fetchs_card_text(rawstore_tmp_db):
    payload = pipeline.build_payload(_rows(), top_n=10)                 # scored at the default 1.5x
    bs.save(B, {"VOL_CONFIRM": 2.5, "RS_MIN": 80})                      # saved after the Fetch
    page = report.render_swing_dashboard(payload)
    rule = _gate_spec(page)["rules"]["volume_confirmation"]
    assert rule["label"] == f"Volume at least {config.VOL_CONFIRM:g}× average"
    assert "Volume at least 2.5× average" not in page
    assert f"volume ≥{config.VOL_CONFIRM:g}× average" in page          # the methodology table too
    assert f"RS rating ≥ {config.RS_MIN:g}" in page


def test_the_ledger_version_is_the_runs_not_the_stores(rawstore_tmp_db):
    payload = pipeline.build_payload(_rows(), top_n=10)
    at_defaults = freeze.model_version(payload)
    bs.save(B, {"VOL_CONFIRM": 2.5})
    assert freeze.model_version(payload) == at_defaults
    assert freeze.model_version() != at_defaults                        # the store's version did move
    assert freeze.effective_config(payload)["VOL_CONFIRM"] == config.VOL_CONFIRM


def test_inside_a_fetch_every_step_sees_the_settings_resolved_at_its_start(rawstore_tmp_db):
    e = swing_settings.effective()
    with swing_settings.pinned(e):
        bs.save(B, {"VOL_CONFIRM": 2.5})                                # a save while the Fetch runs
        assert swing_settings.effective() == e
        assert pipeline.build_payload(_rows(), top_n=10)["passing"]["weinstein"] == 3
    assert swing_settings.effective().vol_confirm == 2.5                # the next run sees the save


def test_a_payload_from_before_this_change_still_renders_with_the_store(rawstore_tmp_db):
    payload = pipeline.build_payload(_rows(), top_n=10)
    payload.pop("settings")
    bs.save(B, {"VOL_CONFIRM": 2.5})
    assert _gate_spec(report.render_swing_dashboard(payload))["rules"]["volume_confirmation"]["label"] \
        == "Volume at least 2.5× average"


def test_a_fetch_pins_its_settings_for_the_whole_scan(rawstore_tmp_db, monkeypatch):
    seen = {}

    def scan(**_):
        bs.save(B, {"VOL_CONFIRM": 2.5})                                # a save while the scan runs
        seen["vol_confirm"] = swing_settings.effective().vol_confirm
        return {}
    monkeypatch.setattr(pipeline.screen_common, "run_screen", scan)
    pipeline.run_scan(no_fetch=True)
    assert seen["vol_confirm"] == config.VOL_CONFIRM
