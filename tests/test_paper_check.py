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


def _docx(body: str) -> bytes:
    import io, zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f"<w:document><w:body>{body}</w:body></w:document>")
    return buf.getvalue()


def test_a_scanned_docx_is_named_as_a_scan():
    from services.paper_check import SCANNED_MESSAGE, docx_layout_problem
    assert docx_layout_problem(_docx("<w:p><w:r><w:drawing/></w:r></w:p>"), 5) == SCANNED_MESSAGE


def test_a_two_column_docx_is_named_as_two_columns():
    from services.paper_check import TWO_COLUMN_MESSAGE, docx_layout_problem
    assert docx_layout_problem(_docx('<w:sectPr><w:cols w:num="2" w:space="720"/></w:sectPr>'), 900) == TWO_COLUMN_MESSAGE


def test_a_plain_docx_has_no_layout_problem():
    from services.paper_check import docx_layout_problem
    assert docx_layout_problem(_docx('<w:p/><w:sectPr><w:cols w:space="720"/></w:sectPr>'), 900) is None
