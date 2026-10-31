import asyncio
from services import v7_explain, gemini
from services.v7_scan import (manuscript_score, assemble_flow, band, four_part_score, explain_flow, merge_repeated_sections,
                              pair_pointer, select_worst_pairs)
import pytest
from services import v7_scan

@pytest.fixture(autouse=True)
def identity_calibration(monkeypatch):
    """These tests check the scoring structure on raw values; the real curve is tested in test_v7_calibration."""
    monkeypatch.setattr(v7_scan, "CALIBRATION", ((0.0, 0.0), (1.0, 1.0)))



def test_merge_literal_repeats_in_first_occurrence_order():
    sections = {"Related Studies": "first", "Limitations": "one", "Related Studies (2)": "second",
                "Background": "other", "Limitations (3)": "", "Rationale": "distinct"}
    assert merge_repeated_sections(sections, list(sections)) == [
        {"name": "Related Studies", "text": "first\nsecond"},
        {"name": "Limitations", "text": "one"},
        {"name": "Background", "text": "other"},
        {"name": "Rationale", "text": "distinct"},
    ]
    assert merge_repeated_sections({"Studies (2)": "a", "Studies": "b"}, ["Studies (2)", "Studies"]) == [
        {"name": "Studies (2)", "text": "a\nb"}]


def test_flow_score_dimensions_and_worst_twelve():
    sections = [{"name": str(i), "text": "words"} for i in range(11)]
    raw = [{"scores": {"pair_weight": 1, "overall_coherence": i / 10,
                       "objectives_alignment": i / 10, "methodology_fit": i / 10,
                       "numeric_consistency": i / 10, "claim_support": i / 10}}
           for i in range(10)]
    raw[0] = {"skipped": "too short"}
    flow = assemble_flow(sections, raw)
    assert flow[0]["scores"]["overall_coherence"] is None
    assert flow[0]["pair_weight"] is None
    assert flow[0]["skipped_reason"] == "too short"
    assert [p["order"] for p in select_worst_pairs(flow)] == list(range(2, 11))
    assert four_part_score(flow)[0] == 50.0
    assert four_part_score(flow)[1]["claim_support"] == 0.5
    assert pair_pointer(flow[1])["score"] == 10.0
    assert band(85) == "Strong" and band(70) == "Solid" and band(69.99) == "Needs Revision"
    assert four_part_score(assemble_flow(sections[:2], [{"skipped": "too short"}]))[0] == 0.0


def test_worst_pair_selection_at_boundaries():
    sections = [{"name": str(i), "text": "enough words for a valid section"} for i in range(15)]
    raw = [{"scores": {"pair_weight": 1, "overall_coherence": i / 10,
                       "objectives_alignment": 0.5, "methodology_fit": 0.5,
                       "numeric_consistency": 0.5, "claim_support": 0.5}} for i in range(14)]
    for n in (0, 1, 8, 9, 12, 13):
        flow = assemble_flow(sections[:n + 1], raw[:n])
        assert len(select_worst_pairs(flow)) == min(n, 12)


def test_pair_score_drives_ranking_and_skipped_pair_has_no_score():
    sections = [{"name": str(i), "text": "The result was 78% for n = 120."} for i in range(4)]
    raw = [
        {"scores": {"pair_weight": 0.9, "overall_coherence": 0.1,
                    "objectives_alignment": 0.8, "methodology_fit": 0.8,
                    "numeric_consistency": 0.8, "claim_support": 0.8}},
        {"scores": {"pair_weight": 0.1, "overall_coherence": 0.9,
                    "objectives_alignment": 0.2, "methodology_fit": 0.2,
                    "numeric_consistency": 0.2, "claim_support": 0.2}},
        {"skipped": "too short"},
    ]
    flow = assemble_flow(sections, raw)
    assert [p["order"] for p in select_worst_pairs(flow)] == [2, 1]
    assert flow[0]["pair_score"] == 0.8 and flow[0]["numeric_applicable"] is True
    assert flow[2]["pair_score"] is None and flow[2]["numeric_applicable"] is False
    assert pair_pointer(flow[1]) == {"order": 2, "section_a": "1", "section_b": "2",
                                     "overall_coherence": 0.9, "score": 20.0}


def test_explanation_cap_and_single_summary(monkeypatch):
    sections = [{"name": str(i), "text": "enough words for a valid section"} for i in range(31)]
    raw = [{"scores": {"pair_weight": 1, "overall_coherence": i / 40,
                       "objectives_alignment": i / 40, "methodology_fit": i / 40,
                       "numeric_consistency": i / 40, "claim_support": i / 40}} for i in range(30)]
    flow = assemble_flow(sections, raw)
    state = {"active": 0, "peak": 0, "summary_calls": 0}

    async def fake_pair(*_, **__):
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        await asyncio.sleep(0.01)
        state["active"] -= 1
        return {"severity": "None", "failed_dimension": "none", "coherence_scores": {}, "highlighted_inconsistencies": [],
                "recommendations": [], "reference_list": []}

    async def fake_summary(*_):
        state["summary_calls"] += 1
        return None

    monkeypatch.setattr(v7_explain, "explain_pair", fake_pair)
    monkeypatch.setattr(v7_explain, "explain_summary", fake_summary)
    findings, recommendations, synthesis = asyncio.run(explain_flow(sections, flow))
    assert [p["order"] for p in flow if p["explanation"] is not None] == list(range(1, 16))
    assert flow[15]["explanation"] is None  # 30 weak pairs: the hard cap is 15
    assert 4 < state["peak"] <= 15 and state["summary_calls"] == 1  # all reviewed pairs run at the same time
    assert findings == []
    assert recommendations == []  # no generic advice is invented for pairs Gemini found nothing in
    assert synthesis["summary"] is None and synthesis["strongest_pair"]["order"] == 30


