from services.parser import ManuscriptParserService
from services.v7_scan import merge_repeated_sections
from main import _references_text
from services.reference_parser import build_parsed_entries


def test_scan_and_go_keeps_doc2_list_and_toc_text_with_previous_section():
    text = """Introduction
The study describes manuscript analysis and its purpose.
REFERENCES        114
Specifically, this study pursues the following objectives:
* Designing
* Review
☐  Objectives and Methodology
Goals: Establish the shared Git repository
int
References the user who owns the manuscript
Research Methodology
The method evaluates related manuscript sections.
References
Author, A. (2024). A paper. https://doi.org/10.1234/example
"""
    result = ManuscriptParserService.parse_manuscript_sections(text, None)
    sections = merge_repeated_sections(result["parsed_sections"], result["section_order"])
    assert [section["name"] for section in sections] == [
        "introduction", "research methodology", "references"
    ]
    for fragment in ("REFERENCES        114", "Specifically,", "* Designing", "* Review",
                     "☐  Objectives and Methodology",
                     "Goals: Establish", "int", "References the user"):
        assert fragment in sections[0]["text"]
def test_doc2_table_column_labels_are_body_text():
    text = """Database Design
The database stores the manuscript scan results.
Table 11.1
RECOMMENDATION
Table Name
Attribute Name
FK Referenced Table
recommendation
recommendation_id
Test Case Description
Expected Result
Actual Result
Remarks
Pre-condition
Actual Result
Remarks
Critical
Test Results
Number
References
Author, A. (2024). Source. https://doi.org/10.1234/source
"""
    parsed = ManuscriptParserService.parse_manuscript_sections(text, None)
    assert list(parsed["parsed_sections"]) == ["database design", "references"]
    assert "Expected Result\nActual Result" in parsed["parsed_sections"]["database design"]
def test_longest_literal_references_block_beats_toc_and_data_dictionary():
    text = """Introduction
The manuscript describes a comparison of adjacent sections.
REFERENCES        114
References the user who owns the manuscript
VARCHAR(36) FK user
References
Author, A. (2024). A short source. https://doi.org/10.1234/short
Author, B. (2023). Another source. https://doi.org/10.1234/other
References
Author, A. (2024). A short source. https://doi.org/10.1234/short
Author, B. (2023). Another source. https://doi.org/10.1234/other
Author, C. (2022). A third source. https://doi.org/10.1234/third
"""
    parsed = ManuscriptParserService.parse_manuscript_sections(text, None)
    selected = _references_text(parsed["parsed_sections"], parsed["section_roles"])
    assert "Author, C." in selected
    assert len(build_parsed_entries(selected)[0]) == 3
    assert _references_text({"references the user": "wrong", "references": "real"},
                            {"references the user": "references", "references": "references"}) == "real"
