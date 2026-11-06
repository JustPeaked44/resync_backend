"""Pure assembly helpers for the v7 manuscript flow."""

import logging
import math
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
MIN_HALF_CHECKS = 3  # a kind of check with fewer than this is too small to weigh as its own half: one miss would swing the category
CHECK_PASS_AT = 0.5  # a neighboring pair rated below this for a category fails that check (a loose link)
# Internal score keys (as the model and Gemini name them) -> the names the API and UI show.
DIMENSION_NAMES = {"objectives_alignment": "context_alignment", "methodology_fit": "operational_alignment",
                   "numeric_consistency": "evidence_consistency", "claim_support": "logical_support"}
RECOMMENDATION_THRESHOLD = 0.85
EXPLAIN_LIMIT = int(os.getenv("EXPLAIN_LIMIT", "0")) or None  # a cap on reviewed pairs (weakest first); 0 or unset: every weak pair
EXPLAIN_CONCURRENCY = int(os.getenv("EXPLAIN_CONCURRENCY", "8"))  # single-pair fallback reviews running at the same time
EXPLAIN_TIMEOUT = 240  # seconds one pair may take (waiting for a rate-limit slot and all retries included)
BRIDGE_ROUNDS = 3  # how many times pairs still missing linking sentences are asked about
MIN_SECTION_WORDS = 5  # a heading with no text under it cannot be rated, so neighbors are paired across it


def has_figures(text):
    text = re.sub(r"https?://\S+|\bdoi\s*:\s*\S+|\b10\.\d{4,9}/\S+", " ", text, flags=re.I)
    text = re.sub(r"\([^()]*\b(?:19|20)\d{2}[a-z]?\s*\)", " ", text)
    text = re.sub(r"\b(?:[A-Z][A-Za-z-]+(?:\s+et\s+al\.)?|et\s+al\.),\s*(?:19|20)\d{2}[a-z]?\b", " ", text)
    text = re.sub(r"\b(?:figure|fig\.?|table|chapter|section|page|pp?\.)\s*\d+(?:\.\d+)*\b", " ", text, flags=re.I)
    text = re.sub(r"(?m)^\s*(?:\d+(?:\.\d+)*[.)]\s+|\d+\s*$)", " ", text)
    # citation years and numbered references such as [12] are not data
    text = re.sub(r"\b(?:19|20)\d{2}[a-z]?\b|\[\d+(?:\s*[,–-]\s*\d+)*\]", " ", text)
    # a figure is a percentage, a decimal, a number of two or more digits, or "n = 5": a lone digit is usually a list number
    return bool(re.search(r"\d+(?:[.,]\d+)?\s*%|(?<![\w.])\d+\.\d+\b|(?<![\w.])\d{2,}\b|\bn\s*=\s*\d+", text, flags=re.I))


FIGURE_WINDOW_CHARS = 1800   # a little more than the 170 words the model reads at the edge of a section


