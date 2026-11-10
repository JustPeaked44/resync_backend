from services.parser import ManuscriptParserService

BODY = "This part explains the idea in plain words so a reader can follow the study from start to finish without confusion. " * 3


def parse(headings):
    text = "\n".join(f"{heading}\n{BODY}" for heading in headings)
    parsed = ManuscriptParserService.parse_manuscript_sections(text, None)
    return parsed, parsed.get("section_order", list(parsed["parsed_sections"]))


def test_the_short_sections_of_a_thesis_first_chapter_are_sections_of_their_own():
    _, order = parse(["Rationale of the Study", "Objectives of the Study", "Scope and Limitations of the Study",
                      "Significance of the Study", "Definition of Terms"])
    names = [name.lower() for name in order]
    for expected in ("scope and limitations of the study", "significance of the study", "definition of terms"):
        assert expected in names, expected


def test_text_under_definition_of_terms_is_not_credited_to_scope_and_limitations():
    parsed, order = parse(["Scope and Limitations of the Study", "Definition of Terms"])
    sections = {name.lower(): text for name, text in parsed["parsed_sections"].items()}
    assert "definition of terms" in sections and sections["definition of terms"].strip()
    assert "definition of terms" not in sections["scope and limitations of the study"].lower()
