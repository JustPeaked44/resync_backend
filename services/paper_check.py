"""Is this document a research paper at all?

The scanner will happily split any text into parts and score it, so a recipe, a letter or a pile of filler text would
come back with a score. This check looks for what makes a manuscript a manuscript: real academic headings (introduction,
methodology, results, conclusion, references ...), citations, and enough writing to compare.
"""
import io
import re
import zipfile
from typing import Any, Dict, Optional, Tuple

from services.reference_parser import extract_intext_citations

MIN_WORDS = 200          # below this there is nothing to compare
SHORT_PAPER_WORDS = 600  # a short paper must show the full set of paper headings to count
# Roles that only a research paper's headings map to. "body" and the synthetic "part N of M" splits do not count.
_ACADEMIC_ROLES = {"abstract", "introduction", "objectives", "literature", "methodology", "results", "discussion",
                   "conclusion", "references", "limitations", "future_work"}
_SYNTHETIC_NAME = re.compile(r"^part \d+ of \d+$", re.I)
# Headings are matched by their words, so a study guide or manual can collect "results", "conclusion" and "limitations"
# by accident. A manuscript also says what it is about (one of these) and shows how it knows (method or sources).
_OPENING_ROLES = {"abstract", "introduction", "objectives", "literature"}
MAX_QUESTION_SHARE = 0.15      # a survey form or question list; full theses with their questionnaires stay under 0.02
_ACADEMIC_WORDS = re.compile(
    r"\b(?:study|research|respondents?|participants?|hypothes[ei]s|objectives?|methodolog\w*|literature|"
    r"findings|data|analysis|results?|conclusion|variables?|sample|survey|framework|significant)\b", re.I)

NOT_A_PAPER_MESSAGE = (
    "This does not look like a research paper, so it was not scanned. A manuscript needs clear section headings "
    "(for example Introduction, Methodology, Results, Conclusion), citations and a reference list. "
    "Upload your paper instead."
)


def check_research_paper(raw_text: str, parse_result: Optional[Dict[str, Any]], has_template: bool = False) -> Dict[str, Any]:
    """Returns {"ok": bool, "reason": str, ...signals}. A custom department template relaxes the heading rule."""
    text = raw_text or ""
    words = len(text.split())
    roles = (parse_result or {}).get("section_roles", {}) or {}
    named = {name: role for name, role in roles.items() if not _SYNTHETIC_NAME.match(name)}
    academic = {role for role in named.values() if role in _ACADEMIC_ROLES}
    has_references = "references" in academic
    citations = len(extract_intext_citations(text)) if text else 0
    vocabulary = len(_ACADEMIC_WORDS.findall(text)) / max(1, words)
    signals = {"words": words, "academic_roles": sorted(academic), "citations": citations, "vocabulary": round(vocabulary, 4)}

    if words < MIN_WORDS:
        return {"ok": False, "reason": "too_short", **signals}
    if words < SHORT_PAPER_WORDS and len(academic) < 4:
        return {"ok": False, "reason": "too_short", **signals}
    needed = 2 if has_template else 3
    structured = len(academic) >= needed or (len(academic) >= 2 and (citations >= 3 or has_references))
    grounded = "methodology" in academic or has_references or citations >= 3
    if not has_template:       # a department template names its own headings
        structured = structured and bool(academic & _OPENING_ROLES) and grounded
    if not structured:
        return {"ok": False, "reason": "no_paper_structure", **signals}
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if sum(line.endswith("?") for line in lines) > MAX_QUESTION_SHARE * len(lines):
        return {"ok": False, "reason": "questionnaire", **signals}
    if vocabulary < 0.004 and citations < 3:
        return {"ok": False, "reason": "not_academic_language", **signals}
    return {"ok": True, "reason": "", **signals}


UNREADABLE_PREFIX = "Could not read this document because "


def unreadable_message(scanned: bool = False, handwritten: bool = False, language: bool = False,
                       two_columns: bool = False) -> Optional[str]:
    """One sentence naming every reason found, in any combination. None when there is none."""
    kinds = [name for name, found in (("scanned", scanned), ("handwritten", handwritten)) if found]
    contents = [name for name, found in (("an unsupported language", language),
                                         ("a two-column structure", two_columns)) if found]
    if kinds:
        reason = f"it is a {' and '.join(kinds)} file"
        if contents:
            reason += " and contains " + " and ".join(contents)
    elif language and not two_columns:
        reason = "this language is unsupported"
    elif contents:
        reason = "it contains " + " and ".join(contents)
    else:
        return None
    return UNREADABLE_PREFIX + reason + "."


