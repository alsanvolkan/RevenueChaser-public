"""PR 2: Fundamentals' price and dollar-volume floors and its type filter are settings (defaults unchanged)."""
from __future__ import annotations

from RevenueChaser.core import board_settings
from RevenueChaser.module_fundamentals import config, freeze
from RevenueChaser.module_fundamentals import settings as fsettings


def test_effective_reads_the_config_defaults_with_no_override(rawstore_tmp_db):
    e = fsettings.effective()
    assert (e.min_price, e.min_dollar_volume, e.operating_only) == (config.MIN_PRICE, config.MIN_DOLLAR_VOLUME, True)
    assert e.prefilter_cfg().MIN_PRICE == config.MIN_PRICE
    assert e.override_values == ()


def test_a_saved_override_reaches_the_prefilter_and_the_ledger_model_version(rawstore_tmp_db):
    before = freeze.model_version()
    board_settings.save(fsettings.BOARD, {"MIN_PRICE": 1.0, "OPERATING_ONLY": 0})
    e = fsettings.effective()
    assert e.min_price == 1.0 and e.operating_only is False
    assert e.prefilter_cfg().MIN_PRICE == 1.0
    assert freeze.effective_config()["MIN_PRICE"] == 1.0
    assert freeze.effective_config()["OPERATING_ONLY"] == 0
    assert freeze.model_version() != before          # a changed threshold is a new model version
    board_settings.reset(fsettings.BOARD)
    assert freeze.model_version() == before
