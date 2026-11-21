"""The provider transcript's prepared / Q&A boundary (#1214).

Shaped on the measured answer: `transcript` is a list of turns with `speaker`, `title`, `content`, `sentiment`,
and the only mark of the boundary is the first analyst turn.
"""
from RevenueChaser.module_earnings_radar.rules import rule8_sentiment, transcript_split


def turn(title, content, speaker="Someone", sentiment=0.0):
    return {"speaker": speaker, "title": title, "content": content, "sentiment": sentiment}


MEASURED_ORDER = [
    turn("Vice President of Investor Relations", "Thank you and welcome to our call."),
    turn("CEO", "We delivered a strong quarter."),
    turn("CFO", "Revenue grew and margins held."),
    turn("Operator", "We will now open the line for questions."),
    turn("Analyst", "Can you unpack the guidance assumption?"),
    turn("CEO", "It assumes no change in pricing."),
]


def test_the_split_is_the_first_analyst_turn_not_the_operator():
    prepared, qa = transcript_split.split_at_first_analyst(MEASURED_ORDER)
    assert "We delivered a strong quarter." in prepared
    # The operator's handover belongs to the prepared half: the boundary is the analyst, not the invitation.
    assert "open the line for questions" in prepared
    assert "unpack the guidance assumption" in qa
    assert "We delivered a strong quarter." not in qa


def test_everything_from_the_first_analyst_onward_is_qa_including_management_answers():
    _, qa = transcript_split.split_at_first_analyst(MEASURED_ORDER)
    assert "It assumes no change in pricing." in qa


def test_a_transcript_with_no_analyst_turn_has_no_qa_half():
    turns = [t for t in MEASURED_ORDER if t["title"] != "Analyst"]
    prepared, qa = transcript_split.split_at_first_analyst(turns)
    assert prepared is not None
    # Inferred boundary, so the absent analyst must read as "no Q&A", never as "the tail is the Q&A".
    assert qa is None


def test_that_pair_leaves_r8_on_its_degraded_branch_and_never_fakes_a_tone_delta():
    turns = [t for t in MEASURED_ORDER if t["title"] != "Analyst"]
    prepared, qa = transcript_split.split_at_first_analyst(turns)
    result = rule8_sentiment.score(prepared_remarks=prepared, qa_section=qa)
    assert result.available is True
    assert "tone_delta" not in result.raw_metrics
    assert "qa_uncertainty" not in result.raw_metrics


def test_both_halves_present_reach_the_tone_delta_branch():
    prepared, qa = transcript_split.split_at_first_analyst(MEASURED_ORDER)
    result = rule8_sentiment.score(prepared_remarks=prepared, qa_section=qa)
    assert "tone_delta" in result.raw_metrics


def test_an_empty_half_is_none_rather_than_an_empty_string():
    turns = [turn("Analyst", "First words of the call are a question.")]
    prepared, qa = transcript_split.split_at_first_analyst(turns)
    assert prepared is None
    assert qa is not None


def test_a_turn_with_no_text_is_dropped_rather_than_joined_as_a_blank():
    turns = [turn("CEO", "Opening."), turn("CFO", "   "), turn("CFO", None), turn("Analyst", "Question?")]
    prepared, _ = transcript_split.split_at_first_analyst(turns)
    assert prepared == "Opening."


def test_no_transcript_at_all_leaves_both_halves_none_so_r8_is_unavailable():
    for empty in (None, [], [{"not": "a turn"}]):
        prepared, qa = transcript_split.split_at_first_analyst(empty)
        assert (prepared, qa) == (None, None)
    assert rule8_sentiment.score(prepared_remarks=None, qa_section=None).available is False


def test_an_analyst_title_is_matched_however_the_provider_spells_it():
    for title in ("Analyst", "analyst", " Analysts ", "Analyst - Some Bank"):
        assert transcript_split.is_analyst(title) is True
    for title in ("CEO", "Operator", "Vice President of Investor Relations", "", None):
        assert transcript_split.is_analyst(title) is False
