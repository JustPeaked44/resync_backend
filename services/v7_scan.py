"""Pure assembly helpers for the v7 manuscript flow."""

import re
import asyncio
import uuid


DIMENSIONS = ("objectives_alignment", "methodology_fit", "numeric_consistency", "claim_support")
SCORE_KEYS = (*DIMENSIONS, "overall_coherence")
NUMERIC_RULE = "both"
# The model rarely rates anything above ~0.85 and scores ordinary good writing around 0.65, so raw averages read
# as failing grades. Each pair rating is mapped through these (raw, scaled) anchors before averaging; calibrated on
# real thesis-length manuscripts (best pairs ~0.82, typical ~0.65, weak ~0.45, unrelated text ~0.15).
CALIBRATION = ((0.15, 0.0), (0.45, 0.60), (0.65, 0.86), (0.82, 1.0))
CRITICAL_THRESHOLD = 0.40
CRITICAL_PENALTY = 10
RECOMMENDATION_THRESHOLD = 0.85
EXPLAIN_LIMIT = 24
DEFAULT_RECOMMENDATION = ("Consider adding transitional sentences to better connect the logical flow "
                          "between these sections.")


def has_figures(text):
    text = re.sub(r"https?://\S+|\bdoi\s*:\s*\S+|\b10\.\d{4,9}/\S+", " ", text, flags=re.I)
    text = re.sub(r"\([^()]*\b(?:19|20)\d{2}[a-z]?\s*\)", " ", text)
    text = re.sub(r"\b(?:[A-Z][A-Za-z-]+(?:\s+et\s+al\.)?|et\s+al\.),\s*(?:19|20)\d{2}[a-z]?\b", " ", text)
    text = re.sub(r"\b(?:figure|fig\.?|table|chapter|section|page|pp?\.)\s*\d+(?:\.\d+)*\b", " ", text, flags=re.I)
    text = re.sub(r"(?m)^\s*(?:\d+(?:\.\d+)*[.)]\s+|\d+\s*$)", " ", text)
    return bool(re.search(r"(?<![\w.])\d+(?:\.\d+)?\s*%?\b|(?<![\w.])\d+(?:\.\d+)?%", text))


def numeric_applicable(text_a, text_b, rule=None):
    present = (has_figures(text_a), has_figures(text_b))
    return all(present) if (rule or NUMERIC_RULE) == "both" else any(present)


def calibrate(raw):
    if raw <= CALIBRATION[0][0]:
        return 0.0
    for (x0, y0), (x1, y1) in zip(CALIBRATION, CALIBRATION[1:]):
        if raw <= x1:
            return y0 + (y1 - y0) * (raw - x0) / (x1 - x0)
    return 1.0


def four_part_score(pairs):
    # The API score does not read v7_logic.calculate_manuscript_score's raw worker output.
    scored = [p for p in pairs if p["scores"]["overall_coherence"] is not None]
    values = {key: [calibrate(p["scores"][key]) for p in scored
                    if key != "numeric_consistency" or p["numeric_applicable"]] for key in DIMENSIONS}
    applicable = sum(bool(items) for items in values.values())
    averages = {key: round(sum(items) / len(items), 4) if items else None for key, items in values.items()}
    weights = {key: 1 / applicable if items else 0.0 for key, items in values.items()}
    score = round(100 * sum(sum(items) / len(items) * weights[key]
                            for key, items in values.items() if items), 2)
    return score, averages, weights


def final_score(pairs):
    """Four-part average (0-100) minus a flat penalty when any single pair is a critical flaw.

    The penalty applies once, not per pair, so many minor issues never stack up to zero.
    Returns (score, dimension averages, dimension weights, penalty applied).
    """
    score, averages, weights = four_part_score(pairs)
    critical = any(p["scores"]["overall_coherence"] is not None
                   and p["scores"]["overall_coherence"] < CRITICAL_THRESHOLD for p in pairs)
    penalty = CRITICAL_PENALTY if critical else 0
    return round(max(0.0, score - penalty), 2), averages, weights, penalty


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


def select_worst_pairs(pairs, limit=12):
    return sorted((p for p in pairs if p["pair_score"] is not None),
                  key=lambda p: (p["pair_score"], p["order"]))[:limit]


def select_pairs_to_explain(pairs):
    """Every pair that is below the recommendation threshold gets a real explanation, weakest first, so
    advice is equally specific across the manuscript instead of only for the worst few pairs."""
    weak = [p for p in pairs if p["scores"]["overall_coherence"] is not None
            and p["scores"]["overall_coherence"] < RECOMMENDATION_THRESHOLD]
    return select_worst_pairs(weak, EXPLAIN_LIMIT)


def band(score):
    return "Strong" if score >= 85 else "Solid" if score >= 70 else "Needs Revision"