# The scan reads English only. Everyday English words make up well over a quarter of English prose and very little of other
# languages written in the same alphabet. Words shared with other languages ("a", "no", "me") are left out on purpose.
_ENGLISH_WORDS = frozenset(
    "the of and to in is that for with as are was were be by this on from or an which it at not have has their these "
    "they will can been its also more such than between may".split())
MIN_ENGLISH_SHARE = 0.15
# Bulleted English (résumés, templates) also has few everyday words, so a low share alone is not enough: either there is
# next to no English at all, or everyday words of other languages outnumber the English ones.
NO_ENGLISH_SHARE = 0.04
_FOREIGN_WORDS = frozenset(
    "de la el que los las del y un una por con para es en le les des et du dans pour est der die und das ist nicht mit "
    "von zu ein eine il di che della gli uma não em ng sa ang mga ay yang dan untuk dari dengan".split())
MIN_LANGUAGE_SAMPLE = 100       # fewer words than this are too few to judge a language
MAX_NON_LATIN_SHARE = 0.30      # a few Greek letters in formulas are fine


def unsupported_language(text: str) -> bool:
    """True when the writing is clearly not English."""
    letters = [c for c in (text or "") if c.isalpha()]
    if len(letters) >= 200:
        non_latin = sum(1 for c in letters if not (c <= "ɏ" or "Ḁ" <= c <= "ỿ" or "Ａ" <= c <= "ｚ"))
        if non_latin / len(letters) > MAX_NON_LATIN_SHARE:
            return True
    words = re.findall(r"[^\W\d_]+", (text or "").lower())
    if len(words) < MIN_LANGUAGE_SAMPLE:
        return False
    english = sum(1 for word in words if word in _ENGLISH_WORDS) / len(words)
    if english >= MIN_ENGLISH_SHARE:
        return False
    return english < NO_ENGLISH_SHARE or sum(1 for word in words if word in _FOREIGN_WORDS) / len(words) > english


def docx_layout(data: Optional[bytes]) -> Tuple[int, bool]:
    """(number of pictures placed in the pages, whether any part is set in two or more columns). (0, False) if unreadable."""
    if not data:
        return 0, False
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            info = z.getinfo("word/document.xml")
            if info.file_size > 60 * 1024 * 1024:
                return 0, False
            xml = z.read(info).decode("utf-8", "ignore")
    except Exception:
        return 0, False
    pictures = xml.count("<w:drawing") + xml.count("<w:pict")
    return pictures, any(int(n) >= 2 for n in re.findall(r'<w:cols[^>]*?\sw:num="(\d+)"', xml))


def is_picture_document(data: Optional[bytes], words: int = 0) -> bool:
    """A Word file made of pictures with almost no text of its own: a scan, or photographed handwriting."""
    return words < MIN_WORDS and docx_layout(data)[0] > 0


async def document_problem(data: Optional[bytes], raw_text: str, paper_ok: bool) -> Optional[str]:
    """Why this document cannot be read, naming every reason at once. None when it can.

    A picture document and an unsupported language are always refused. Two columns alone are refused only when the paper
    check also failed, so a paper that reads fine despite a small two-column part is still scanned."""
    words = len((raw_text or "").split())
    two_columns = docx_layout(data)[1]
    language = unsupported_language(raw_text)
    if is_picture_document(data, words):
        from services.page_check import read_pictures
        seen = await read_pictures(data) or {}     # empty when the pictures could not be looked at: call it a scan
        handwritten = bool(seen.get("handwritten"))
        return unreadable_message(
            scanned=bool(seen.get("scanned_print")) or not handwritten,
            handwritten=handwritten,
            language=language or bool(seen.get("non_english")),
            two_columns=two_columns or bool(seen.get("two_columns")),
        )
    if language or (two_columns and not paper_ok):
        return unreadable_message(language=language, two_columns=two_columns)
    return None
