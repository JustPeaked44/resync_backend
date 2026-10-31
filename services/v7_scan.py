"""Pure assembly helpers for the v7 manuscript flow."""

import logging
import os
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
# Gemini decides whether a pair holds a REAL inconsistency ("None" is a false alarm); the model's rating then grades it
# for display: below 0.40 Critical, 0.40 to 0.65 Moderate, 0.65 to 0.85 Minor. Grades do not change the score.
SEVERITY_PENALTY = {"Critical": 10, "Moderate": 5, "Minor": 1, "None": 0}
START_SCORE = 100
# Internal score keys (as the model and Gemini name them) -> the names the API and UI show.
DIMENSION_NAMES = {"objectives_alignment": "context_alignment", "methodology_fit": "operational_alignment",
                   "numeric_consistency": "evidence_consistency", "claim_support": "logical_support"}
RECOMMENDATION_THRESHOLD = 0.85
EXPLAIN_LIMIT = 15  # the local step keeps at most this many pairs; also the hard cap on Gemini calls per scan
EXPLAIN_CONCURRENCY = int(os.getenv("EXPLAIN_CONCURRENCY", EXPLAIN_LIMIT))  # every pair is reviewed at the same time
EXPLAIN_TIMEOUT = 240  # seconds one pair may take (waiting for a rate-limit slot and all retries included)
MIN_SECTION_WORDS = 5  # a heading with no text under it cannot be rated, so neighbors are paired across it


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


