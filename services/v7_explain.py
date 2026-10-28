"""Strict, torch-free Gemini explanations of v7-scored neighbor pairs."""

import json
import logging
from services import gemini

logger = logging.getLogger("resync.v7_explain")
STRICT_RULES = """STRICT RULES:
1. Do not assume or mention research type.
2. Do not demand specific structural changes or missing chapters.
3. Do not use outside knowledge; use only supplied text and model scores.
4. Only assess whether Section B logically correlates with Section A.
5. Never change model scores; they come from the PyTorch model."""
PAIR_PROMPT = """Explain correlation between neighboring manuscript sections.
{rules}
Section A ({a}): {text_a}
Section B ({b}): {text_b}
Model scores (0 to 1): {scores}
Culprit sentences in A: {culprits_a}
Culprit sentences in B: {culprits_b}
Return JSON with exactly four top-level keys: coherence_scores (copy model scores),
highlighted_inconsistencies (array of objects with section, quote, what, why),
recommendations (array of objects with section, original_sentence, recommended_revision),
reference_list (array of objects with section, sentence).
Use exactly {a} or {b} as each section value. Quotes and original sentences must be copied verbatim from the named section. If no issue is supported, return empty arrays. Recommend only revisions justified by the supplied text."""
SUMMARY_PROMPT = """Summarize correlation of these neighboring manuscript pairs in one concise paragraph.
{rules}
Use only these scored pairs and verified findings: {data}
Return JSON with one string key, summary. Do not invent evidence or alter scores."""


def coherence_scores(scores):
    return {"overall": scores["overall_coherence"], "objectives": scores["objectives_alignment"],
            "methodology": scores["methodology_fit"], "numeric": scores["numeric_consistency"],
            "claims": scores["claim_support"]}


def normalize_explanation(raw, scores, role_a, text_a, role_b, text_b):
    raw = raw if isinstance(raw, dict) else {}
    source = {role_a: text_a, role_b: text_b}
    aliases = {"Section A": role_a, "Section B": role_b,
               f"Section A ({role_a})": role_a, f"Section B ({role_b})": role_b}

    def section_name(value):
        if not isinstance(value, str):
            return None
        return value if value in source else aliases.get(value)

    def verified(section, quote):
        if not isinstance(section, str) or section not in source or not isinstance(quote, str) or not quote.strip():
            return None
        match, ok = gemini.verify_quote(quote, source[section])
        return match if ok else None

    issues = []
    for item in raw.get("highlighted_inconsistencies", []) if isinstance(raw.get("highlighted_inconsistencies"), list) else []:
        if isinstance(item, dict):
            section = section_name(item.get("section"))
            if (section and item.get("what") != "No major inconsistencies found."
                    and all(isinstance(item.get(k), str) and item[k].strip() for k in ("what", "why"))):
                issues.append({"section": section, "quote": verified(section, item.get("quote")),
                               "what": item["what"], "why": item["why"]})
    recommendations = []
    for item in raw.get("recommendations", []) if isinstance(raw.get("recommendations"), list) else []:
        if not isinstance(item, dict):
            continue
        section = section_name(item.get("section"))
        revision = item.get("recommended_revision")
        if section and isinstance(revision, str) and revision.strip() and revision != "No major inconsistencies found.":
            recommendations.append({"section": section,
                                    "original_sentence": verified(section, item.get("original_sentence")),
                                    "recommended_revision": revision})
    references = []
    for item in raw.get("reference_list", []) if isinstance(raw.get("reference_list"), list) else []:
        if isinstance(item, dict):
            section = section_name(item.get("section"))
            quote = verified(section, item.get("sentence"))
            if quote:
                references.append({"section": section, "sentence": quote})
    return {"coherence_scores": coherence_scores(scores), "highlighted_inconsistencies": issues,
            "recommendations": recommendations, "reference_list": references}


async def _generate(prompt):
    payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
               "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"}}
    response = await gemini.post_gemini_with_retry(payload)
    response.raise_for_status()
    return json.loads(response.json()["candidates"][0]["content"]["parts"][0]["text"])


async def explain_pair(role_a, text_a, role_b, text_b, scores, culprits_a, culprits_b):
    prompt = PAIR_PROMPT.format(rules=STRICT_RULES, a=role_a, b=role_b,
                                text_a=gemini.truncate_text(text_a),
                                text_b=gemini.truncate_text(text_b),
                                scores=json.dumps(coherence_scores(scores)),
                                culprits_a=json.dumps(culprits_a), culprits_b=json.dumps(culprits_b))
    try:
        return normalize_explanation(await _generate(prompt), scores, role_a, text_a, role_b, text_b)
    except Exception:
        logger.exception("v7 Gemini pair explanation failed for %s -> %s", role_a, role_b)
        return normalize_explanation({}, scores, role_a, text_a, role_b, text_b)


async def explain_summary(flow, findings):
    data = [{"order": p["order"], "section_a": p["section_a"], "section_b": p["section_b"],
             "scores": p["scores"]} for p in flow if p["scores"]["overall_coherence"] is not None]
    prompt = SUMMARY_PROMPT.format(rules=STRICT_RULES, data=json.dumps({"pairs": data, "findings": findings}))
    try:
        raw = await _generate(prompt)
        summary = raw.get("summary") if isinstance(raw, dict) else None
        return summary.strip() if isinstance(summary, str) and summary.strip() else None
    except Exception:
        logger.exception("v7 Gemini summary failed")
        return None
