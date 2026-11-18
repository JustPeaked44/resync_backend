from services.v7_scan import assemble_flow, flatten_explanations, is_prose, senseless_recommendation

SCORES = {"pair_weight": 1, "overall_coherence": 0.3, "objectives_alignment": 0.3, "methodology_fit": 0.3,
          "numeric_consistency": 0.3, "claim_support": 0.3}


def flow_with(severity, recommendations, bridges=()):
    sections = [{"name": "A", "text": "A says the sample had 40 students in total."}, {"name": "B", "text": "B says the sample had 90 students."}]
    flow = assemble_flow(sections, [{"scores": dict(SCORES)}])
    flow[0]["severity"] = severity
    flow[0]["explanation"] = {"severity": severity, "highlighted_inconsistencies": [], "reference_list": [],
                              "recommendations": list(recommendations), "bridges": list(bridges)}
    return sections, flow


EDIT = {"section": "B", "original_sentence": "B says the sample had 90 students.", "recommended_revision": "B says the sample had 40 students.",
        "issue": "The sample size differs.", "kind": "edit"}
BRIDGE = {"section": "A", "sentence": "Linking sentence.", "placement": "end"}


def test_serious_inconsistencies_get_recommendations():
    for severity in ("Critical", "Moderate"):
        sections, flow = flow_with(severity, [EDIT])
        assert len(flatten_explanations(flow, sections)[1]) == 1


def test_minor_differences_and_loose_links_get_no_recommendations():
    for severity in ("Minor", "None", None):
        sections, flow = flow_with(severity, [EDIT], [BRIDGE])
        assert flatten_explanations(flow, sections)[1] == []


def test_recommendations_about_citations_tables_and_captions_are_dropped():
    for sentence in ("Khan, S. (2020). Learning online. https://doi.org/10.1000/xyz", "Name | Age | Score", "Table 3: Data Dictionary - User",
                     "● Planning", "Smith et al. (2019) found that learning improved."):
        assert senseless_recommendation({"original_sentence": sentence, "issue": ""}), sentence
    assert senseless_recommendation({"original_sentence": "", "issue": "Add a table that summarises the results."})
    assert not senseless_recommendation(EDIT)


def test_table_and_diagram_sections_are_not_prose():
    assert not is_prose("Features | SciTura | Khan\nPrice | Free | Paid\nTopics | Science | Math")
    assert not is_prose("\n".join(f"● Step {i}" for i in range(12)))
    assert is_prose("This study explains why practice matters. It follows learners over one semester.\nResults show gains.")


def test_a_clashing_pair_gets_one_fix_on_one_side_with_its_reason():
    other = dict(EDIT, section="A", original_sentence="A says the sample had 40 students in total.", recommended_revision="A says 90 students.")
    sections, flow = flow_with("Critical", [other, EDIT])
    recs = flatten_explanations(flow, sections)[1]
    assert [r["section"] for r in recs] == ["B"] and recs[0]["issue"]


def test_a_recommendation_without_a_reason_is_not_shown():
    sections, flow = flow_with("Critical", [dict(EDIT, issue="")])
    assert flatten_explanations(flow, sections)[1] == []


def test_the_same_finding_quoted_from_either_side_is_listed_once():
    sections, flow = flow_with("Critical", [])
    a, b = "A says the sample had 40 students in total.", "B says the sample had 90 students."
    flow[0]["explanation"]["highlighted_inconsistencies"] = [
        {"section": "A", "quote": a, "what": "Sample differs", "why": "40 against 90."},
        {"section": "B", "quote": b, "what": "Sample differs", "why": "90 against 40."}]
    flow[0]["explanation"]["reference_list"] = [{"section": "B", "sentence": b}, {"section": "A", "sentence": a}]
    findings = flatten_explanations(flow, sections)[0]
    assert len(findings) == 1


def test_front_matter_lists_and_back_matter_are_not_compared():
    from services.v7_scan import is_argument_section
    for name in ("Acknowledgement", "ACKNOWLEDGEMENTS", "Dedication", "Table of Contents", "List of Tables", "List of Figures", "Appendix B - Letter",
                 "Appendices", "Curriculum Vitae", "Approval Sheet", "Certificate of Originality", "Team Composition", "Glossary", "Chapter II: Abbreviations"):
        assert not is_argument_section(name), name
    for name in ("Introduction", "Review of Related Literature", "Methodology", "Results", "Conclusion", "Definition of Terms", "Scope and Limitations of the Study"):
        assert is_argument_section(name), name


def test_sections_about_different_subjects_are_a_critical_mismatch_even_when_nothing_contradicts():
    import asyncio
    from services import v7_explain
    from services.v7_scan import apply_subject_check

    sections = [{"name": "A", "text": "Coastal erosion is a problem for island nations."}, {"name": "B", "text": "Heart surgery grafts differ in patency."}]
    flow = assemble_flow(sections, [{"scores": dict(SCORES, overall_coherence=0.1)}])
    flow[0]["explanation"] = {"severity": "None", "failed_dimension": "none", "highlighted_inconsistencies": [], "reference_list": [], "recommendations": [], "bridges": []}

    async def different(items):
        return {1: {"subject_a": "coastal erosion", "subject_b": "heart surgery", "quote_a": sections[0]["text"], "quote_b": sections[1]["text"]}}

    import pytest
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(v7_explain, "check_subjects_batch", different)
        asyncio.run(apply_subject_check(flow, sections))
    explanation = flow[0]["explanation"]
    assert explanation["severity"] == "Critical" and "do not belong to the same study" in explanation["highlighted_inconsistencies"][0]["why"]
    findings, recommendations = flatten_explanations(flow, sections)
    assert len(findings) == 1 and findings[0]["evidence_verified"] and recommendations == []


def test_the_subject_check_only_accepts_quotes_that_are_in_the_text(monkeypatch):
    import asyncio
    from services import v7_explain

    async def answer(prompt, max_tokens=None):
        return {"pairs": [{"pair_id": 1, "same_subject": False, "subject_a": "x", "subject_b": "y", "quote_a": "invented", "quote_b": "Heart surgery."},
                          {"pair_id": 2, "same_subject": True}]}

    monkeypatch.setattr(v7_explain, "_generate", answer)
    items = [{"id": 1, "role_a": "A", "text_a": "Coastal erosion.", "role_b": "B", "text_b": "Heart surgery."},
             {"id": 2, "role_a": "A", "text_a": "Same.", "role_b": "B", "text_b": "Same."}]
    assert asyncio.run(v7_explain.check_subjects_batch(items)) == {}