def figure_windows(sections, index_a, index_b):
    """The text the model really compares for a pair, which is where figures must be for the numbers to be rated: the end
    of the first section against the start of the second for neighbors, the starts of both otherwise."""
    first, second = sections[index_a]["text"] or "", sections[index_b]["text"] or ""
    return (first[-FIGURE_WINDOW_CHARS:] if index_b == index_a + 1 else first[:FIGURE_WINDOW_CHARS]), second[:FIGURE_WINDOW_CHARS]


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
    """(overall score, dimension breakdown), counted as checks passed.

    A check is one pair of sections judged for one of the four categories (Evidence Consistency only for pairs where both
    sections hold figures). A check fails when the AI review quoted a real contradiction in that category, or when two
    neighboring sections are rated below CHECK_PASS_AT for it (a loose link, which gets linking-sentence suggestions).
    Far-apart pairs fail only on a quoted contradiction, since unrelated chapters are meant to differ; a pair the AI
    review could not judge falls back to the model's rating.

    A category has two kinds of checks: the flow between neighboring sections, and the cross-chapter pairs that should
    agree. When both kinds have at least MIN_HALF_CHECKS checks, each gets its own pass rate and the category's score is the
    average of the two, so the many far-apart pairs that almost always pass can never hide weak flow. With fewer checks than
    that the two are pooled, since one miss would swing the category by a quarter. "N/A" when a category has none. The overall score is the average of the applicable categories' percentages, rounded up, so every category carries
    equal weight. `failures` lists exactly which checks failed and why, so every score can be traced. `transitions` is
    accepted for older callers and no longer used.
    """
    scored = [p for p in pairs if p["scores"]["overall_coherence"] is not None]
    breakdown = {}
    for key, name in DIMENSION_NAMES.items():
        failures = []
        counts = {"flow": [0, 0], "cross": [0, 0]}   # kind -> [checks, passed]
        for p in scored:
            rating = p["scores"].get(key)
            if rating is None or (key == "numeric_consistency" and not p.get("numeric_applicable", True)):
                continue
            severity, neighbors = p.get("severity"), p.get("adjacent", True) is not False
            kind = counts["flow" if neighbors else "cross"]
            kind[0] += 1
            if severity in ("Critical", "Moderate", "Minor") and p.get("failed_dimension") == key:
                reason = "contradiction"
            elif rating < CHECK_PASS_AT and (neighbors or severity is None):
                reason = "loose_link"
            else:
                kind[1] += 1
                continue
            failures.append({"order": p.get("order"), "section_a": p.get("section_a", ""), "section_b": p.get("section_b", ""),
                             "reason": reason, "severity": severity if reason == "contradiction" else None,
                             "rating": round(rating, 4)})
        total = counts["flow"][0] + counts["cross"][0]
        if not total:
            breakdown[name] = {"score": "N/A", "halves": False, "items": 0, "passed": 0, "issues": 0, "pairs": 0, "failures": [],
                               "flow": {"items": 0, "passed": 0}, "cross": {"items": 0, "passed": 0},
                               "baseline": None, "penalty": 0, "deductions": []}
            continue
        halves = counts["flow"][0] >= MIN_HALF_CHECKS and counts["cross"][0] >= MIN_HALF_CHECKS
        rates = [passed / items for items, passed in counts.values() if items]
        score = sum(rates) / len(rates) if halves else (counts["flow"][1] + counts["cross"][1]) / total   # small samples are pooled, not split
        breakdown[name] = {"score": round(score * 100, 2), "halves": halves, "items": total,
                           "passed": counts["flow"][1] + counts["cross"][1],
                           "issues": sum(f["reason"] == "contradiction" for f in failures), "pairs": total,
                           "failures": failures,
                           "flow": {"items": counts["flow"][0], "passed": counts["flow"][1]},
                           "cross": {"items": counts["cross"][0], "passed": counts["cross"][1]},
                           "baseline": None, "penalty": 0, "deductions": []}
    values = [entry["score"] for entry in breakdown.values() if entry["score"] != "N/A"]
    return (float(math.ceil(round(sum(values) / len(values), 6))) if values else 0.0), breakdown


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


def comparable_sections(parsed_sections, section_order, section_roles):
    """The sections a scan compares, in reading order: repeated headings merged, reference lists and empty headings left out."""
    return [section for section in merge_repeated_sections(parsed_sections, section_order)
            if section_roles.get(section["name"]) != "references" and len(section["text"].split()) >= MIN_SECTION_WORDS]


def select_worst_pairs(pairs, limit=12):   # limit=None keeps every pair
    return sorted((p for p in pairs if p["pair_score"] is not None),
                  key=lambda p: (p["pair_score"], p["order"]))[:limit]


