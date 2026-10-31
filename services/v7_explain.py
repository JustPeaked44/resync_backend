"""Strict, torch-free Gemini explanations of v7-scored neighbor pairs."""

import asyncio
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
PAIR_PROMPT = """You are an objective logic auditor. Analyze Section A and Section B, two sections of the same manuscript.
If the provided Overall Coherence score is below 0.85, determine whether there is a REAL logical inconsistency between them. A low score alone is not an inconsistency.
{rules}
Write every user-facing sentence for an undergraduate student reading their own thesis: plain everyday words, short sentences, no technical or academic jargon. Always refer to the two sections by their real names, {a} and {b}, written like: the "{a}" section. Never write "Section A", "Section B", "A" or "B" in user-facing text, and say plainly what the student should look at or change.
In user-facing text, never mention the model, PyTorch, or how scores are produced. Express any score as a whole-number percentage (e.g. 80%), never a raw decimal.
{view_note}
Section A ({a}), {label_a}: {text_a}
Section B ({b}), {label_b}: {text_b}
Model scores (0 to 1): {scores}
Pair score for the Strong/Fair/Weak assessment (0 to 1): {pair_score}
Culprit sentences in A: {culprits_a}
Culprit sentences in B: {culprits_b}
Return JSON with exactly nine top-level keys: severity (exactly one of "Critical", "Moderate", "Minor", "None"), failed_dimension (exactly one of "objectives_alignment", "methodology_fit", "numeric_consistency", "claim_support", "none"), coherence_scores (copy model scores), link_summary (a string), comparison (an object with string keys a_says and b_says),
highlighted_inconsistencies (array of objects with section, quote, what, why),
recommendations (array of objects with section, original_sentence, issue, recommended_revision, category),
reference_list (array of objects with section, sentence),
bridges (array of objects with section, placement, sentence).
severity: assign "None" whenever the text flows logically and the second section addresses the goals the first one sets up. Assign "Critical", "Moderate" or "Minor" ONLY when there is a direct, undeniable contradiction or mismatch that you can quote from the supplied text, and you MUST quote it in highlighted_inconsistencies. "Critical" means the two sections directly contradict each other (quote both sides). "Moderate" means a specific claim, method, goal, or number in one section is clearly inconsistent with what the other section says or promises. "Minor" means a small but real discrepancy in a specific figure, term, or name between the two sections. A different topic, a missing transition, loose wording, or a low score is NOT an inconsistency and must be "None".
failed_dimension says which part of the paper this issue belongs to, so pick the single best fit: "objectives_alignment" (the text drifts from the purpose or aims the paper sets up), "methodology_fit" (how the work was done does not match how it is described elsewhere), "numeric_consistency" (numbers, data, or figures conflict or are missing), "claim_support" (a statement is not backed up by the surrounding text). Use "none" when severity is "None".
STRICT TRIAGE RULES: If you cannot quote a direct contradiction or mismatch, you MUST set severity to "None" and deduct nothing: return ["No major inconsistencies found."] for highlighted_inconsistencies and empty arrays for recommendations, reference_list, and bridges. A low score alone is not an issue: sections about different parts of a paper naturally score low. FALSE ALARM OVERRIDE: If the provided score is low, but the text actually aligns logically, you MUST return severity "None", output "No major inconsistencies found." in highlighted_inconsistencies, and set recommendations to []. Do not hallucinate issues just because the score is low. You are the ultimate judge of logic. If it makes sense, pass it.
Do not generate generic advice. Only give a recommendation that directly fixes an exact quote you found, and use that exact quote as its original_sentence. The culprit sentences below are only hints: use one only if it is part of a real issue.
comparison is a quick side-by-side for the student: a_says is what the "{a}" section says about the point where the two sections disagree, and b_says is what the "{b}" section says about that same point. Each is ONE short phrase of at most 15 words, concrete (keep key terms and numbers from the text), with no advice. If one section says nothing about the point, write "does not mention it".
link_summary is the one-line problem: at most 20 words, naming the mismatch between the two sections (for example: the "{a}" section promises X, but the "{b}" section never covers X). If they connect well, say so briefly. No advice or rewrite in link_summary.
For each recommendation, issue is the most important field: in one short sentence, name the specific inconsistency or missing link that this sentence creates between the two sections. Say what the sentence claims and what the other section says or lacks (for example: Section A promises X, but Section B only covers Y), quoting or closely paraphrasing both sides from the supplied text. Never describe the rewrite in issue, and never use vague wording such as "could be clearer" or "does not flow".
Keep issue to ONE sentence of at most 20 words. recommended_revision must be ONE sentence of similar length to the original sentence (at most 40 words) that changes only what is needed to repair the issue; do not add new claims or extra clauses.
recommended_revision then only repairs that issue. Recommendations that merely rephrase a sentence without a named issue are not allowed.
For each recommendation, category names the score part its revision fixes: objectives (alignment with aims), methodology (fit of methods), claims (support for claims), or numeric (consistency of figures). Use exactly one of these four values.
Use exactly {a} or {b} as each section value. Quotes and original sentences must be copied verbatim from the named section.
Recommend only revisions justified by the supplied text.
Give one recommendation for each exact quote in highlighted_inconsistencies that you can fix, and do not combine passages.
bridges are NEW linking sentences the student can ADD, the fastest way to connect two sections. If severity is not "None", return exactly TWO bridges, one on each side: first section is exactly {a} with placement "end" (added after its last sentence), then section is exactly {b} with placement "start" (added before its first sentence). Each sentence is ONE plain sentence of at most 30 words written as natural thesis prose that states the concrete content link between the two sections: name the specific things in one section that the other section depends on, builds on, or applies (for example: the tables defined here store the data entered through the screens for batches and listings). NEVER mention section names or write meta phrases such as "this section", "the next section", "the previous section", "as discussed above", or "building on". Use only topics, terms, and numbers that appear in the supplied text; never invent facts, results, or numbers, and never copy an existing sentence. If severity is "None", return an empty array. {bridges_note}
A recommended revision must preserve the original sentence's meaning and repair only the inconsistency you quoted; never adopt the other section's topic."""
SUMMARY_PROMPT = """Summarize how consistent these pairs of manuscript sections are in one concise paragraph.
{rules}
Never mention the model, PyTorch, or how scores are produced. Express any score as a whole-number percentage (e.g. 80%), never a raw decimal.
Use only these scored pairs and verified findings: {data}
Return JSON with one string key, summary. Do not invent evidence or alter scores."""


