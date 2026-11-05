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


def test_a_tiny_document_is_refused():
    assert _check("Introduction\nHello.\nMethodology\nWorld.")["reason"] == "too_short"
