from services.parser import ManuscriptParserService

BODY = "This part explains the idea in plain words so a reader can follow the study from start to finish without confusion. " * 3

FRONT = "\n".join([
    "ACKNOWLEDGEMENT", "We thank our families for their patience and support throughout this long study of ours.",
    "TABLE OF CONTENTS",
    "ACKNOWLEDGEMENT | 2", "TABLE OF CONTENTS | 3", "LIST OF TABLES | 4", "CHAPTER I INTRODUCTION",
    "Rationale of the Study | 6", "Objectives of the Study | 7", "Scope and Limitations of the Study | 8",
    "LIST OF TABLES", "Table No. | Table Name | Page No.", "1 | Comparative Matrix | 20", "2 | Agile Sprint Plan | 33",
    "3 | Business Model Canvas | 41", "4 | Validation Board | 54", "5 | Gantt Chart | 57",
])


def sections(text):
    parsed = ManuscriptParserService.parse_manuscript_sections(text, None)
    return {name.lower(): body for name, body in parsed["parsed_sections"].items()}


def test_scanning_starts_after_the_table_of_contents_and_lists():
    found = sections(f"{FRONT}\nRationale of the Study\n{BODY}\nObjectives of the Study\n{BODY}")
    everything = " ".join(found.values())
    assert "thank our families" not in everything
    assert "Comparative Matrix" not in everything and "Page No." not in everything
    assert "rationale of the study" in found and "objectives of the study" in found


def test_text_inside_a_figure_is_not_part_of_the_section():
    figure = "\n".join(["Agile Methodology", "● Planning", "● Requirement", "Analysis", "● Designing", "● Testing", "Figure 1: Flow of the Study"])
    found = sections(f"Rationale of the Study\n{BODY}\n{figure}\n{BODY}")
    everything = " ".join(found.values())
    assert "Designing" not in everything and "Figure 1" not in everything


def test_abstract_before_contents_and_lists_is_kept_without_rows():
    rows = ["ABSTRACT | i", "LIST OF TABLESv", "Topic\t2", "Another topic .... 3",
            "Entry | iv", "More\t5", "LIST OF TABLES", "Table name | 6",
            "LIST OF FIGURES", "53\tProject Road Map\t186"]
    text = "\n".join(["Title page", "ABSTRACT", BODY, "TABLE OF CONTENTS", *rows,
                       "CHAPTER I", "INTRODUCTION", BODY])
    parsed = ManuscriptParserService.parse_manuscript_sections(text, None)
    assert parsed["section_roles"][parsed["section_order"][0]] == "abstract"
    assert BODY.strip() in parsed["parsed_sections"][parsed["section_order"][0]]
    assert not any(row in body for row in rows + ["CHAPTER I"] for body in parsed["parsed_sections"].values())
    assert "introduction" in parsed["section_roles"].values()


def test_the_reference_list_of_a_paper_with_numbered_headings_is_not_part_of_its_conclusion():
    body = "This part explains the idea in plain words so a reader can follow the study from start to finish without confusion. " * 3
    text = "\n".join(["Title of the study", "1. Introduction", body, "2. Methodology", body, "3. Results", body, "4. Conclusion", body,
               "References", "Cohen, J. (1988). Statistical power analysis. Lawrence Erlbaum.", "Vygotsky, L. S. (1978). Mind in society. Harvard University Press."])
    parsed = ManuscriptParserService.parse_manuscript_sections(text, None)
    roles = parsed["section_roles"]
    assert "references" in roles.values()
    reference_key = next(key for key, role in roles.items() if role == "references")
    assert "Vygotsky" in parsed["parsed_sections"][reference_key]
    assert all("Vygotsky" not in body for key, body in parsed["parsed_sections"].items() if key != reference_key)
