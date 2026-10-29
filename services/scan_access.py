"""The sole projection from a stored scan result to its locked API view."""

from copy import deepcopy


def locked_scan_view(result):
    if not result or not result.get("access") or result["access"].get("unlocked"):
        return result

    view = deepcopy(result)
    sections = view.get("manuscript_sections") or []
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
        section["text"] = None
    view["citations"] = []
    view["inconsistencies"] = []
    view["ai_synthesis"]["summary"] = None
    for pair in view.get("manuscript_flow", []):
        pair["culprit_sentences"] = {"section_a": [], "section_b": []}
        pair["explanation"] = None
    return view