def coherence_scores(scores):
    return {"overall": scores["overall_coherence"], "objectives": scores["objectives_alignment"],
            "methodology": scores["methodology_fit"], "numeric": scores["numeric_consistency"],
            "claims": scores["claim_support"]}


EXPLAIN_ATTEMPTS = 3
EXPLAIN_RETRY_PAUSE = 1.5
META_PHRASE = re.compile(r"\b(this section|the next section|the previous section|the following section|as discussed above|building on)\b", re.I)
SEVERITIES = ("Critical", "Moderate", "Minor", "None")
DIMENSION_KEYS = ("objectives_alignment", "methodology_fit", "numeric_consistency", "claim_support")
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


def normalize_explanation(raw, scores, role_a, text_a, role_b, text_b, adjacent=True):
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
                and revision != "No major inconsistencies found."
                and " ".join(plain_text(revision).split()) != " ".join(original.split())):  # a "revision" that changes nothing is noise
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
    bridges = []
    for item in raw.get("bridges", []) if isinstance(raw.get("bridges"), list) else []:
        if not isinstance(item, dict):
            continue
        section, sentence = section_name(item.get("section")), item.get("sentence")
        if (section and item.get("placement") in ("start", "end") and isinstance(sentence, str) and sentence.strip()
                and not META_PHRASE.search(sentence)):
            bridges.append({"section": section, "placement": item["placement"], "sentence": friendly(sentence)})
    severity = raw.get("severity").strip().capitalize() if isinstance(raw.get("severity"), str) else None
    quoted = any(item["quote"] for item in issues) or bool(recommendations)   # verified lines from the paper
    if severity not in SEVERITIES:  # no valid label: a quoted issue or recommendation means a real problem was found
        severity = "Moderate" if quoted else "None"
    if severity != "None" and not quoted:  # strict rule: no issue without a quotable contradiction
        severity = "None"
    if severity == "None":  # triage: no real inconsistency means no findings, no advice and no penalty
        issues, recommendations, references, bridges = [], [], [], []
    bridges = bridges[:2] if adjacent else []   # a linking sentence only makes sense between neighboring sections
    allowed = [key for key in DIMENSION_KEYS if key != "numeric_consistency" or scores.get("numeric_applicable")]
    # Gemini's own pick flips between runs on near-ties, so the issue is charged to the part the model rated weakest for
    # this pair: the same answer every scan, and it matches the numbers shown in the score boxes.
    failed = "none" if severity == "None" else min(allowed, key=lambda key: scores[key])
    summary = raw.get("link_summary")
    comparison = raw.get("comparison") if isinstance(raw.get("comparison"), dict) else {}
    return {"severity": severity, "failed_dimension": failed, "coherence_scores": coherence_scores(scores), "link_summary": friendly(summary) if isinstance(summary, str) else "",
            "comparison": {key: friendly(comparison[key]) if isinstance(comparison.get(key), str) else ""
                           for key in ("a_says", "b_says")},
            "highlighted_inconsistencies": issues,
            "recommendations": recommendations, "reference_list": references, "bridges": bridges}