# The far-apart sections that matter most, in the order they are looked at. Each is reviewed on its own, because a
# contradiction between two sections on the same topic is found far more reliably one pair at a time than in a batch.
FAR_REVIEW_LIMIT = int(os.getenv("FAR_REVIEW_LIMIT", "8"))
FAR_REVIEW_ORDER = (("objectives", "conclusion"), ("objectives", "results"), ("methodology", "results"),
                    ("methodology", "conclusion"), ("objectives", "methodology"), ("introduction", "conclusion"),
                    ("abstract", "conclusion"), ("abstract", "results"), ("objectives", "discussion"), ("methodology", "discussion"))


def main_far_pairs(pairs, sections, roles=None):
    """The pairs joining the sections that must agree however far apart they sit: for each kind (objectives with
    conclusion, methodology with results, ...) the pair made of the section that carries that role's name, or else the
    first section with that role."""
    if sections is None:
        return []
    role_of = lambda index: (roles or {}).get(sections[index]["name"]) or sections[index].get("role")

    def pick(role):
        indexes = [i for i in range(len(sections)) if role_of(i) == role]
        named = [i for i in indexes if role in sections[i]["name"].lower() or sections[i]["name"].lower() in role]
        return (named or indexes or [None])[0]

    by_indexes = {(p["index_a"], p["index_b"]): p for p in pairs if p["scores"]["overall_coherence"] is not None}
    chosen = []
    for first, second in FAR_REVIEW_ORDER:
        i, j = pick(first), pick(second)
        if i is None or j is None or i == j:
            continue
        pair = by_indexes.get((min(i, j), max(i, j)))
        if pair is not None and not pair.get("adjacent") and pair not in chosen:
            chosen.append(pair)
    return chosen[:FAR_REVIEW_LIMIT]


def select_pairs_to_explain(pairs, sections=None, roles=None):
    """Every pair that is below the recommendation threshold gets a real explanation, weakest first, so
    advice is equally specific across the whole manuscript. A cap applies only if EXPLAIN_LIMIT is set.

    The main far-apart pairs that must agree (objectives and conclusion, methodology and results) are always reviewed,
    even when the model rates them as similar: two sections on one topic look alike to the model while saying opposite
    things, and those contradictions are exactly what a reviewer needs to hear about."""
    always = {id(p) for p in main_far_pairs(pairs, sections, roles)}
    weak = [p for p in pairs if p["scores"]["overall_coherence"] is not None
            and (p["scores"]["overall_coherence"] < RECOMMENDATION_THRESHOLD or id(p) in always)]
    return select_worst_pairs(weak, EXPLAIN_LIMIT)


# The model's rating of a weak pair sets its group: Gemini is asked about every pair of a group in ONE call.
CRITICAL_BELOW = 0.40
MODERATE_BELOW = 0.65     # Minor runs from here up to RECOMMENDATION_THRESHOLD
BATCH_MAX = 12            # a group larger than this is split, so one answer never grows too long to read


def severity_batches(pairs):
    """[(tier, pairs)] for the pairs to review: Critical (< 0.40), Moderate (0.40 to 0.65) and Minor (0.65 to 0.85), weakest
    first. One Gemini call per group, or per BATCH_MAX pairs when a group is large."""
    tiers = (("Critical", lambda o: o < CRITICAL_BELOW), ("Moderate", lambda o: CRITICAL_BELOW <= o < MODERATE_BELOW),
             ("Minor", lambda o: o >= MODERATE_BELOW))
    batches = []
    for name, belongs in tiers:
        group = sorted((p for p in pairs if belongs(p["scores"]["overall_coherence"])), key=lambda p: p["scores"]["overall_coherence"])
        batches.extend((name, group[start:start + BATCH_MAX]) for start in range(0, len(group), BATCH_MAX))
    return batches


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
        applicable = numeric_applicable(*figure_windows(sections, index_a, index_b)) if scores else False
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


