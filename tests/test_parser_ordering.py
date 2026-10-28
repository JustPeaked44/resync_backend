from pathlib import Path

from services.ingestion import DocumentIngestionService
from services.parser import ManuscriptParserService
from services.v7_scan import merge_repeated_sections


FIXTURE = Path(__file__).parent / "fixtures" / "google_docs_plaintext_ordering_export.txt"


def test_plaintext_export_keeps_distinct_headings_in_source_order():
    exported_text = DocumentIngestionService.strip_document_noise(
        FIXTURE.read_text(encoding="utf-8")
    )

    result = ManuscriptParserService.parse_manuscript_sections(exported_text, None)

    assert list(result["parsed_sections"]) == [
        "introduction",
        "methodology",
        "background",
        "results",
    ]
    assert result["section_roles"] == {
        "introduction": "introduction",
        "methodology": "methodology",
        "background": "introduction",
        "results": "results",
    }
    assert "INTRODUCTION_FIRST" in result["parsed_sections"]["introduction"]
    assert "INTRODUCTION_SECOND" in result["parsed_sections"]["background"]
    assert result["section_order"] == list(result["parsed_sections"])

    merged = merge_repeated_sections(result["parsed_sections"], result["section_order"])
    assert [section["name"] for section in merged] == ["introduction", "methodology", "background", "results"]


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


def test_auto_detect_keeps_different_literal_headings_with_same_role():
    text = ("Related Literature\n" + "Literature text with sufficient detail for the parser. " * 5 + "\n"
            + "Related Studies\n" + "First study text with sufficient detail for the parser. " * 5 + "\n"
            + "Related Studies (2)\n" + "Second study text with sufficient detail for the parser. " * 5)
    result = ManuscriptParserService.parse_manuscript_sections(text, None)
    merged = merge_repeated_sections(result["parsed_sections"], result["section_order"])
    assert [section["name"] for section in merged] == ["related literature", "related studies"]
    assert "First study text" in merged[1]["text"] and "Second study text" in merged[1]["text"]
