"""Is this document a research paper at all?

The scanner will happily split any text into parts and score it, so a recipe, a letter or a pile of filler text would
come back with a score. This check looks for what makes a manuscript a manuscript: real academic headings (introduction,
methodology, results, conclusion, references ...), citations, and enough writing to compare.
"""
import re
from typing import Any, Dict, Optional

from services.reference_parser import extract_intext_citations

MIN_WORDS = 200          # below this there is nothing to compare
SHORT_PAPER_WORDS = 600  # a short paper must show the full set of paper headings to count
# Roles that only a research paper's headings map to. "body" and the synthetic "part N of M" splits do not count.
_ACADEMIC_ROLES = {"abstract", "introduction", "objectives", "literature", "methodology", "results", "discussion",
                   "conclusion", "references", "limitations", "future_work"}
_SYNTHETIC_NAME = re.compile(r"^part \d+ of \d+$", re.I)
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
    if not structured:
        return {"ok": False, "reason": "no_paper_structure", **signals}
    if vocabulary < 0.004 and citations < 3:
        return {"ok": False, "reason": "not_academic_language", **signals}
    return {"ok": True, "reason": "", **signals}
