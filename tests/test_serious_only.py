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
