"""Pure assembly helpers for the v7 manuscript flow."""

import re
import asyncio
import uuid


DIMENSIONS = ("objectives_alignment", "methodology_fit", "numeric_consistency", "claim_support")
SCORE_KEYS = (*DIMENSIONS, "overall_coherence")
NUMERIC_RULE = "both"


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


def four_part_score(pairs):
    # The API score does not read v7_logic.calculate_manuscript_score's raw worker output.
    scored = [p for p in pairs if p["scores"]["overall_coherence"] is not None]
    values = {key: [p["scores"][key] for p in scored
                    if key != "numeric_consistency" or p["numeric_applicable"]] for key in DIMENSIONS}
    applicable = sum(bool(items) for items in values.values())
    averages = {key: round(sum(items) / len(items), 4) if items else None for key, items in values.items()}
    weights = {key: 1 / applicable if items else 0.0 for key, items in values.items()}
    score = round(100 * sum(sum(items) / len(items) * weights[key]
                            for key, items in values.items() if items), 2)
    return score, averages, weights


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
    return sorted((p for p in pairs if p["pair_score"] is not None),
                  key=lambda p: (p["pair_score"], p["order"]))[:12]


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
            scores = dict(pair["scores"], pair_weight=pair["pair_weight"], pair_score=pair["pair_score"])
            culprits = pair["culprit_sentences"]
            pair["explanation"] = await explain_pair(
                pair["section_a"], sections[index]["text"], pair["section_b"], sections[index + 1]["text"],
                scores, culprits["section_a"], culprits["section_b"])

    await asyncio.gather(*(explain(pair) for pair in select_worst_pairs(flow)))
    findings, recommendations = flatten_explanations(flow)
    scored = [p for p in flow if p["pair_score"] is not None]
    synthesis = {"summary": await explain_summary(flow, findings),
                 "strongest_pair": pair_pointer(max(scored, key=lambda p: p["pair_score"])) if scored else None,
                 "weakest_pair": pair_pointer(min(scored, key=lambda p: p["pair_score"])) if scored else None}
    return findings, recommendations, synthesis


def flatten_explanations(flow):
    findings, recommendations = [], []
    for pair in flow:
        explanation = pair["explanation"]
        if explanation is None:
            continue
        names = (pair["section_a"], pair["section_b"])
        score = round(pair["pair_score"] * 100, 2)
        pair_recommendations = explanation["recommendations"]
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
        for item in pair_recommendations:
            revision = item["recommended_revision"]
            recommendations.append({"recommendation_id": str(uuid.uuid4()),
                                    "section_a": names[0], "section_b": names[1], "pair_order": pair["order"],
                                    "original_sentence": item["original_sentence"],
                                    "recommended_revision": revision, "recommendation_text": revision,
                                    "recommendation_priority": "High" if score < 50 else "Medium" if score < 70 else "Low",
                                    "target_section_name": item["section"]})
    return findings, recommendations
