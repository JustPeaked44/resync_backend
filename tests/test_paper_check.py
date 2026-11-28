import pytest

from services.paper_check import check_research_paper
from services.parser import ManuscriptParserService

SECTION = "This study examines how students manage their time. " * 40


def _check(text):
    return check_research_paper(text, ManuscriptParserService.parse_manuscript_sections(text, None))


def test_a_structured_paper_is_accepted():
    text = "\n".join(f"{h}\n{SECTION}" for h in ("Introduction", "Related Literature", "Methodology", "Results", "Conclusion"))
    assert _check(text)["ok"]


@pytest.mark.parametrize("text", [
    " ".join(["Lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor."] * 120),
    "\n".join(["Ingredients", "Two cups of flour and sugar. " * 40, "Directions", "Mix the batter and bake it in the oven. " * 40]),
    "Dear Sir,\n" + "I am writing to request a leave of absence for next week because of family matters. " * 60,
])
def test_filler_and_everyday_documents_are_refused(text):
    result = _check(text)
    assert not result["ok"] and result["reason"] == "no_paper_structure"


def _sections(*headings, body=SECTION):
    return "\n".join(f"{heading}\n{body}" for heading in headings)


def test_a_study_guide_with_research_sounding_headings_is_refused():
    text = _sections("System Architecture", "Findings, Evidence and Verification Rules",
                     "What Makes a Recommendation Credible", "Honest Limitations")
    result = _check(text)
    assert not result["ok"] and result["reason"] == "no_paper_structure"


def test_a_proposal_with_its_first_three_chapters_is_accepted():
    assert _check(_sections("Introduction", "Related Literature", "Methodology"))["ok"]


def test_a_questionnaire_is_refused():
    questions = "How often do you study the data from your research?\n" * 15
    result = _check(_sections("Objectives", "Methodology", "Results", "Conclusion", body=SECTION + "\n" + questions))
    assert not result["ok"] and result["reason"] == "questionnaire"


TEMPLATE = ["Rationale of the Study", "Objectives of the Study", "Scope and Limitations", "Related Literature",
            "Research Design", "References"]


def test_a_manuscript_written_to_the_template_matches_however_its_headings_are_numbered():
    from services.paper_check import template_matches, template_problem
    text = _sections("CHAPTER I", "1.1 RATIONALE OF THE STUDY", "Objective of Study", "Scope and Limitation",
                     "2.1 Related Literature")
    assert template_matches(text, TEMPLATE) == (4, 5)      # "References" is not counted
    assert template_problem(text, TEMPLATE) is None


def test_a_manuscript_written_to_another_template_is_refused():
    from services.paper_check import template_problem
    problem = template_problem(_sections("Abstract", "Introduction", "Methods", "Related Literature", "Conclusion"), TEMPLATE)
    assert problem.startswith("Could not read this document because it does not match the uploaded template: only 1 of")


def test_headings_only_mentioned_inside_sentences_do_not_count():
    from services.paper_check import template_matches
    text = "The rationale of the study and its research design are described by the writer in a long sentence. " * 30
    assert template_matches(text, TEMPLATE)[0] == 0


def test_no_template_or_a_very_short_one_refuses_nothing():
    from services.paper_check import template_problem
    assert template_problem(SECTION, None) is None
    assert template_problem(SECTION, ["Introduction", "References"]) is None


def test_a_tiny_document_is_refused():
    assert _check("Introduction\nHello.\nMethodology\nWorld.")["reason"] == "too_short"


def _docx(body: str) -> bytes:
    import io, zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f"<w:document><w:body>{body}</w:body></w:document>")
    return buf.getvalue()


PICTURE = "<w:p><w:r><w:drawing/></w:r></w:p>"
TWO_COLUMNS = '<w:sectPr><w:cols w:num="2" w:space="720"/></w:sectPr>'
ENGLISH = ("This study examines how the students of the university manage their time and what this means for their "
           "grades in the courses that they take during the year. ") * 20
