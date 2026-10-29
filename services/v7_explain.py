"""Strict, torch-free Gemini explanations of v7-scored neighbor pairs."""

import json
import logging
import re
from services import gemini

logger = logging.getLogger("resync.v7_explain")


def plain_text(value):
    value = re.sub(r"(?m)^\s*(?:#{1,6}\s*|[-*•●○■☐☑☒]\s+|\d+[.)]\s+)+", "", value)
    return value.replace("*", "").replace("`", "").strip()


STRICT_RULES = """STRICT RULES:
1. Do not assume or mention research type.
2. Do not demand specific structural changes or missing chapters.
3. Do not use outside knowledge; use only supplied text and model scores.
4. Only assess whether Section B logically correlates with Section A.
5. Never change model scores; they come from the PyTorch model."""
PAIR_PROMPT = """Explain correlation between neighboring manuscript sections.
{rules}
In user-facing text, never mention the model, PyTorch, or how scores are produced. Express any score as a whole-number percentage (e.g. 80%), never a raw decimal.
Section A ({a}): {text_a}
Section B ({b}): {text_b}
Model scores (0 to 1): {scores}
Pair score for the Strong/Fair/Weak assessment (0 to 1): {pair_score}
Culprit sentences in A: {culprits_a}
Culprit sentences in B: {culprits_b}
Return JSON with exactly four top-level keys: coherence_scores (copy model scores),
highlighted_inconsistencies (array of objects with section, quote, what, why),
recommendations (array of objects with section, original_sentence, recommended_revision),
reference_list (array of objects with section, sentence).
Use exactly {a} or {b} as each section value. Quotes and original sentences must be copied verbatim from the named section. If no issue is supported, return empty arrays. Recommend only revisions justified by the supplied text.
Return one recommendation for EACH culprit sentence supplied above and EACH highlighted finding quote that verifies in its named section. Do not combine passages. Use the exact culprit or quote as original_sentence. Return all of these in this same JSON call.
A recommended revision must preserve the original sentence's meaning and repair only its logical link to the neighboring section; never adopt the neighboring section's topic."""
SUMMARY_PROMPT = """Summarize correlation of these neighboring manuscript pairs in one concise paragraph.
{rules}
Never mention the model, PyTorch, or how scores are produced. Express any score as a whole-number percentage (e.g. 80%), never a raw decimal.
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
        cleaned = plain_text(quote)
        match, ok = gemini.verify_quote(cleaned, source[section])
        return match if ok else None

    issues = []
    for item in raw.get("highlighted_inconsistencies", []) if isinstance(raw.get("highlighted_inconsistencies"), list) else []:
        if isinstance(item, dict):
            section = section_name(item.get("section"))
            if (section and item.get("what") != "No major inconsistencies found."
                    and all(isinstance(item.get(k), str) and item[k].strip() for k in ("what", "why"))):
                issues.append({"section": section, "quote": verified(section, item.get("quote")),
                               "what": plain_text(item["what"]), "why": plain_text(item["why"])})
    recommendations = []
    for item in raw.get("recommendations", []) if isinstance(raw.get("recommendations"), list) else []:
        if not isinstance(item, dict):
            continue
        section = section_name(item.get("section"))
        revision = item.get("recommended_revision")
        original = verified(section, item.get("original_sentence"))
        # shortcut: multiline quotes often join table cells, revisit with sentence spans if wrapped prose is lost.
        if (section and original and "\n" not in original and isinstance(revision, str) and revision.strip()
                and revision != "No major inconsistencies found."):
            recommendations.append({"section": section,
                                    "original_sentence": original,
                                    "recommended_revision": plain_text(revision)})
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
                                pair_score=scores.get("pair_score", scores["overall_coherence"]),
                                culprits_a=json.dumps(culprits_a), culprits_b=json.dumps(culprits_b))
    try:
        result = normalize_explanation(await _generate(prompt), scores, role_a, text_a, role_b, text_b)
    except Exception:
        logger.exception("v7 Gemini pair explanation failed for %s -> %s", role_a, role_b)
        result = normalize_explanation({}, scores, role_a, text_a, role_b, text_b)
    sources = {role_a: text_a, role_b: text_b}
    required = [(role_a, item.get("sentence")) for item in culprits_a if isinstance(item, dict)]
    required += [(role_b, item.get("sentence")) for item in culprits_b if isinstance(item, dict)]
    required += [(item["section"], item["quote"]) for item in result["highlighted_inconsistencies"]]
    unique = {}
    for item in result["recommendations"]:
        if item["original_sentence"] in sources[item["section"]]:
            unique.setdefault((item["section"], item["original_sentence"]), item)
    result["recommendations"] = list(unique.values())
    seen = {(item["section"], item["original_sentence"]) for item in result["recommendations"]}
    for section, sentence in required:
        if not isinstance(sentence, str) or not sentence or "\n" in sentence or sentence not in sources[section]:
            continue
        if (section, sentence) not in seen:
            # A missed Gemini item still gets a source-backed review action without inventing a revision.
            result["recommendations"].append({"section": section, "original_sentence": sentence,
                                              "recommended_revision": f"Review how this sentence connects to the neighboring section: {sentence}"})
            seen.add((section, sentence))
    return result


async def explain_summary(flow, findings):
    data = [{"order": p["order"], "section_a": p["section_a"], "section_b": p["section_b"],
             "scores": p["scores"], "pair_score": p["pair_score"]} for p in flow if p["pair_score"] is not None]
    prompt = SUMMARY_PROMPT.format(rules=STRICT_RULES, data=json.dumps({"pairs": data, "findings": findings}))
    try:
        raw = await _generate(prompt)
        summary = raw.get("summary") if isinstance(raw, dict) else None
        cleaned = plain_text(summary) if isinstance(summary, str) else ""
        return cleaned or None
    except Exception:
        logger.exception("v7 Gemini summary failed")
        return None
