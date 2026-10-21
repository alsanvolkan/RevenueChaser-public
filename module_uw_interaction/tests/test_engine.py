"""Tests for the UW Interaction engine — validation, coercion, row normalization.

The engine never touches the network: a fake client records the endpoint called
and returns canned bodies, so the tests exercise the catalog-driven logic only.
"""
import pytest

from RevenueChaser.module_uw_interaction.engine import (
    ToolError, _coerce, _columns_from, _extract, run_tool,
)


class FakeUW:
    """Minimal UWClient stand-in: records raw_endpoint calls, serves canned bodies."""

    def __init__(self, body=None):
        self.body = body
        self.calls = []
        self.auth_error = None

    def raw_endpoint(self, ep, params=None):
        self.calls.append({"ep": ep, "params": params})
        return self.body


class TestCoerce:
    def test_blank_is_none(self):
        assert _coerce("", "text") is None
        assert _coerce(None, "int") is None

    def test_int_float(self):
        assert _coerce("42", "int") == 42
        assert _coerce("4.2", "float") == 4.2

    def test_int_rejects_alpha(self):
        with pytest.raises(ToolError):
            _coerce("abc", "int")

    def test_bool_accepts_common(self):
        assert _coerce("true", "bool") is True
        assert _coerce("0", "bool") is False
        assert _coerce("yes", "bool") is True

    def test_bool_rejects_unknown(self):
        with pytest.raises(ToolError):
            _coerce("maybe", "bool")

    def test_text_passthrough(self):
        assert _coerce("AAPL", "text") == "AAPL"


class TestExtract:
    def test_data_list(self):
        rows, err = _extract({"data": [{"ticker": "A"}, {"ticker": "B"}]}, "data")
        assert len(rows) == 2 and err is None

    def test_data_object_wrapped(self):
        rows, err = _extract({"data": {"level": 5}}, "data")
        assert rows == [{"level": 5}] and err is None

    def test_bare_list(self):
        rows, _ = _extract([{"a": 1}], "list")
        assert rows == [{"a": 1}]

    def test_object_spec_from_data(self):
        rows, err = _extract({"data": {"flip": 10}}, "object")
        assert rows == [{"flip": 10}] and err is None

    def test_empty_data_is_no_error(self):
        rows, err = _extract({"data": []}, "data")
        assert rows == [] and err is None

    def test_non_dict_rows_dropped(self):
        rows, _ = _extract({"data": [{"a": 1}, "junk", None]}, "data")
        assert rows == [{"a": 1}]

    def test_unexpected_scalar_reports_error(self):
        rows, err = _extract(7, "data")
        assert rows == [] and err is not None


class TestColumnsFrom:
    def test_first_seen_order(self):
        cols = _columns_from([{"b": 1, "a": 2}, {"c": 3, "b": 4}])
        assert cols == ["b", "a", "c"]


class TestRunTool:
    def test_unknown_tool(self):
        with pytest.raises(ToolError):
            run_tool(FakeUW(), "no_such_tool", {})

    def test_required_param_missing(self):
        uw = FakeUW({"data": []})
        # crypto_pair_state.pair is required with no default.
        with pytest.raises(ToolError, match="required"):
            run_tool(uw, "crypto_pair_state", {})

    def test_invalid_int(self):
        uw = FakeUW({"data": []})
        with pytest.raises(ToolError, match="not a whole number"):
            run_tool(uw, "flow_alerts", {"limit": "abc"})

    def test_path_placeholder_filled_and_not_in_query(self):
        uw = FakeUW({"data": [{"ticker": "AAPL", "price": 200}]})
        res = run_tool(uw, "darkpool_ticker", {"ticker": "AAPL", "limit": "50"})
        assert uw.calls[0]["ep"] == "/darkpool/AAPL"
        assert uw.calls[0]["params"] == {"limit": 50}
        assert res.row_count == 1

    def test_bool_sent_as_string(self):
        uw = FakeUW({"data": []})
        run_tool(uw, "flow_alerts", {"is_sweep": "true"})
        assert uw.calls[0]["params"]["is_sweep"] == "true"

    def test_defaults_applied_when_blank(self):
        uw = FakeUW({"data": [{"x": 1}]})
        res = run_tool(uw, "darkpool_ticker", {"ticker": "AAPL"})
        # limit default 100 applied; no error
        assert res.params_used.get("limit") == 100

    def test_http_status_recorded(self):
        uw = FakeUW({"data": []})
        uw.auth_error = {"status": 403, "endpoint": "/x", "detail": "route_not_permitted"}
        res = run_tool(uw, "flow_alerts", {})
        assert res.http_status == 403


class TestPathAndSelectSafety:
    """M12 (ADR-X1, #144): a path value is one encoded segment; a select value is
    one of its options."""

    def test_path_value_is_percent_encoded(self):
        uw = FakeUW({"data": []})
        run_tool(uw, "darkpool_ticker", {"ticker": "../../congress/recent-trades?x=1#"})
        assert uw.calls[0]["ep"] == "/darkpool/..%2F..%2Fcongress%2Frecent-trades%3Fx%3D1%23"

    def test_a_space_in_a_path_value_is_encoded_once(self):
        uw = FakeUW({"data": []})
        run_tool(uw, "institution_holdings", {"name": "VANGUARD GROUP INC"})
        assert uw.calls[0]["ep"] == "/institution/VANGUARD%20GROUP%20INC/holdings"

    @pytest.mark.parametrize("val", [".", ".."])
    def test_a_dot_segment_is_refused(self, val):
        uw = FakeUW({"data": []})
        with pytest.raises(ToolError):
            run_tool(uw, "darkpool_ticker", {"ticker": val})
        assert uw.calls == []

    def test_unknown_select_value_is_refused(self):
        uw = FakeUW({"data": []})
        with pytest.raises(ToolError, match="categories"):
            run_tool(uw, "prediction_unusual", {"categories": "Crypto&admin=1"})
        assert uw.calls == []

    def test_known_select_value_is_sent(self):
        uw = FakeUW({"data": []})
        run_tool(uw, "prediction_unusual", {"categories": "Crypto"})
        assert uw.calls[0]["params"]["categories"] == "Crypto"


class TestEmptyObjectBody:
    """DF48: a failed call (``_get`` -> ``{}``) must not become one blank row."""

    def test_empty_dict_is_zero_rows_with_reason(self):
        rows, err = _extract({}, "object")
        assert rows == [] and err

    def test_empty_data_dict_is_zero_rows(self):
        rows, err = _extract({"data": {}}, "object")
        assert rows == []

    def test_non_empty_object_still_one_row(self):
        rows, err = _extract({"data": {"flip": 10}}, "object")
        assert rows == [{"flip": 10}] and err is None


class TestDF49ClearedFilter:
    """DF49: a cleared field is not the same as an absent one; only the absent one takes the default."""

    def test_absent_min_premium_takes_the_default(self):
        uw = FakeUW({"data": []})
        run_tool(uw, "flow_alerts", {})
        assert uw.calls[0]["params"]["min_premium"] == 10000.0

    def test_blank_min_premium_clears_the_filter(self):
        uw = FakeUW({"data": []})
        run_tool(uw, "flow_alerts", {"min_premium": ""})
        assert "min_premium" not in uw.calls[0]["params"]

    def test_a_cleared_required_param_keeps_its_catalog_default(self):
        """DF49: only an optional field can be cleared; a required one with a default still falls back to it."""
        uw = FakeUW({"data": []})
        run_tool(uw, "darkpool_ticker", {"ticker": ""})
        assert "AAPL" in uw.calls[0]["ep"]