async def _generate(prompt):
    payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
               "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}}
    response = await gemini.post_gemini_with_retry(payload)
    response.raise_for_status()
    return json.loads(response.json()["candidates"][0]["content"]["parts"][0]["text"])


async def explain_pair(role_a, text_a, role_b, text_b, scores, culprits_a, culprits_b, adjacent=True):
    if adjacent:   # neighbors connect at their boundary: the end of the first section and the start of the next
        view = {"view_note": "You only see the END of Section A and the START of Section B, because that is where the two sections connect. Judge whether Section B follows from what Section A ends with.",
                "label_a": "the end of that section", "label_b": "the start of that section", "bridges_note": "",
                "text_a": gemini.truncate_tail(text_a), "text_b": gemini.truncate_text(text_b, 3500)}
    else:          # far-apart sections: the start of each carries its goals and topic
        view = {"view_note": "These two sections are NOT neighbors in the manuscript, and you see the START of each. Judge whether they are consistent with each other, and whether the later section carries through the goals, methods, and claims the earlier one sets up (for example: whether the methodology, results, and conclusions match the stated objectives).",
                "label_a": "the start of that section", "label_b": "the start of that section",
                "bridges_note": "These two sections are not neighbors, so bridges MUST be an empty array.",
                "text_a": gemini.truncate_text(text_a, 3500), "text_b": gemini.truncate_text(text_b, 3500)}
    prompt = PAIR_PROMPT.format(rules=STRICT_RULES, a=role_a, b=role_b, **view,
                                scores=json.dumps(coherence_scores(scores)),
                                pair_score=scores.get("pair_score", scores["overall_coherence"]),
                                culprits_a=json.dumps(culprits_a), culprits_b=json.dumps(culprits_b))
    result = None
    for attempt in range(1, EXPLAIN_ATTEMPTS + 1):  # Gemini often returns a transient 429/503 under load
        try:
            result = normalize_explanation(await _generate(prompt), scores, role_a, text_a, role_b, text_b, adjacent)
            break
        except gemini.GeminiQuotaExhausted:
            logger.warning("v7 Gemini quota exhausted; %s -> %s left unreviewed", role_a, role_b)
            break  # no point retrying until the quota resets
        except Exception:
            logger.exception("v7 Gemini pair explanation failed for %s -> %s (attempt %s of %s)", role_a, role_b, attempt, EXPLAIN_ATTEMPTS)
            if attempt < EXPLAIN_ATTEMPTS:
                await asyncio.sleep(EXPLAIN_RETRY_PAUSE * attempt)
    if result is None:
        result = normalize_explanation({}, scores, role_a, text_a, role_b, text_b)
        result["severity"] = None  # unknown, so the pair is neither cleared nor penalized
        result["failed_dimension"] = None
    sources = {role_a: text_a, role_b: text_b}
    unique = {}
    for item in result["recommendations"]:
        if item["original_sentence"] in sources[item["section"]]:
            unique.setdefault((item["section"], item["original_sentence"]), item)
    result["recommendations"] = list(unique.values())
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
