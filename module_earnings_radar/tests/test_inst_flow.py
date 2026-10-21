from RevenueChaser.core.config import INST_FLOW_MIN_NET
from RevenueChaser.module_earnings_radar.pipeline import (
    _flow_window, _institutional_flow_comment,
)


class TestInstitutionalFlowComment:
    def test_empty_is_no_flow(self):
        label, net, tone = _institutional_flow_comment({})
        assert label == "No institutional flow"
        assert net == 0.0
        assert tone == "neutral"

    def test_small_net_is_thin_not_buying(self):
        label, net, tone = _institutional_flow_comment({
            "inst_call": 8000, "inst_put": 2000,
        })
        assert abs(net) < INST_FLOW_MIN_NET
        assert label == "Thin flow"
        assert tone == "neutral"
        assert net == 6000

    def test_small_one_sided_does_not_get_sweep_label(self):
        label, _, tone = _institutional_flow_comment({
            "inst_call": 8000, "inst_put": 0, "sweep": 8000,
        })
        assert label == "Thin flow"
        assert tone == "neutral"
        assert "sweep" not in label.lower()

    def test_large_call_imbalance_is_buying(self):
        label, net, tone = _institutional_flow_comment({
            "inst_call": 80_000, "inst_put": 10_000,
        })
        assert abs(net) >= INST_FLOW_MIN_NET
        assert label == "Institutional buying"
        assert tone == "bull"
        assert net == 70_000

    def test_large_put_imbalance_is_selling(self):
        label, net, tone = _institutional_flow_comment({
            "inst_call": 10_000, "inst_put": 80_000,
        })
        assert label == "Institutional selling"
        assert tone == "bear"
        assert net == -70_000

    def test_large_balanced_is_mixed(self):
        label, _, tone = _institutional_flow_comment({
            "inst_call": 200_000, "inst_put": 170_000,
        })
        assert label == "Mixed institutional flow"
        assert tone == "neutral"

    def test_sweep_appends_only_when_sized(self):
        label, _, _ = _institutional_flow_comment({
            "inst_call": 80_000, "inst_put": 10_000, "sweep": 30_000,
        })
        assert label == "Inst. buying sweep"


class TestFlowWindow:
    def test_empty(self):
        assert _flow_window([]) == (0, None)

    def test_single_print_has_no_span(self):
        n, span = _flow_window([{"executed_at": "2026-08-13T19:00:00Z"}])
        assert n == 1
        assert span is None

    def test_span_hours(self):
        n, span = _flow_window([
            {"executed_at": "2026-08-13T18:00:00Z"},
            {"executed_at": "2026-08-13T19:30:00Z"},
        ])
        assert n == 2
        assert span == 1.5

    def test_bad_timestamps_ignored(self):
        n, span = _flow_window([
            {"executed_at": "nope"},
            {"executed_at": "2026-08-13T18:00:00Z"},
        ])
        assert n == 2
        assert span is None

