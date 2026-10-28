import asyncio
from services import v7_explain
from services.v7_scan import (assemble_flow, band, four_part_score, explain_flow, merge_repeated_sections,
                              pair_pointer, select_worst_pairs)


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


def test_explanation_cap_and_single_summary(monkeypatch):
    sections = [{"name": str(i), "text": "enough words for a valid section"} for i in range(14)]
    raw = [{"scores": {"pair_weight": 1, "overall_coherence": i / 10,
                       "objectives_alignment": i / 10, "methodology_fit": i / 10,
                       "numeric_consistency": i / 10, "claim_support": i / 10}} for i in range(13)]
    flow = assemble_flow(sections, raw)
    state = {"active": 0, "peak": 0, "summary_calls": 0}

    async def fake_pair(*_):
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        await asyncio.sleep(0.01)
        state["active"] -= 1
        return {"coherence_scores": {}, "highlighted_inconsistencies": [],
                "recommendations": [], "reference_list": []}

    async def fake_summary(*_):
        state["summary_calls"] += 1
        return None

    monkeypatch.setattr(v7_explain, "explain_pair", fake_pair)
    monkeypatch.setattr(v7_explain, "explain_summary", fake_summary)
    findings, recommendations, synthesis = asyncio.run(explain_flow(sections, flow))
    assert [p["order"] for p in flow if p["explanation"] is not None] == list(range(1, 13))
    assert flow[12]["explanation"] is None
    assert state["peak"] <= 4 and state["summary_calls"] == 1
    assert findings == recommendations == []
    assert synthesis["summary"] is None and synthesis["strongest_pair"]["order"] == 13