def test_unfinished_rate_limited_review_is_partial(monkeypatch):
    sections = [{"name": "A", "text": "enough words for a valid section"},
                {"name": "B", "text": "enough words for a valid section"}]
    flow = assemble_flow(sections, [{"scores": {"pair_weight": 1, "overall_coherence": 0.2,
        "objectives_alignment": 0.2, "methodology_fit": 0.2,
        "numeric_consistency": 0.2, "claim_support": 0.2}}])

    async def unavailable(*_, **__):
        return None

    monkeypatch.setattr(v7_explain, "explain_pair", unavailable)
    monkeypatch.setattr(v7_explain, "explain_summary", unavailable)
    monkeypatch.setattr(gemini, "rate_limited", lambda: True)
    _, _, synthesis = asyncio.run(explain_flow(sections, flow))
    assert synthesis["review_status"] == {
        "status": "partial", "unreviewed": 1, "reason": "rate_limited",
        "message": "Some AI reviews could not finish because the AI's temporary limit was reached. This score is provisional; try scanning again later."}


def pair_for_score(severity=None, dimension=None, numeric=True, order=1, overall=0.5):
    return {"order": order, "section_a": f"S{order}", "section_b": f"S{order + 1}", "severity": severity, "failed_dimension": dimension,
            "numeric_applicable": numeric, "scores": {key: overall for key in ("objectives_alignment", "methodology_fit", "numeric_consistency", "claim_support", "overall_coherence")}}


def test_each_dimension_starts_at_100_and_loses_points_per_real_issue_then_the_overall_is_the_plain_average():
    pairs = [pair_for_score("Critical", "methodology_fit", order=1), pair_for_score("Moderate", "methodology_fit", order=2),
             pair_for_score("Minor", "claim_support", order=3), pair_for_score("None", "none", order=4), pair_for_score(None, None, order=5)]
    score, breakdown = manuscript_score(pairs)
    assert {name: entry["score"] for name, entry in breakdown.items()} == {
        "context_alignment": 100, "operational_alignment": 85, "evidence_consistency": 100, "logical_support": 99}
    assert score == 96.0   # (100 + 85 + 100 + 99) / 4
    operational = breakdown["operational_alignment"]
    assert (operational["penalty"], operational["issues"], operational["pairs"]) == (15, 2, 5)
    assert [(d["severity"], d["points"]) for d in operational["deductions"]] == [("Critical", 10), ("Moderate", 5)]   # traceable
    assert manuscript_score([pair_for_score("Critical", "objectives_alignment", overall=0.1)] * 11)[1]["context_alignment"]["score"] == 0   # never below 0
    assert manuscript_score([])[0] == 0.0


def test_a_dimension_with_no_applicable_pair_is_na_and_left_out_of_the_average():
    pairs = [pair_for_score("Moderate", "claim_support", numeric=False, order=1), pair_for_score("None", "none", numeric=False, order=2)]
    score, breakdown = manuscript_score(pairs)
    assert breakdown["evidence_consistency"]["score"] == "N/A" and breakdown["evidence_consistency"]["deductions"] == []
    assert breakdown["logical_support"]["score"] == 95
    assert score == 98.33   # (100 + 100 + 95) / 3: numeric is left out
    mixed = [pair_for_score("Critical", "objectives_alignment", numeric=False, order=1),
             pair_for_score("Critical", "methodology_fit", numeric=False, order=2), pair_for_score("Critical", "methodology_fit", numeric=False, order=3)]
    assert manuscript_score(mixed)[0] == 90.0   # Context 90, Operational 80, Logical 100, Evidence N/A: (90 + 80 + 100) / 3


