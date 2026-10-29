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
Write every user-facing sentence for an undergraduate student reading their own thesis: plain everyday words, short sentences, no technical or academic jargon. Always refer to the two sections by their real names, {a} and {b}, written like: the "{a}" section. Never write "Section A", "Section B", "A" or "B" in user-facing text, and say plainly what the student should look at or change.
In user-facing text, never mention the model, PyTorch, or how scores are produced. Express any score as a whole-number percentage (e.g. 80%), never a raw decimal.
Section A ({a}): {text_a}
Section B ({b}): {text_b}
Model scores (0 to 1): {scores}
Pair score for the Strong/Fair/Weak assessment (0 to 1): {pair_score}
Culprit sentences in A: {culprits_a}
Culprit sentences in B: {culprits_b}
Return JSON with exactly six top-level keys: coherence_scores (copy model scores), link_summary (a string), comparison (an object with string keys a_says and b_says),
highlighted_inconsistencies (array of objects with section, quote, what, why),
recommendations (array of objects with section, original_sentence, issue, recommended_revision, category),
reference_list (array of objects with section, sentence).
comparison is a quick side-by-side for the student: a_says is what the "{a}" section says about the point where the two sections disagree, and b_says is what the "{b}" section says about that same point. Each is ONE short phrase of at most 15 words, concrete (keep key terms and numbers from the text), with no advice. If one section says nothing about the point, write "does not mention it".
link_summary is the one-line problem: at most 20 words, naming the mismatch between the two sections (for example: the "{a}" section promises X, but the "{b}" section never covers X). If they connect well, say so briefly. No advice or rewrite in link_summary.
For each recommendation, issue is the most important field: in one short sentence, name the specific inconsistency or missing link that this sentence creates between the two sections. Say what the sentence claims and what the other section says or lacks (for example: Section A promises X, but Section B only covers Y), quoting or closely paraphrasing both sides from the supplied text. Never describe the rewrite in issue, and never use vague wording such as "could be clearer" or "does not flow".
Keep issue to ONE sentence of at most 20 words. recommended_revision must be ONE sentence of similar length to the original sentence (at most 40 words) that changes only what is needed to repair the issue; do not add new claims or extra clauses.
recommended_revision then only repairs that issue. Recommendations that merely rephrase a sentence without a named issue are not allowed.
For each recommendation, category names the score part its revision fixes: objectives (alignment with aims), methodology (fit of methods), claims (support for claims), or numeric (consistency of figures). Use exactly one of these four values.
Use exactly {a} or {b} as each section value. Quotes and original sentences must be copied verbatim from the named section. If the provided Overall Coherence score is less than 0.85, you MUST generate at least one recommendation. Even if there are no direct contradictions, recommend how the student can better align or transition the text between these two sections.
If no issue is supported and the Overall Coherence score is 0.85 or higher, return empty arrays. Recommend only revisions justified by the supplied text.
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


CATEGORY_SCORES = {"objectives": "objectives_alignment", "methodology": "methodology_fit",
                   "claims": "claim_support", "numeric": "numeric_consistency"}
CATEGORY_ALIASES = {"claim": "claims", "claim_support": "claims", "numbers": "numeric",
                    "numeric_consistency": "numeric", "objectives_alignment": "objectives",
                    "methodology_fit": "methodology"}


def recommendation_category(value, scores):
    category = value.strip().lower() if isinstance(value, str) else ""
    category = CATEGORY_ALIASES.get(category, category)
    if category in CATEGORY_SCORES:
        return category
    applicable = (key for key in CATEGORY_SCORES if key != "numeric" or scores.get("numeric_applicable"))
    return min(applicable, key=lambda key: scores[CATEGORY_SCORES[key]])


def normalize_explanation(raw, scores, role_a, text_a, role_b, text_b):
    raw = raw if isinstance(raw, dict) else {}
    source = {role_a: text_a, role_b: text_b}
    aliases = {"Section A": role_a, "Section B": role_b,
               f"Section A ({role_a})": role_a, f"Section B ({role_b})": role_b}

    def friendly(value):
        """Plain text with any 'Section A/B' wording swapped for the real section names."""
        text = plain_text(value)
        for label, name in (("Section A", role_a), ("Section B", role_b)):
            text = text.replace(f"{label} ({name})", label)
            text = re.sub(rf"\b{label}\b", f'the "{name}" section', text, flags=re.I)
        return re.sub(r'^the "', 'The "', text)

    def section_name(value):
        if not isinstance(value, str):
            return None
        return value if value in source else aliases.get(value)

    def verified(section, quote):
        if not isinstance(section, str) or section not in source or not isinstance(quote, str) or not quote.strip():
            return None
        cleaned = plain_text(quote)
        _, ok = gemini.verify_quote(cleaned, source[section])
        if not ok:
            return None
        pattern = re.sub(r"(?:\\ )+", lambda _: r"\s+", re.escape(cleaned))
        pattern = pattern.replace("'", "['‘’]").replace('"', '["“”]')
        match = re.search(pattern, source[section], flags=re.I)
        return match.group() if match else None

    issues = []
    for item in raw.get("highlighted_inconsistencies", []) if isinstance(raw.get("highlighted_inconsistencies"), list) else []:
        if isinstance(item, dict):
            section = section_name(item.get("section"))
            if (section and item.get("what") != "No major inconsistencies found."
                    and all(isinstance(item.get(k), str) and item[k].strip() for k in ("what", "why"))):
                issues.append({"section": section, "quote": verified(section, item.get("quote")),
                               "what": friendly(item["what"]), "why": friendly(item["why"])})
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
                                    "recommended_revision": friendly(revision),
                                    "issue": friendly(item["issue"]) if isinstance(item.get("issue"), str) else "",
                                    "category": recommendation_category(item.get("category"), scores)})
    references = []
    for item in raw.get("reference_list", []) if isinstance(raw.get("reference_list"), list) else []:
        if isinstance(item, dict):
            section = section_name(item.get("section"))
            quote = verified(section, item.get("sentence"))
            if quote:
                references.append({"section": section, "sentence": quote})
    summary = raw.get("link_summary")
    comparison = raw.get("comparison") if isinstance(raw.get("comparison"), dict) else {}
    return {"coherence_scores": coherence_scores(scores), "link_summary": friendly(summary) if isinstance(summary, str) else "",
            "comparison": {key: friendly(comparison[key]) if isinstance(comparison.get(key), str) else ""
                           for key in ("a_says", "b_says")},
            "highlighted_inconsistencies": issues,
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
                                              "category": recommendation_category(None, scores), "issue": "",
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