def manuscript_score(pairs, transitions=None):
    """(overall score, dimension breakdown).

    Every dimension starts from its `baseline`: how well the paper's neighboring sections lead into each other, from the
    model's calibrated ratings of every transition (100 when `transitions` is not given). It then loses points for each
    real issue the AI review charged to it (Critical 10, Moderate 5, Minor 1), so a paper never scores a perfect 100
    just because nothing was quotable, or because the AI review could not run. A dimension with nothing to measure, such
    as Evidence Consistency in a paper with no figures, is "N/A". The overall score is the plain average of the
    applicable ones. `deductions` lists exactly which pairs cost which points, so every score can be traced.
    """
    scored = [p for p in pairs if p["scores"]["overall_coherence"] is not None]
    breakdown = {}
    for key, name in DIMENSION_NAMES.items():
        used = [p for p in scored if key != "numeric_consistency" or p.get("numeric_applicable", True)]
        average = (transitions or {}).get(key)
        if not used and average is None:
            breakdown[name] = {"score": "N/A", "baseline": None, "penalty": 0, "issues": 0, "pairs": 0, "deductions": []}
            continue
        baseline = START_SCORE if not transitions else (round(calibrate(average) * 100) if average is not None else START_SCORE)
        hits = [p for p in used if p.get("failed_dimension") == key and p.get("severity") in ("Critical", "Moderate", "Minor")]
        deductions = [{"order": p.get("order"), "section_a": p.get("section_a", ""), "section_b": p.get("section_b", ""),
                       "severity": p["severity"], "points": SEVERITY_PENALTY[p["severity"]]} for p in hits]
        penalty = sum(item["points"] for item in deductions)
        breakdown[name] = {"score": max(0, baseline - penalty), "baseline": baseline, "penalty": penalty, "issues": len(hits),
                           "pairs": len(used), "deductions": deductions}
    values = [entry["score"] for entry in breakdown.values() if entry["score"] != "N/A"]
    return (round(sum(values) / len(values), 2) if values else 0.0), breakdown


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
    """One flow entry per pair the local model kept. A pair is any two sections (index_a < index_b); older callers pass
    neighbors only, so a missing index means "the section at this position and the next"."""
    flow = []
    for position, raw in enumerate(ml_pairs):
        index_a = raw.get("index_a", position)
        index_b = raw.get("index_b", index_a + 1)
        scores = raw.get("scores")
        applicable = numeric_applicable(sections[index_a]["text"], sections[index_b]["text"]) if scores else False
        pair_dimensions = DIMENSIONS if applicable else tuple(key for key in DIMENSIONS if key != "numeric_consistency")
        flow.append({
            "order": position + 1,
            "index_a": index_a,
            "index_b": index_b,
            "adjacent": index_b == index_a + 1,
            "section_a": sections[index_a]["name"],
            "section_b": sections[index_b]["name"],
            "pair_weight": scores["pair_weight"] if scores else None,
            "pair_score": sum(scores[key] for key in pair_dimensions) / len(pair_dimensions) if scores else None,
            "numeric_applicable": applicable,
            "scores": {key: scores[key] if scores else None for key in SCORE_KEYS},
            "skipped_reason": raw.get("skipped") if not scores else None,
            "culprit_sentences": {"section_a": raw.get("culprits_a", []), "section_b": raw.get("culprits_b", [])},
            "severity": None,
            "failed_dimension": None,
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
    from services import gemini
    from services.v7_explain import explain_pair, explain_summary

    semaphore = asyncio.Semaphore(EXPLAIN_CONCURRENCY)

    async def explain(pair):
        async with semaphore:
            index_a, index_b = pair["index_a"], pair["index_b"]
            scores = dict(pair["scores"], pair_weight=pair["pair_weight"], pair_score=pair["pair_score"],
                          numeric_applicable=pair["numeric_applicable"])
            culprits = pair["culprit_sentences"]
            try:
                pair["explanation"] = await asyncio.wait_for(explain_pair(
                    pair["section_a"], sections[index_a]["text"], pair["section_b"], sections[index_b]["text"],
                    scores, culprits["section_a"], culprits["section_b"], adjacent=pair["adjacent"]), EXPLAIN_TIMEOUT)
            except asyncio.TimeoutError:  # never let one slow pair hold the whole scan at 99%
                logging.getLogger("resync.v7_scan").warning("Pair %s took over %ss; leaving it unreviewed", pair["order"], EXPLAIN_TIMEOUT)

    to_explain = select_pairs_to_explain(flow)
    gemini.expect_calls(len(to_explain) + 1)  # one call per reviewed pair plus the summary, so scan polls can say when the AI is busy
    try:
        await asyncio.gather(*(explain(pair) for pair in to_explain), return_exceptions=True)  # one bad pair never cancels the rest
        # Gemini fails more under load (429/503), so pairs it could not judge get one calmer, one-at-a-time second pass.
        for pair in to_explain:
            if pair["explanation"] and pair["explanation"]["severity"] is None:
                await explain(pair)
        for pair in flow:
            overall = pair["scores"]["overall_coherence"]
            if overall is None:
                continue
            explanation = pair["explanation"]
            # Strong pairs were never sent to Gemini and have no issue; a weak pair Gemini could not review stays unknown.
            # The AI review is the judge: its grade stands (strict rules: only a contradiction it can quote is an issue).
            pair["severity"] = explanation["severity"] if explanation else ("None" if overall >= RECOMMENDATION_THRESHOLD else None)
            pair["failed_dimension"] = explanation["failed_dimension"] if explanation else ("none" if overall >= RECOMMENDATION_THRESHOLD else None)
        findings, recommendations = flatten_explanations(flow, sections)
        scored = [p for p in flow if p["pair_score"] is not None]
        unreviewed = sum(1 for p in flow if p["scores"]["overall_coherence"] is not None and p["severity"] is None)
        # Tell the report why pairs are missing, so "no recommendations" is never mistaken for "nothing wrong".
        review_status = {"status": "complete" if not unreviewed else "daily_limit" if gemini.quota_blocked() else "partial",
                         "unreviewed": unreviewed}
        synthesis = {"summary": await explain_summary(flow, findings),
                     "strongest_pair": pair_pointer(max(scored, key=lambda p: p["pair_score"])) if scored else None,
                     "weakest_pair": pair_pointer(min(scored, key=lambda p: p["pair_score"])) if scored else None,
                     "review_status": review_status}
    finally:
        gemini.expect_calls(0)
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
        # Linking sentences to add come first: they connect the two sections, which is what moves the score.
        bridges = [{"section": b["section"], "original_sentence": None, "issue": "", "kind": "add",
                    "placement": b["placement"], "recommended_revision": b["sentence"]}
                   for b in explanation.get("bridges", [])]
        fallback_category = recommendation_category(
            None, dict(pair["scores"], numeric_applicable=pair["numeric_applicable"]))
        pair_recommendations = sorted(explanation["recommendations"],
                                      key=lambda r: (names.index(r["section"]),
                                                     sections[(pair["index_a"], pair["index_b"])[names.index(r["section"])]]["text"].find(r["original_sentence"])
                                                     if sections else r["original_sentence"]))
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
        for item in bridges + pair_recommendations:
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
                                    "kind": item.get("kind", "edit"),
                                    **({"placement": item["placement"]} if item.get("placement") else {}),
                                    "section": item["section"],
                                    "category": item.get("category", fallback_category),
                                    "recommended_revision": revision, "recommendation_text": revision,
                                    "recommendation_priority": "High" if score < 50 else "Medium" if score < 70 else "Low",
                                    "target_section_name": item["section"]})
    return findings, recommendations