# ---- Counts that two sections report differently --------------------------------------------------------------------
# The AI compares the end of one section with the start of the next, so a figure buried in the middle of a long section
# (how many people answered a survey) can be contradicted elsewhere without the model ever seeing both. Headcounts are
# simple enough to compare directly over the whole text of both sections.
_COUNT_GROUPS = {"respondent": "respondents", "participant": "respondents", "subject": "respondents", "student": "respondents",
                 "sample": "respondents", "user": "users", "expert": "experts", "panelist": "experts", "teacher": "teachers",
                 "employee": "employees", "patient": "patients", "people": "people", "person": "people"}
_COUNT_RE = re.compile(r"(?<![\w.,/-])(\d{1,3}(?:,\d{3})+|\d{2,5})(?!\s*%|\d|[.,]\d)\s+(?:[A-Za-z-]+\s+){0,2}?(respondent|participant|subject|student|"
                       r"sample|user|expert|panelist|teacher|employee|patient|people|person)s?\b", re.I)
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_SIDE_GROUP_RE = re.compile(r"\b(pilot|preliminary|pre-?test|pre-?survey|trial|focus group|initial)\b", re.I)


def _counts_in(text):
    """{group: {number: first sentence stating it}} for the headcounts a section states."""
    found = {}
    for sentence in _SENTENCE_RE.split(text or ""):
        if _SIDE_GROUP_RE.search(sentence):   # a pilot or a focus group is a different group of people, not the main sample
            continue
        for match in _COUNT_RE.finditer(sentence):
            number = int(match.group(1).replace(",", ""))
            if number < 10 or ("," not in match.group(1) and 1900 <= number <= 2100):   # small numbers and years
                continue
            found.setdefault(_COUNT_GROUPS[match.group(2).lower()], {}).setdefault(number, sentence.strip())
    return found


def figure_conflict(text_a, text_b):
    """(group, number_a, sentence_a, number_b, sentence_b) when each section gives ONE headcount for the same kind of
    people and the two differ; None otherwise. A section that mentions several different counts is left alone."""
    counts_a, counts_b = _counts_in(text_a), _counts_in(text_b)
    for group in counts_a.keys() & counts_b.keys():
        if len(counts_a[group]) == 1 and len(counts_b[group]) == 1:
            (number_a, sentence_a), = counts_a[group].items()
            (number_b, sentence_b), = counts_b[group].items()
            if number_a != number_b:
                return group, number_a, sentence_a, number_b, sentence_b
    return None


def apply_figure_conflicts(flow, sections):
    """Adds a quoted finding and a one-sentence fix for every pair whose sections report different headcounts."""
    rank = {None: -1, "None": 0, "Minor": 1, "Moderate": 2, "Critical": 3}
    for pair in flow:
        if pair["scores"]["overall_coherence"] is None:
            continue
        a, b = sections[pair["index_a"]], sections[pair["index_b"]]
        conflict = figure_conflict(a["text"], b["text"])
        if not conflict:
            continue
        group, number_a, sentence_a, number_b, sentence_b = conflict
        if sentence_a not in a["text"] or sentence_b not in b["text"]:
            continue
        written_b = f"{number_b:,}" if f"{number_b:,}" in sentence_b else str(number_b)
        written_a = f"{number_a:,}" if "," in written_b else str(number_a)
        fixed = sentence_b.replace(written_b, written_a, 1)
        explanation = pair["explanation"] or {
            "severity": "None", "failed_dimension": "none", "coherence_scores": {}, "link_summary": "",
            "comparison": {"a_says": "", "b_says": ""}, "highlighted_inconsistencies": [], "recommendations": [],
            "reference_list": [], "bridges": []}
        explanation["highlighted_inconsistencies"].append({
            "section": b["name"], "quote": sentence_b, "what": f"Different number of {group}",
            "why": f"{a['name'].title()} says {number_a:,} {group}, but {b['name'].title()} says {number_b:,}."})
        explanation["reference_list"].append({"section": a["name"], "sentence": sentence_a})
        explanation["recommendations"].append({
            "section": b["name"], "original_sentence": sentence_b, "recommended_revision": fixed, "kind": "edit",
            "issue": f"This section reports {number_b:,} {group} but {a['name']} reports {number_a:,}. Use the correct number in both places.",
            "category": "evidence"})
        if rank.get(explanation.get("severity"), 0) < rank["Moderate"]:
            explanation["severity"] = "Moderate"
        explanation["failed_dimension"] = "numeric_consistency"
        pair["numeric_applicable"] = True
        pair["explanation"] = explanation


