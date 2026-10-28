from services.v7_scan import (assemble_flow, band, dimension_averages, merge_repeated_sections,
                              pair_pointer, select_worst_pairs, weighted_score)


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


def test_flow_score_dimensions_and_worst_eight():
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
    assert [p["order"] for p in select_worst_pairs(flow)] == list(range(2, 10))
    assert weighted_score(flow) == 50.0
    assert dimension_averages(flow)["claim_support"] == 0.5
    assert pair_pointer(flow[1])["score"] == 10.0
    assert band(85) == "Strong" and band(70) == "Solid" and band(69.99) == "Needs Revision"
    assert weighted_score(assemble_flow(sections[:2], [{"skipped": "too short"}])) == 0.0
