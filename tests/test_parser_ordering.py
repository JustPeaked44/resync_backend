from pathlib import Path

from services.ingestion import DocumentIngestionService
from services.parser import ManuscriptParserService
from services.scoring import aggregate_role_texts


FIXTURE = Path(__file__).parent / "fixtures" / "google_docs_plaintext_ordering_export.txt"


def test_plaintext_export_keeps_repeated_roles_in_source_order():
    exported_text = DocumentIngestionService.strip_document_noise(
        FIXTURE.read_text(encoding="utf-8")
    )

    result = ManuscriptParserService.parse_manuscript_sections(exported_text, None)

    assert list(result["parsed_sections"]) == [
        "introduction",
        "methodology",
        "introduction (2)",
        "results",
    ]
    assert result["section_roles"] == {
        "introduction": "introduction",
        "methodology": "methodology",
        "introduction (2)": "introduction",
        "results": "results",
    }
    assert "INTRODUCTION_FIRST" in result["parsed_sections"]["introduction"]
    assert "INTRODUCTION_SECOND" in result["parsed_sections"]["introduction (2)"]
    assert result["section_order"] == list(result["parsed_sections"])

    role_texts = aggregate_role_texts(result["parsed_sections"], result["section_roles"])
    assert role_texts["introduction"].index("INTRODUCTION_FIRST") < role_texts["introduction"].index("INTRODUCTION_SECOND")


def test_template_sections_follow_source_order_before_missing_template_sections():
    text = """Results
The results section appears first in this source export.
Introduction
The introduction section appears second in this source export.
Methodology
The methodology section appears third in this source export.
"""

    result = ManuscriptParserService.parse_manuscript_sections(
        text, ["Introduction", "Methodology", "Results", "Discussion"]
    )

    assert list(result["parsed_sections"]) == [
        "Results",
        "Introduction",
        "Methodology",
        "Discussion",
    ]
    assert result["section_order"] == list(result["parsed_sections"])
