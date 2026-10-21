"""#199 E-4: decided cleanup (E20 dead code, E21 docs/strings, E26 testWithAstra, dead UW 403 path)."""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


class TestTestWithAstraGone:
    def test_directory_is_deleted(self):
        assert not (ROOT / "module_earnings_radar" / "testWithAstra").exists()

    @pytest.mark.parametrize("doc", ["docs/rules/redesign.md"])
    def test_docs_do_not_point_into_it(self, doc):
        assert "testWithAstra" not in (ROOT / doc).read_text(encoding="utf-8")


class TestDeadCodeGone:
    def test_nrc_score_all(self):
        from RevenueChaser.module_earnings_radar.rules import nrc
        assert not hasattr(nrc, "score_all")

    def test_cluster_metrics(self):
        from RevenueChaser.module_earnings_radar import classifier
        assert not hasattr(classifier, "get_cluster_metrics")

    def test_rule_result_weight_value(self):
        from RevenueChaser.core.models.events import RuleResult
        assert not hasattr(RuleResult, "weight_value")

    def test_nrc_latest_constant(self):
        from RevenueChaser.app import runners
        assert not hasattr(runners, "NRC_LATEST")

    def test_rotation_score_is_not_assigned_in_the_pipeline(self):
        src = (ROOT / "module_earnings_radar" / "pipeline.py").read_text(encoding="utf-8")
        assert "rotation_score" not in src


class TestDeadUwRevenuePathGone:
    def test_config_flag(self):
        from RevenueChaser.core import config
        assert not hasattr(config, "REVENUE_EST_ENABLED")

    def test_provider_method(self):
        from RevenueChaser.core.providers.unusual_whales import UWClient
        assert not hasattr(UWClient, "earnings_estimates")

    def test_pipeline_helper(self):
        from RevenueChaser.module_earnings_radar.pipeline import EarningsPipeline
        assert not hasattr(EarningsPipeline, "_revenue_est_map")


class TestMethodologyStringsFollowConfig:
    def _html(self, tmp_path):
        from RevenueChaser.module_earnings_radar.report import export_html
        stats = {"total": 3, "passed": 2, "bypassed": 0, "filtered": 1, "by_reason": {"OI": 1}, "excluded": []}
        return export_html([], [], {}, filename=str(tmp_path / "e.html"), filter_stats=stats).read_text(encoding="utf-8")

    def test_option_volume_gate_is_the_configured_contract_count(self, tmp_path):
        from RevenueChaser.core.config import MIN_OPTION_VOLUME
        html = self._html(tmp_path)
        assert f"OptVol≥{MIN_OPTION_VOLUME:,} contracts" in html
        assert f"≥ {MIN_OPTION_VOLUME:,} contracts" in html
        assert "OptVol≥$100" not in html and "≥ $100" not in html

    def test_other_gates_follow_config(self, tmp_path):
        from RevenueChaser.core.config import MIN_TOTAL_OI, MIN_DOLLAR_OPTION_VOLUME
        html = self._html(tmp_path)
        assert f"OI≥{MIN_TOTAL_OI:,}" in html
        assert f"$Vol≥${MIN_DOLLAR_OPTION_VOLUME // 1000}K" in html

    def test_etf_count_is_the_rotation_universe(self, tmp_path):
        from RevenueChaser.module_sector_rotation.universe import ROTATION_ETFS
        html = self._html(tmp_path)
        assert f"across all {len(ROTATION_ETFS)} ETFs" in html
        assert "all 46 ETFs" not in html
