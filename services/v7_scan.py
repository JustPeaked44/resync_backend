"""Pure assembly helpers for the v7 manuscript flow."""

import re


DIMENSIONS = ("objectives_alignment", "methodology_fit", "numeric_consistency", "claim_support")
SCORE_KEYS = (*DIMENSIONS, "overall_coherence")


def merge_repeated_sections(parsed_sections, section_order):
    merged = {}
    for heading in section_order:
        if heading not in parsed_sections:
            continue
        base = re.sub(r" \(\d+\)$", "", heading)
        text = (parsed_sections[heading] or "").strip()
        if base in merged:
            if text:
                merged[base]["text"] = "\n".join(filter(None, (merged[base]["text"], text)))
        else:
            merged[base] = {"name": heading, "text": text}
    return list(merged.values())


def select_worst_pairs(pairs):
    return sorted((p for p in pairs if p["scores"]["overall_coherence"] is not None),
                  key=lambda p: (p["scores"]["overall_coherence"], p["order"]))[:8]


def weighted_score(pairs):
    scored = [p for p in pairs if p["scores"]["overall_coherence"] is not None]
    weight = sum(p["pair_weight"] for p in scored)
    if not scored:
        return 0.0
    if weight <= 1e-9:
        return round(100 * sum(p["scores"]["overall_coherence"] for p in scored) / len(scored), 2)
    return round(100 * sum(p["scores"]["overall_coherence"] * p["pair_weight"] for p in scored) / weight, 2)


def band(score):
    return "Strong" if score >= 85 else "Solid" if score >= 70 else "Needs Revision"


def dimension_averages(pairs):
    scored = [p["scores"] for p in pairs if p["scores"]["overall_coherence"] is not None]
    return {key: round(sum(p[key] for p in scored) / len(scored), 4) if scored else None for key in DIMENSIONS}


def assemble_flow(sections, ml_pairs):
    flow = []
    for index, raw in enumerate(ml_pairs):
        scores = raw.get("scores")
        flow.append({
            "order": index + 1,
            "section_a": sections[index]["name"],
            "section_b": sections[index + 1]["name"],
            "pair_weight": scores["pair_weight"] if scores else None,
            "scores": {key: scores[key] if scores else None for key in SCORE_KEYS},
            "skipped_reason": raw.get("skipped") if not scores else None,
            "culprit_sentences": {"section_a": raw.get("culprits_a", []), "section_b": raw.get("culprits_b", [])},
            "explanation": None,
        })
    return flow


def pair_pointer(pair):
    if pair is None:
        return None
    overall = pair["scores"]["overall_coherence"]
    return {"order": pair["order"], "section_a": pair["section_a"], "section_b": pair["section_b"],
            "overall_coherence": overall, "score": round(overall * 100, 2)}
