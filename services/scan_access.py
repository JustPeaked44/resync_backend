"""The sole projection from a stored scan result to its locked API view."""

from copy import deepcopy
from hashlib import sha256
import random
import re


FILLER_WORDS = ("calm", "pale", "soft", "open", "clear", "quiet", "light", "plain")


def _section_index(names, name):
    exact = [i for i, value in enumerate(names) if value.strip().lower() == name.strip().lower()]
    if exact:
        return exact[0] if len(exact) == 1 else -1

    def normalize(value):
        value = re.sub(r"^(?:(?:chapter|part|section)\s+)?(?:\d+(?:\.\d+)*|[ivxlcdm]+)(?:[\s:.)\-\u2013\u2014]+|$)", "", value.strip(), flags=re.I)
        return " ".join("".join(c if c.isalnum() else " " for c in value.lower()).split())

    target = normalize(name)
    matches = [i for i, value in enumerate(names) if target and normalize(value) == target]
    return matches[0] if len(matches) == 1 else -1


def _usable_pair(pair, sections):
    a, b = pair.get("section_a") or "", pair.get("section_b") or ""
    names = [section["name"] for section in sections]
    indices = [_section_index(names, name) for name in (a, b)]
    if not a.strip() or not b.strip() or _section_index([a], b) == 0 or (indices[0] >= 0 and indices[0] == indices[1]):
        return False
    toc = lambda name, text: bool(re.match(r"^chapter\b", name.strip(), re.I) and re.search(r"(?:\.{2,}|\s{2,})\s*\d+\s*$", text, re.M))
    if any(toc(name, name) for name in (a, b)):
        return False
    for index in indices:
        if index < 0:
            continue
        section = sections[index]
        text = section.get("text")
        if isinstance(text, str) and not text.strip() and not section.get("blocks"):
            return False
        if toc(section["name"], section["name"] + "\n" + (text or "")):
            return False
    return True


def _has_revision(recommendation):
    text = re.sub(r"\*\*|`|\*", "", recommendation.get("recommended_revision") or "")
    text = re.sub(r"^\s*#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"^\s*(?:[-+]\s+|\d+[.)]\s+)", "", text, flags=re.M)
    return bool(text.strip())


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
    advice_orders = {item.get("pair_order") for item in full_recommendations if _has_revision(item)}
    all_clear = sum(1 for pair in view.get("manuscript_flow", [])
                    if _usable_pair(pair, sections) and pair.get("scores", {}).get("overall_coherence") is not None
                    and pair.get("severity") == "None" and pair.get("order") not in advice_orders)
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
        "all_clear": all_clear,
    }
    for section in sections[1:]:
        heading = _filler(len(section["name"]), rng)
        budget = min(4000 - len(heading), remaining)
        paragraphs = []
        for paragraph in re.split(r"\n+", section.get("text") or ""):
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
        section.pop("blocks", None)
    view["citations"] = []
    view["inconsistencies"] = []
    view["ai_synthesis"]["summary"] = None
    for pair in view.get("manuscript_flow", []):
        pair["culprit_sentences"] = {"section_a": [], "section_b": []}
        pair["explanation"] = None
    return view