async def explain_flow(sections, flow, roles=None):
    from services import gemini
    from services.v7_explain import (FLOW_SUGGESTION_BELOW, explain_batch, explain_pair, explain_summary, pair_cache_key,
                                     suggest_bridges_batch)
    from services.verdict_cache import VerdictCache

    semaphore = asyncio.Semaphore(EXPLAIN_CONCURRENCY)
    log = logging.getLogger("resync.v7_scan")

    def scores_of(pair):
        return dict(pair["scores"], pair_weight=pair["pair_weight"], pair_score=pair["pair_score"],
                    numeric_applicable=pair["numeric_applicable"])

    async def explain(pair):
        async with semaphore:
            index_a, index_b = pair["index_a"], pair["index_b"]
            culprits = pair["culprit_sentences"]
            try:
                pair["explanation"] = await asyncio.wait_for(explain_pair(
                    pair["section_a"], sections[index_a]["text"], pair["section_b"], sections[index_b]["text"],
                    scores_of(pair), culprits["section_a"], culprits["section_b"], adjacent=pair["adjacent"]), EXPLAIN_TIMEOUT)
            except asyncio.TimeoutError:  # never let one slow pair hold the whole scan at 99%
                log.warning("Pair %s took over %ss; leaving it unreviewed", pair["order"], EXPLAIN_TIMEOUT)

    def batch_items(group):
        return [{"id": pair["order"], "role_a": pair["section_a"], "text_a": sections[pair["index_a"]]["text"],
                 "role_b": pair["section_b"], "text_b": sections[pair["index_b"]]["text"], "scores": scores_of(pair),
                 "culprits_a": pair["culprit_sentences"]["section_a"], "culprits_b": pair["culprit_sentences"]["section_b"],
                 "adjacent": pair["adjacent"]} for pair in group]

    async def review_batch(group):
        """One Gemini call for a whole severity group. Pairs it does not answer are reviewed one by one afterwards."""
        try:
            answers = await asyncio.wait_for(explain_batch(batch_items(group)), EXPLAIN_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("A batch of %s pairs took over %ss; reviewing them one by one", len(group), EXPLAIN_TIMEOUT)
            return
        for pair in group:
            if pair["order"] in answers:
                pair["explanation"] = answers[pair["order"]]

    async def second_opinion(group):
        """A contradiction only counts when a second review of just these pairs also finds one."""
        try:
            answers = await asyncio.wait_for(explain_batch(batch_items(group)), EXPLAIN_TIMEOUT)
        except asyncio.TimeoutError:
            return   # not confirmed, not denied: the first verdict stands
        for pair in group:
            second = answers.get(pair["order"])
            if second is not None and second["severity"] == "None":
                pair["explanation"] = second

    to_explain = select_pairs_to_explain(flow, sections, roles)
    # A pair whose text the AI has already judged keeps that verdict, so the same paper always gets the same score, and the
    # AI is only asked about pairs it has not seen (see verdict_cache).
    keys = {pair["order"]: pair_cache_key(pair["section_a"], sections[pair["index_a"]]["text"], pair["section_b"],
                                           sections[pair["index_b"]]["text"], scores_of(pair), pair["adjacent"]) for pair in to_explain}
    remembered = await VerdictCache.get_many(keys.values())
    for pair in to_explain:
        if keys[pair["order"]] in remembered:
            pair["explanation"] = remembered[keys[pair["order"]]]
    to_review = [pair for pair in to_explain if pair["explanation"] is None]
    batches = severity_batches(to_review)
    gemini.expect_calls(len(batches) + 1)  # one call per severity group plus the summary, so scan polls can say when the AI is busy
    try:
        await asyncio.gather(*(review_batch(group) for _, group in batches), return_exceptions=True)  # one bad batch never cancels the rest
        # Pairs a batch left out (or whose batch failed) get a review of their own, so nothing is skipped silently.
        missing = [pair for pair in to_explain if pair["explanation"] is None]
        if missing:
            gemini.expect_calls(len(missing))
            await asyncio.gather(*(explain(pair) for pair in missing), return_exceptions=True)
        # The main far-apart pairs get a review of their own whatever the batch said, so a contradiction is not lost in a group.
        singles = [pair for pair in main_far_pairs(flow, sections, roles) if pair in to_review
                   and (pair["explanation"] or {}).get("severity") in (None, "None")]
        if singles:
            gemini.expect_calls(len(singles))
            await asyncio.gather(*(explain(pair) for pair in singles), return_exceptions=True)
        # Gemini fails more under load (429/503), so pairs it could not judge get one calmer, one-at-a-time second pass.
        for pair in to_explain:
            if pair["explanation"] and pair["explanation"]["severity"] is None:
                await explain(pair)
        # A one-off answer on a borderline pair must not move the score from one scan to the next, so every contradiction
        # found this time is put to a second review of just those pairs; if that one finds nothing, the pair is clear.
        flagged = [pair for pair in to_review if pair["explanation"] and pair["explanation"]["severity"] in ("Critical", "Moderate", "Minor")]
        if flagged:
            rechecks = severity_batches(flagged)
            gemini.expect_calls(len(rechecks))
            await asyncio.gather(*(second_opinion(group) for _, group in rechecks), return_exceptions=True)
        # A loosely linked neighbor pair always gets linking sentences to add: when the main review wrote none, ask for
        # just those (all pairs in ONE call), so a score that lost points to weak links never comes with an empty list.
        loose = [p for p in flow if p.get("adjacent", True) and p["explanation"] and p["explanation"].get("severity") == "None"
                 and len(p["explanation"].get("bridges") or []) < 2 and (p["scores"]["overall_coherence"] or 1) < FLOW_SUGGESTION_BELOW]
        for _ in range(BRIDGE_ROUNDS):   # pairs the request missed or whose sentences were already in the paper are asked again
            pending = [p for p in loose if len(p["explanation"].get("bridges") or []) < 2]
            if not pending:
                break
            gemini.expect_calls(1)
            try:
                found = await asyncio.wait_for(suggest_bridges_batch(
                    [{"id": p["order"], "role_a": p["section_a"], "text_a": sections[p["index_a"]]["text"],
                      "role_b": p["section_b"], "text_b": sections[p["index_b"]]["text"]} for p in pending]), EXPLAIN_TIMEOUT)
            except asyncio.TimeoutError:
                found = {}
                log.warning("Linking sentences for %s pairs took over %ss; skipped", len(pending), EXPLAIN_TIMEOUT)
            for pair in pending:
                pair["explanation"]["bridges"] = found.get(pair["order"]) or pair["explanation"].get("bridges") or []
        # Remember what was just decided, linking sentences included, for the next scan of the same text.
        decided = [pair for pair in to_explain if pair["explanation"] and (pair in to_review or pair in loose)]
        await VerdictCache.save_many({keys[pair["order"]]: pair["explanation"] for pair in decided})
        apply_figure_conflicts(flow, sections)
        # The pair review sees where sections meet; this reads every section in full and compares all of it (full_check).
        from services.full_check import apply_full_check
        await apply_full_check(flow, sections, roles)
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
        review_status = {"status": "complete" if not unreviewed else "partial", "unreviewed": unreviewed}
        if unreviewed and gemini.rate_limited():
            review_status.update(reason="rate_limited", message="Some AI reviews could not finish because the AI's temporary limit was reached. This score is provisional; try scanning again later.")
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
