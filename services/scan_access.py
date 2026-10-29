"""The sole projection from a stored scan result to its locked API view."""

from copy import deepcopy
from hashlib import sha256
import random
import re


FILLER_WORDS = ("calm", "pale", "soft", "open", "clear", "quiet", "light", "plain")


def _filler(length, rng):
    words = []
    while len(" ".join(words)) < length:
        words.append(rng.choice(FILLER_WORDS))
    return " ".join(words)[:length]


def locked_scan_view(result):
    if not result or not result.get("access") or result["access"].get("unlocked"):
        return result

    view = deepcopy(result)
    sections = view.get("manuscript_sections") or []
    rng = random.Random(int.from_bytes(sha256(str(result.get("analysis_run_id", "")).encode()).digest(), "big"))
    remaining = max(0, 40000 - sum(len(section["name"]) for section in sections[1:]))
    full_recommendations = view.get("recommendations") or []
    section_order = {item["name"]: index for index, item in enumerate(sections)}

    def position(item):
        name = item.get("section") or item.get("target_section_name")
        text = sections[section_order[name]]["text"] if name in section_order else ""
        return section_order.get(name, len(sections)), text.find(item.get("original_sentence") or "")

    view["recommendations"] = sorted(full_recommendations, key=position)[:2]
    view["access"]["locked_counts"] = {
        "sections": max(0, len(sections) - 1),
        "recommendations": max(0, len(full_recommendations) - len(view["recommendations"])),
        "citations": len(view.get("citations") or []),
    }
    for section in sections[1:]:
        heading = _filler(len(section["name"]), rng)
        budget = min(4000 - len(heading), remaining)
        paragraphs = []
        for paragraph in re.split(r"\n\s*\n", section.get("text") or ""):
            if not paragraph:
                continue
            if budget <= 0:
                break
            length = min(max(40, ((len(paragraph) + 20) // 40) * 40), budget)
            paragraphs.append(_filler(length, rng))
            budget -= length
        section["placeholder"] = {"heading": heading, "paragraphs": paragraphs}
        remaining -= sum(map(len, paragraphs))
        section["text"] = None
    view["citations"] = []
    view["inconsistencies"] = []
    view["ai_synthesis"]["summary"] = None
    for pair in view.get("manuscript_flow", []):
        pair["culprit_sentences"] = {"section_a": [], "section_b": []}
        pair["explanation"] = None
    return view
