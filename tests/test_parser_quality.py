from services.parser import ManuscriptParserService
from services.v7_scan import merge_repeated_sections


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