def test_a_false_alarm_is_free_and_the_ai_reviews_own_grade_is_the_penalty(monkeypatch):
    sections = [{"name": n, "text": "enough words for a valid section"} for n in "ABCDE"]
    overall = [0.2, 0.5, 0.8, 0.3]
    raw = [{"scores": {"pair_weight": 1, "overall_coherence": o, "objectives_alignment": o, "methodology_fit": o,
                       "numeric_consistency": o, "claim_support": o}} for o in overall]
    flow = assemble_flow(sections, raw)
    verdicts = {"A": "Moderate", "B": "Critical", "C": "Minor", "D": "None"}

    async def fake_pair(role_a, *_, **__):
        return {"severity": verdicts[role_a], "failed_dimension": "none" if verdicts[role_a] == "None" else "claim_support",
                "coherence_scores": {}, "highlighted_inconsistencies": [], "recommendations": [], "reference_list": [], "bridges": []}

    async def fake_summary(*_):
        return None

    monkeypatch.setattr(v7_explain, "explain_pair", fake_pair)
    monkeypatch.setattr(v7_explain, "explain_summary", fake_summary)
    asyncio.run(explain_flow(sections, flow))
    assert [p["severity"] for p in flow] == ["Moderate", "Critical", "Minor", "None"]   # the review's grade stands
    score, breakdown = manuscript_score(flow)
    assert breakdown["logical_support"]["score"] == 84 and breakdown["logical_support"]["issues"] == 3   # 100 - 5 - 10 - 1; false alarm free
    assert score == 94.67   # numeric is N/A for these texts: (100 + 100 + 84) / 3


def test_failed_reviews_get_a_second_one_at_a_time_pass(monkeypatch):
    sections = [{"name": n, "text": "enough words for a valid section"} for n in "ABC"]
    raw = [{"scores": {"pair_weight": 1, "overall_coherence": 0.5, "objectives_alignment": 0.5, "methodology_fit": 0.5,
                       "numeric_consistency": 0.5, "claim_support": 0.5}}] * 2
    flow = assemble_flow(sections, raw)
    calls = {"A": 0, "B": 0}

    async def flaky(role_a, *_, **__):
        calls[role_a] += 1
        failed = role_a == "A" and calls["A"] == 1     # pair A fails the first time only
        return {"severity": None if failed else "None", "failed_dimension": None if failed else "none",
                "coherence_scores": {}, "highlighted_inconsistencies": [], "recommendations": [], "reference_list": [], "bridges": []}

    async def fake_summary(*_):
        return None

    monkeypatch.setattr(v7_explain, "explain_pair", flaky)
    monkeypatch.setattr(v7_explain, "explain_summary", fake_summary)
    asyncio.run(explain_flow(sections, flow))
    assert calls == {"A": 2, "B": 1}                   # only the failed pair is retried
    assert [p["severity"] for p in flow] == ["None", "None"]


def test_a_pair_that_takes_too_long_is_left_unreviewed_without_holding_the_others(monkeypatch):
    sections = [{"name": n, "text": "enough words for a valid section"} for n in "ABC"]
    raw = [{"scores": {"pair_weight": 1, "overall_coherence": 0.5, "objectives_alignment": 0.5, "methodology_fit": 0.5,
                       "numeric_consistency": 0.5, "claim_support": 0.5}}] * 2
    flow = assemble_flow(sections, raw)

    async def slow_for_a(role_a, *_, **__):
        if role_a == "A":
            await asyncio.sleep(5)
        return {"severity": "None", "failed_dimension": "none", "coherence_scores": {}, "highlighted_inconsistencies": [],
                "recommendations": [], "reference_list": [], "bridges": []}

    async def fake_summary(*_):
        return None

    monkeypatch.setattr(v7_scan, "EXPLAIN_TIMEOUT", 0.05)
    monkeypatch.setattr(v7_explain, "explain_pair", slow_for_a)
    monkeypatch.setattr(v7_explain, "explain_summary", fake_summary)
    asyncio.run(explain_flow(sections, flow))
    assert [p["severity"] for p in flow] == [None, "None"]   # A unreviewed, B reviewed


def test_a_part_starts_from_how_well_transitions_connect_and_never_from_a_free_100():
    # identity calibration in this file, so a raw 0.65 is a 65
    transitions = {"objectives_alignment": 0.65, "methodology_fit": 0.65, "numeric_consistency": None, "claim_support": 0.82}
    issue = pair_for_score("Moderate", "methodology_fit", order=1)
    score, breakdown = manuscript_score([issue], transitions)
    assert {n: e["baseline"] for n, e in breakdown.items()} == {"context_alignment": 65, "operational_alignment": 65,
                                                                 "evidence_consistency": 100, "logical_support": 82}
    assert breakdown["operational_alignment"]["score"] == 60 and breakdown["operational_alignment"]["penalty"] == 5
    assert score == 76.75    # (65 + 60 + 100 + 82) / 4
    # the AI review could not run (nothing reviewed): the score is the transition baseline, not 100
    unreviewed = [pair_for_score(None, None, order=1)]
    assert manuscript_score(unreviewed, transitions)[0] == 78.0    # (65 + 65 + 100 + 82) / 4
    assert manuscript_score(unreviewed)[0] == 100.0                # without a baseline it is the plain penalty system
    # nothing to measure for figures: N/A and left out of the average
    no_figures = [pair_for_score(None, None, numeric=False, order=1)]
    score, breakdown = manuscript_score(no_figures, transitions)
    assert breakdown["evidence_consistency"]["score"] == "N/A"   # no figures in any pair or transition: nothing to measure
