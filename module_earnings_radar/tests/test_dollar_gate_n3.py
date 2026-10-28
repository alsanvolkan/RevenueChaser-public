"""Fable review N3 (Earnings Radar): the $Vol gate was OptVol x 100, so with the default gates (500 contracts,
$50k) it was the OptVol gate a second time and the pre-score counted volume twice. It now reads the day's call plus
put premium the UW screener serves, the same rule as the options data's earnings set
(``rcdata/build/options/uw_screener.premium``). Offline."""
from RevenueChaser.core.config import MIN_DOLLAR_OPTION_VOLUME
from RevenueChaser.module_earnings_radar import pipeline
from RevenueChaser.module_earnings_radar.pipeline import EarningsPipeline
from RevenueChaser.rcdata.build.options import uw_screener

BUSY = {"call_volume": 4000, "put_volume": 1000, "total_open_interest": 9000}


def _outcome(row: dict):
    p = EarningsPipeline(["AAA"], uw=None, cal_meta={"AAA": {"expected_move_perc": 0.05}})
    p._screener_cache = {"AAA": row}
    p.preselect(watchlist=set())
    return p._filter_outcome["AAA"]


def test_a_busy_tape_of_cheap_options_fails_the_dollar_gate():
    assert _outcome({**BUSY, "call_premium": 30_000, "put_premium": 19_999}) == ("excluded", ["$Vol"])


def test_enough_premium_passes():
    assert _outcome({**BUSY, "call_premium": 30_000, "put_premium": 20_000}) == ("passed", [])


def test_no_premium_served_fails_the_dollar_gate_never_a_guess():
    assert _outcome(dict(BUSY)) == ("excluded", ["$Vol"])
    assert _outcome({**BUSY, "call_premium": 80_000}) == ("excluded", ["$Vol"])


def test_the_board_and_the_options_data_read_the_same_premium():
    for row in ({"call_premium": "222941665", "put_premium": "163537151"}, {"call_premium": 1}, {}):
        assert pipeline.option_premium(row) == uw_screener.premium(row)
    assert MIN_DOLLAR_OPTION_VOLUME == 50_000