def assemble_flow(sections, ml_pairs):
    flow = []
    for index, raw in enumerate(ml_pairs):
        scores = raw.get("scores")
        applicable = numeric_applicable(sections[index]["text"], sections[index + 1]["text"]) if scores else False
        pair_dimensions = DIMENSIONS if applicable else tuple(key for key in DIMENSIONS if key != "numeric_consistency")
        flow.append({
            "order": index + 1,
            "section_a": sections[index]["name"],
            "section_b": sections[index + 1]["name"],
            "pair_weight": scores["pair_weight"] if scores else None,
            "pair_score": sum(scores[key] for key in pair_dimensions) / len(pair_dimensions) if scores else None,
            "numeric_applicable": applicable,
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
            "overall_coherence": overall, "score": round(pair["pair_score"] * 100, 2)}


async def explain_flow(sections, flow):
    from services.v7_explain import explain_pair, explain_summary

    semaphore = asyncio.Semaphore(4)

    async def explain(pair):
        async with semaphore:
            index = pair["order"] - 1
            scores = dict(pair["scores"], pair_weight=pair["pair_weight"], pair_score=pair["pair_score"],
                          numeric_applicable=pair["numeric_applicable"])
            culprits = pair["culprit_sentences"]
            pair["explanation"] = await explain_pair(
                pair["section_a"], sections[index]["text"], pair["section_b"], sections[index + 1]["text"],
                scores, culprits["section_a"], culprits["section_b"])

    await asyncio.gather(*(explain(pair) for pair in select_pairs_to_explain(flow)))
    findings, recommendations = flatten_explanations(flow, sections)
    scored = [p for p in flow if p["pair_score"] is not None]
    synthesis = {"summary": await explain_summary(flow, findings),
                 "strongest_pair": pair_pointer(max(scored, key=lambda p: p["pair_score"])) if scored else None,
                 "weakest_pair": pair_pointer(min(scored, key=lambda p: p["pair_score"])) if scored else None}
    return findings, recommendations, synthesis


def flatten_explanations(flow, sections=None):
    from services.v7_explain import recommendation_category

    findings, recommendations = [], []
    highlighted = set()
    for pair in flow:
        explanation = pair["explanation"]
        overall = pair["scores"]["overall_coherence"]
        if explanation is None and overall is None:
            continue
        names = (pair["section_a"], pair["section_b"])
        score = round(pair["pair_score"] * 100, 2)
        explanation = explanation or {"highlighted_inconsistencies": [], "recommendations": [], "reference_list": []}
        fallback_category = recommendation_category(
            None, dict(pair["scores"], numeric_applicable=pair["numeric_applicable"]))
        pair_recommendations = sorted(explanation["recommendations"],
                                      key=lambda r: (names.index(r["section"]),
                                                     sections[pair["order"] - 1 + names.index(r["section"])]["text"].find(r["original_sentence"])
                                                     if sections else r["original_sentence"]))
        if overall < RECOMMENDATION_THRESHOLD and not pair_recommendations:
            # A low score never ships with zero advice, whether Gemini returned none or was never asked.
            pair_recommendations = [{"section": names[1], "original_sentence": None,
                                     "issue": f'The "{names[0]}" section does not lead clearly into the "{names[1]}" section.',
                                     "recommended_revision": DEFAULT_RECOMMENDATION}]
        for item in explanation["highlighted_inconsistencies"]:
            quotes = [{"section": item["section"], "text": item["quote"]}] if item["quote"] else []
            quotes.extend({"section": ref["section"], "text": ref["sentence"]}
                          for ref in explanation["reference_list"] if ref["section"] in names and
                          (ref["section"], ref["sentence"]) != (item["section"], item["quote"]))
            matching_revision = next((r["recommended_revision"] for r in pair_recommendations
                                      if r["section"] == item["section"]), "")
            findings.append({"section_a": names[0], "section_b": names[1], "pair_order": pair["order"],
                             "coherence_score": score, "explanation_what": item["what"],
                             "explanation_why": item["why"], "suggested_fix": matching_revision,
                             "inconsistency_id": str(uuid.uuid4()),
                             "evidence_a": next((q["text"] for q in quotes if q["section"] == names[0]), ""),
                             "evidence_b": next((q["text"] for q in quotes if q["section"] == names[1]), ""),
                             "evidence_quotes": quotes, "evidence_verified": bool(quotes),
                             "finding_status": "material_issue"})
        number = 0
        for item in pair_recommendations:
            key = (item["section"], item["original_sentence"])
            if item["original_sentence"] is not None:  # the default advice has no sentence to dedupe on
                if key in highlighted:
                    continue
                highlighted.add(key)
            revision = item["recommended_revision"]
            number += 1
            recommendations.append({"recommendation_id": str(uuid.uuid4()),
                                    "id": f"r-{pair['order']}-{number}",
                                    "section_a": names[0], "section_b": names[1], "pair_order": pair["order"],
                                    "original_sentence": item["original_sentence"],
                                    "issue": item.get("issue", ""),
                                    "section": item["section"],
                                    "category": item.get("category", fallback_category),
                                    "recommended_revision": revision, "recommendation_text": revision,
                                    "recommendation_priority": "High" if score < 50 else "Medium" if score < 70 else "Low",
                                    "target_section_name": item["section"]})
    return findings, recommendations
