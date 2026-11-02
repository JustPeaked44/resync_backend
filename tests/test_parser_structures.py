"""Papers that do not use the usual capstone headings must still be split into sections the scan can compare."""

import pytest

from services.parser import ManuscriptParserService


def body(topic, sentences=9):
    """A paragraph long enough to look like real prose, about one topic."""
    return " ".join(
        f"This paragraph number {n} explains the {topic} of the study in plain words so that the reader can follow it."
        for n in range(1, sentences + 1)
    )


def parse(text):
    result = ManuscriptParserService.parse_manuscript_sections(text, None)
    return result, result["parsed_sections"], result["section_roles"]


def test_numbered_journal_headings_are_sections():
    text = "\n".join(["A Study of Sleep and Grades", "Jane Doe", "",
                      "1. Introduction", body("background"), "2. Related Work", body("literature"),
                      "3. Methods", body("procedure"), "4. Results", body("findings"),
                      "5. Conclusion", body("summary"), "References", "Smith, J. (2020). A paper. Journal, 1(1), 1-10."])
    result, sections, roles = parse(text)
    assert len([n for n, r in roles.items() if r != "references"]) >= 5
    assert result["structure"] == "structure"   # "1. Title" is found by its shape, not by the usual heading words
    assert "sleep" not in " ".join(sections).lower()  # the title page is not a section


def test_roman_numeral_all_caps_headings_are_sections():
    text = "\n".join(["I. INTRODUCTION", body("background"), "II. RELATED WORK", body("literature"),
                      "III. PROPOSED SYSTEM", body("design"), "IV. EXPERIMENTS", body("evaluation"),
                      "V. CONCLUSION", body("summary")])
    _, sections, _ = parse(text)
    assert len(sections) == 5


def test_chapter_headings_with_their_own_titles_are_sections():
    text = "\n".join(["CHAPTER 1", "THE PROBLEM AND ITS SETTING", body("problem"),
                      "CHAPTER 2", "WHAT OTHERS HAVE FOUND", body("literature"),
                      "CHAPTER 3", "HOW THE WORK WAS DONE", body("procedure"),
                      "CHAPTER 4", "WHAT THE DATA SHOWED", body("findings")])
    result, sections, _ = parse(text)
    assert len(sections) == 4 and result["structure"] == "structure"
    assert any("what others have found" in name for name in sections)


def test_plain_title_lines_between_paragraphs_are_sections():
    text = "\n".join(["Why we did this", body("motivation"), "How we did it", body("procedure"),
                      "What we found", body("findings"), "What it means", body("meaning")])
    _, sections, _ = parse(text)
    assert list(sections) == ["why we did this", "how we did it", "what we found", "what it means"]


def test_a_paper_with_no_headings_is_split_into_equal_parts_in_order():
    paragraphs = [body(f"topic {n}", 6) for n in range(1, 41)]
    result, sections, roles = parse("\n\n".join(paragraphs))
    assert result["structure"] == "split" and 4 <= len(sections) <= 8
    names = list(sections)
    assert names == result["section_order"]
    assert roles[names[0]] == "introduction" and roles[names[-1]] == "conclusion"
    rejoined = " ".join(sections.values()).split()
    assert len(rejoined) == len(" ".join(paragraphs).split())  # nothing is lost or repeated


def test_usual_headings_still_win_over_the_fallbacks():
    text = "\n".join(["Introduction", body("background"), "Methodology", body("procedure"),
                      "Results", body("findings"), "Conclusion", body("summary")])
    result, sections, roles = parse(text)
    assert result["structure"] == "headings"
    assert set(roles.values()) == {"introduction", "methodology", "results", "conclusion"}


def test_text_that_is_too_short_to_compare_stays_empty():
    result, sections, _ = parse("Dear sir, please find my resume attached.")
    assert sections == {}