SPANISH = ("La presente investigación tiene como objetivo determinar la incidencia de la propiedad, planta y equipo en "
           "los resultados económicos de las empresas de la región durante el año. ") * 20
BULLETS = ("Skills Python Java SQL network support hardware repair customer service teamwork leadership. "
           "Experience technician intern at the campus help desk. ") * 15
POLISH = "Celem niniejszej pracy jest analiza wyników finansowych przedsiębiorstw działających w regionie. " * 20
RUSSIAN = "Настоящее исследование посвящено анализу финансовых результатов предприятий региона. " * 20
BECAUSE = "Could not read this document because "


@pytest.mark.parametrize("found, reason", [
    ({"language": True}, "this language is unsupported."),
    ({"two_columns": True}, "it contains a two-column structure."),
    ({"scanned": True}, "it is a scanned file."),
    ({"handwritten": True}, "it is a handwritten file."),
    ({"language": True, "two_columns": True}, "it contains an unsupported language and a two-column structure."),
    ({"scanned": True, "language": True}, "it is a scanned file and contains an unsupported language."),
    ({"scanned": True, "two_columns": True}, "it is a scanned file and contains a two-column structure."),
    ({"handwritten": True, "language": True}, "it is a handwritten file and contains an unsupported language."),
    ({"handwritten": True, "two_columns": True}, "it is a handwritten file and contains a two-column structure."),
    ({"scanned": True, "handwritten": True}, "it is a scanned and handwritten file."),
    ({"scanned": True, "handwritten": True, "language": True, "two_columns": True},
     "it is a scanned and handwritten file and contains an unsupported language and a two-column structure."),
])
def test_every_combination_has_its_own_sentence(found, reason):
    from services.paper_check import unreadable_message
    assert unreadable_message(**found) == BECAUSE + reason


def test_no_reason_means_no_message():
    from services.paper_check import unreadable_message
    assert unreadable_message() is None


@pytest.mark.parametrize("text, unsupported", [(ENGLISH, False), (SPANISH, True), (RUSSIAN, True), (POLISH, True),
                                               (BULLETS, False), ("Hola mundo.", False)])
def test_only_writing_that_is_clearly_not_english_is_unsupported(text, unsupported):
    from services.paper_check import unsupported_language
    assert unsupported_language(text) is unsupported


def _problem(body, text, paper_ok, seen=None, monkeypatch=None):
    import asyncio
    from services import page_check
    from services.paper_check import document_problem
    if monkeypatch:
        async def read(_data):
            return seen
        monkeypatch.setattr(page_check, "read_pictures", read)
    return asyncio.run(document_problem(_docx(body), text, paper_ok))


def test_a_spanish_two_column_paper_names_both():
    assert _problem(TWO_COLUMNS, SPANISH, False) == BECAUSE + "it contains an unsupported language and a two-column structure."


def test_another_language_is_refused_even_when_the_structure_passes():
    assert _problem("<w:p/>", SPANISH, True) == BECAUSE + "this language is unsupported."


def test_two_columns_alone_are_refused_only_when_the_paper_check_failed():
    assert _problem(TWO_COLUMNS, ENGLISH, False) == BECAUSE + "it contains a two-column structure."
    assert _problem(TWO_COLUMNS, ENGLISH, True) is None


def test_a_plain_english_docx_has_no_problem():
    assert _problem('<w:p/><w:sectPr><w:cols w:space="720"/></w:sectPr>', ENGLISH, False) is None
    assert _problem(PICTURE, ENGLISH, True) is None


@pytest.mark.parametrize("seen, reason", [
    (None, "it is a scanned file."),                                    # the pictures could not be looked at
    ({"scanned_print": True}, "it is a scanned file."),
    ({"handwritten": True}, "it is a handwritten file."),
    ({"handwritten": True, "non_english": True, "two_columns": True},
     "it is a handwritten file and contains an unsupported language and a two-column structure."),
])
def test_a_picture_document_says_what_the_pictures_show(monkeypatch, seen, reason):
    assert _problem(PICTURE, "", False, seen, monkeypatch) == BECAUSE + reason
