"""A check that reads the WHOLE manuscript, not just where sections meet.

The pair review (v7_scan.explain_flow) looks at the end of one section and the start of the next, so a statement buried in
the middle of a long section can disagree with another part without the AI ever seeing both. This step covers the rest:

1. Every section is read in full, a piece at a time, and the statements other parts could contradict are listed with the
   sentence they came from (objectives, methods, numbers, claims, key terms). A sentence the AI "quotes" that is not really
   in the text is thrown away.
2. All those statements are compared against each other in one go, across the whole paper, for mismatches.
3. Each suspected mismatch is put to a second review that sees the full paragraphs around both sentences, and only
   confirmed ones are kept, together with a minimal corrected sentence.

Confirmed findings join the normal report: they are added to the pair of sections they concern (a pair is created when the
local model had not kept it) with quotes the student can find, so they are highlighted and scored like any other.
Every Gemini answer is remembered by a fingerprint of what it was shown, so the same paper gives the same findings. The
step never fails a scan: any problem leaves the report exactly as the pair review made it.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

from services import gemini, v7_explain
from services.verdict_cache import VerdictCache

logger = logging.getLogger("resync.full_check")

VERSION = "1"                      # bump when the prompts below change, so remembered answers are not reused
CHUNK_CHARS = int(os.getenv("FULL_CHECK_CHUNK_CHARS", "12000"))
MAX_CHUNK_CALLS = int(os.getenv("FULL_CHECK_MAX_CALLS", "80"))   # a safety stop for a huge paper
CONCURRENCY = 6
MAX_FACTS_PER_CHUNK = max(30, round(30 * CHUNK_CHARS / 12000))   # a bigger piece may list proportionally more statements
LEDGER_SPLIT_CHARS = 120_000       # a longer list of statements is compared in two calls
MAX_CONFLICTS = 12
MIN_SECTION_WORDS = 15
KINDS = ("objective", "method", "number", "claim", "term")
SEVERITIES = ("Critical", "Moderate", "Minor")
CATEGORY_DIMENSION = {"objectives": "objectives_alignment", "methodology": "methodology_fit", "claims": "claim_support",
                      "numeric": "numeric_consistency"}
RANK = {None: -1, "None": 0, "Minor": 1, "Moderate": 2, "Critical": 3}
META_PHRASE = v7_explain.META_PHRASE

FACT_PROMPT = """You are reading ONE part of a student's thesis, called "{section}", to list the statements that other parts of the paper could contradict.
List up to 30 statements of these kinds:
- objective: aims, research questions, hypotheses, scope and limits
- method: design, sampling, instruments, tools, techniques, frameworks, datasets, procedure steps
- number: counts, sizes, percentages, scores, dates, durations, versions
- claim: findings, conclusions, recommendations, causal or comparative claims
- term: the name or definition given to a key concept, system or component
Rules: copy "quote" EXACTLY, word for word, from the text below (one whole sentence, never edited or joined). "statement" is a short plain restatement of at most 25 words. Skip background, citations and general knowledge, and skip anything vague. If the text has none, return an empty list.
Return JSON: {{"facts":[{{"kind":"objective|method|number|claim|term","statement":"...","quote":"..."}}]}}
TEXT:
{text}"""

CROSS_PROMPT = """Below are statements taken from every part of one student's thesis. Each has an id, the section it came from and its kind.
Find statements from DIFFERENT sections that contradict each other or do not match, so that a reader would not know which is true. Examples: two different numbers for the same thing (people, items, dates, percentages, versions); a method, tool, design or framework named differently in two places; an objective or research question that changed between sections; a conclusion that claims more than the findings support (for example a causal claim from correlational results); the same term used for two different things.
Do NOT report: different wording of the same idea, one statement being more detailed than another, a statement with no counterpart, or statements about different things. Also do NOT report a difference in numbers that legitimate research reasons can explain (attrition or dropouts, missing responses, exclusion criteria, a smaller group analysed than recruited); report numbers only when they are physically impossible (for example more people analysed than recruited, or parts larger than their total).
Be strict: report a pair only when you can explain the mismatch in one clear sentence that names both. At most {max} pairs, most serious first. For each pair also name the ONE statement that should be corrected ("fix_id").
Severity: Critical = the sections directly contradict each other; Moderate = a claim, method, goal or number does not match; Minor = a small term, figure or name differs.
Return JSON: {{"conflicts":[{{"fact_a":"F1","fact_b":"F2","severity":"Critical|Moderate|Minor","category":"objectives|methodology|claims|numeric","what":"short title","why":"one or two plain sentences","fix_id":"F2"}}]}}
STATEMENTS:
{ledger}"""

CONFIRM_PROMPT = """A tool suspects that the pairs of statements below, taken from one student's thesis, contradict each other. For each pair you see the sentence and the paragraph around it.
Decide whether a thesis adviser reading both paragraphs would agree they conflict. Answer genuine=false when it is only different wording, one being more detailed, two statements about different things, or a difference in numbers that attrition, dropouts, missing responses or exclusion criteria can explain (only physically impossible numbers are genuine).
For a genuine pair, also rewrite the sentence marked CORRECT so it agrees with the other one: copy that sentence and change only what is needed, in the same style, at most 60 words, without mentioning sections or the other statement.
Return JSON: {{"results":[{{"id":1,"genuine":true,"severity":"Critical|Moderate|Minor","fixed_sentence":"..."}}]}}
{items}"""


def enabled() -> bool:
    return os.getenv("FULL_CHECK", "on").strip().lower() not in ("off", "false", "0", "no")


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").translate(gemini._QUOTES)).strip().lower()


def chunk_text(text: str, size: int = CHUNK_CHARS) -> List[str]:
    """Whole paragraphs, up to `size` characters each; a paragraph longer than that is cut at sentence ends."""
    pieces: List[str] = []
    for paragraph in (text or "").split("\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) <= size:
            pieces.append(paragraph)
        else:
            pieces.extend(sentence for sentence in re.split(r"(?<=[.!?])\s+", paragraph) if sentence)
    chunks: List[str] = []
    current = ""
    for piece in pieces:
        if current and len(current) + len(piece) + 1 > size:
            chunks.append(current)
            current = ""
        current = f"{current}\n{piece}" if current else piece
    if current:
        chunks.append(current)
    return chunks


async def _ask(prompt: str, label: str) -> Optional[Dict[str, Any]]:
    """One Gemini answer as a dict, or None when it could not be had (quota, errors). Two tries."""
    for attempt in (1, 2):
        try:
            raw = await v7_explain._generate(prompt, max_tokens=32768)
            return raw if isinstance(raw, dict) else None
        except gemini.GeminiQuotaExhausted:
            logger.warning("Full check (%s) skipped: the AI's limit was reached", label)
            return None
        except Exception:
            logger.exception("Full check (%s) failed (attempt %s of 2)", label, attempt)
            if attempt == 1:
                await asyncio.sleep(1.5)
    return None


async def _extract(section: str, chunk: str) -> Optional[List[Dict[str, str]]]:
    """The checkable statements of one piece of one section, each with a sentence that really is in the text."""
    key = VerdictCache.fingerprint("full-check-facts", VERSION, section, chunk)
    saved = (await VerdictCache.get_many([key])).get(key)
    if saved is not None and isinstance(saved.get("facts"), list):
        return saved["facts"]
    raw = await _ask(FACT_PROMPT.format(section=section, text=chunk), f"read {section}")
    if raw is None or not isinstance(raw.get("facts"), list):
        return None
    facts = []
    for item in raw["facts"][:MAX_FACTS_PER_CHUNK]:
        if not isinstance(item, dict):
            continue
        quote, statement = item.get("quote"), item.get("statement")
        if not (isinstance(quote, str) and isinstance(statement, str) and quote.strip() and statement.strip()):
            continue
        quote = " ".join(quote.split())
        if len(quote) > 500 or not gemini.verify_quote(quote, chunk)[1]:   # an invented or altered "quote" is dropped
            continue
        kind = item.get("kind") if item.get("kind") in KINDS else "claim"
        facts.append({"kind": kind, "statement": " ".join(statement.split())[:240], "quote": quote})
    await VerdictCache.save_many({key: {"severity": "None", "facts": facts}})
    return facts


def _paragraph_around(text: str, quote: str) -> str:
    target = _norm(quote)
    for paragraph in (text or "").split("\n"):
        if target in _norm(paragraph):
            return paragraph.strip()[:900]
    return quote


def _exact(text: str, quote: str) -> str:
    """The quote as it is written in the paper (spacing and curly quotes as the student typed them)."""
    pattern = re.sub(r"(?:\\ )+", lambda _: r"\s+", re.escape(" ".join(quote.split())))
    pattern = pattern.replace("'", "['‘’]").replace('"', '["“”]')
    match = re.search(pattern, text or "", flags=re.I)
    return match.group() if match else quote


async def _cross_check(facts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Mismatches between statements of different sections, as asked of the AI. Facts carry id, section, kind, statement."""
    def lines(group):
        return "\n".join(f'{f["id"]} | {f["section"]} | {f["kind"]} | {f["statement"]}' for f in group)

    ledger = lines(facts)
    groups = [facts] if len(ledger) <= LEDGER_SPLIT_CHARS else [
        [f for f in facts if f["kind"] in ("number", "method")], [f for f in facts if f["kind"] not in ("number", "method")]]
    found: List[Dict[str, Any]] = []
    for group in groups:
        if len({f["section"] for f in group}) < 2:
            continue
        text = lines(group)
        key = VerdictCache.fingerprint("full-check-cross", VERSION, text)
        saved = (await VerdictCache.get_many([key])).get(key)
        if saved is not None and isinstance(saved.get("conflicts"), list):
            found.extend(saved["conflicts"])
            continue
        raw = await _ask(CROSS_PROMPT.format(max=MAX_CONFLICTS, ledger=text), "compare")
        if raw is None or not isinstance(raw.get("conflicts"), list):
            continue
        conflicts = [c for c in raw["conflicts"] if isinstance(c, dict)][:MAX_CONFLICTS]
        await VerdictCache.save_many({key: {"severity": "None", "conflicts": conflicts}})
        found.extend(conflicts)
    return found


def _sane_fix(fixed: Any, original: str) -> Optional[str]:
    if not isinstance(fixed, str) or not fixed.strip():
        return None
    fixed = " ".join(fixed.split())
    if _norm(fixed) == _norm(original) or len(fixed.split()) > 70 or META_PHRASE.search(fixed):
        return None
    return fixed


async def _confirm(candidates: List[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    """{candidate number: {"severity", "fixed_sentence"}} for the candidates a second look with full context agrees with."""
    if not candidates:
        return {}
    blocks = []
    for number, c in enumerate(candidates, 1):
        blocks.append(
            f'=== PAIR {number} ===\nSuspected problem: {c["what"]}. {c["why"]}\n'
            f'Statement 1 (section "{c["a"]["section"]}"){" - CORRECT THIS ONE" if c["fix"] is c["a"] else ""}:\n  Sentence: {c["a"]["quote"]}\n  Paragraph: {c["a"]["context"]}\n'
            f'Statement 2 (section "{c["b"]["section"]}"){" - CORRECT THIS ONE" if c["fix"] is c["b"] else ""}:\n  Sentence: {c["b"]["quote"]}\n  Paragraph: {c["b"]["context"]}\n')
    prompt = CONFIRM_PROMPT.format(items="\n".join(blocks))
    key = VerdictCache.fingerprint("full-check-confirm", VERSION, prompt)
    saved = (await VerdictCache.get_many([key])).get(key)
    raw = saved if saved is not None and isinstance(saved.get("results"), list) else await _ask(prompt, "confirm")
    if raw is None or not isinstance(raw.get("results"), list):
        return {}
    if saved is None:
        await VerdictCache.save_many({key: {"severity": "None", "results": raw["results"]}})
    confirmed: Dict[int, Dict[str, Any]] = {}
    for item in raw["results"]:
        if not isinstance(item, dict) or item.get("genuine") is not True:
            continue
        try:
            number = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if 1 <= number <= len(candidates):
            c = candidates[number - 1]
            confirmed[number] = {"severity": item.get("severity") if item.get("severity") in SEVERITIES else c["severity"],
                                 "fixed_sentence": _sane_fix(item.get("fixed_sentence"), c["fix"]["quote"])}
    return confirmed


def _ensure_pair(flow: List[Dict[str, Any]], sections: List[Dict[str, Any]], i: int, j: int) -> Dict[str, Any]:
    """The flow entry for sections i and j, made when the local model had not kept that pair."""
    from services.v7_scan import DIMENSIONS, assemble_flow
    for pair in flow:
        if pair["index_a"] == i and pair["index_b"] == j:
            return pair
    ratings = {key: 0.8 for key in DIMENSIONS}
    raw = {"index_a": i, "index_b": j, "scores": {**ratings, "overall_coherence": 0.8, "pair_weight": 1.0},
           "culprits_a": [], "culprits_b": []}
    pair = assemble_flow(sections, [raw])[0]
    pair["order"] = max((p["order"] for p in flow), default=0) + 1
    flow.append(pair)
    return pair


def _add_finding(pair: Dict[str, Any], sections: List[Dict[str, Any]], conflict: Dict[str, Any], severity: str, fixed: Optional[str]) -> None:
    a, b, fix = conflict["a"], conflict["b"], conflict["fix"]
    explanation = pair["explanation"] or {
        "severity": "None", "failed_dimension": "none", "coherence_scores": {}, "link_summary": "",
        "comparison": {"a_says": "", "b_says": ""}, "highlighted_inconsistencies": [], "recommendations": [],
        "reference_list": [], "bridges": []}
    known = {item.get("quote") for item in explanation["highlighted_inconsistencies"]}
    for side in (a, b):
        if side["quote"] not in known:
            explanation["highlighted_inconsistencies"].append(
                {"section": side["section"], "quote": side["quote"], "what": conflict["what"], "why": conflict["why"]})
    explanation["reference_list"].append({"section": (b if fix is a else a)["section"], "sentence": (b if fix is a else a)["quote"]})
    if fixed and not any(r.get("original_sentence") == fix["quote"] for r in explanation["recommendations"]):
        explanation["recommendations"].append({
            "section": fix["section"], "original_sentence": fix["quote"], "recommended_revision": fixed, "kind": "edit",
            "issue": conflict["why"], "category": conflict["category"]})
    if not explanation.get("link_summary"):
        explanation["link_summary"] = conflict["why"]
    if not (explanation["comparison"].get("a_says") or explanation["comparison"].get("b_says")):
        explanation["comparison"] = {"a_says": a["statement"], "b_says": b["statement"]}
    if RANK.get(explanation.get("severity"), 0) < RANK[severity]:
        explanation["severity"] = severity
        explanation["failed_dimension"] = CATEGORY_DIMENSION.get(conflict["category"], "claim_support")
    elif explanation.get("failed_dimension") in (None, "none"):
        explanation["failed_dimension"] = CATEGORY_DIMENSION.get(conflict["category"], "claim_support")
    if conflict["category"] == "numeric":
        pair["numeric_applicable"] = True
    pair["explanation"] = explanation


def start(sections: List[Dict[str, Any]]) -> Optional["asyncio.Future"]:
    """Begins reading and comparing the whole paper now, so it runs while the pair reviews and the model are still working.
    The finished work is handed to apply_full_check(prepared=...). None when the check is off or not possible."""
    if not enabled() or len(sections) < 2 or gemini.quota_blocked():
        return None
    return asyncio.ensure_future(_prepare(sections))


async def apply_full_check(flow: List[Dict[str, Any]], sections: List[Dict[str, Any]], roles: Optional[Dict[str, str]] = None,
                           prepared: Optional["asyncio.Future"] = None) -> int:
    """Reads every section in full, compares all of it, and adds the confirmed mismatches to the flow. Returns how many."""
    if not enabled() or len(sections) < 2 or gemini.quota_blocked():
        if prepared is not None:
            prepared.cancel()
        return 0
    try:
        found = await (prepared if prepared is not None else _prepare(sections))
        return _commit(flow, sections, found)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Full check failed; the report keeps the pair review's findings")
        return 0


async def _prepare(sections: List[Dict[str, Any]]):
    """Everything that needs the AI: (candidates, confirmed). It does not touch the report, so it can run early."""
    jobs = [(index, chunk) for index, section in enumerate(sections) if len((section["text"] or "").split()) >= MIN_SECTION_WORDS
            for chunk in chunk_text(section["text"])][:MAX_CHUNK_CALLS]
    if not jobs:
        return [], {}
    gemini.expect_calls(len(jobs) + 2)
    gate = asyncio.Semaphore(CONCURRENCY)

    async def read(job):
        async with gate:
            name, chunk = sections[job[0]]["name"], job[1]
            found = await _extract(name, chunk)
            if found is None and len(chunk) > 2500:   # an answer that came back broken: read the same piece again in two halves
                halves = chunk_text(chunk, size=len(chunk) // 2 + 1)
                parts = [await _extract(name, half) for half in halves]
                found = [fact for part in parts for fact in (part or [])] if any(part is not None for part in parts) else None
            return found

    results = await asyncio.gather(*(read(job) for job in jobs), return_exceptions=True)
    facts: List[Dict[str, Any]] = []
    for (index, _), found in zip(jobs, results):
        for fact in found if isinstance(found, list) else []:
            facts.append({**fact, "id": f"F{len(facts) + 1}", "index": index, "section": sections[index]["name"]})
    if len({fact["index"] for fact in facts}) < 2:
        return [], {}
    by_id = {fact["id"]: fact for fact in facts}

    candidates: List[Dict[str, Any]] = []
    seen = set()
    for item in await _cross_check(facts):
        a, b = by_id.get(str(item.get("fact_a"))), by_id.get(str(item.get("fact_b")))
        if not (a and b and a["index"] != b["index"]):
            continue
        # The AI sometimes names a field wrongly ("claims" instead of "what") or leaves one out. The two statements are what
        # matter, and the confirm step re-checks them with the real quotes, so a missing title or reason is filled in.
        spare = [v for k, v in item.items() if k not in ("fact_a", "fact_b", "severity", "category", "fix_id", "what", "why")
                 and isinstance(v, str) and v.strip()]
        what = item.get("what") if isinstance(item.get("what"), str) and item["what"].strip() else (spare[0] if spare else "Statements disagree")
        why = item.get("why") if isinstance(item.get("why"), str) and item["why"].strip() else (
            spare[-1] if len(spare) > 1 else f"The {a['section']} and {b['section']} sections do not agree.")
        if (a["index"], b["index"]) > (b["index"], a["index"]):
            a, b = b, a
        if (a["id"], b["id"]) in seen:
            continue
        seen.add((a["id"], b["id"]))
        # The AI names statements by id ("Statement F3"); the student sees section names instead.
        def plain(text, a=a, b=b):
            for fact in (a, b):
                text = re.sub(rf"\b(?:the )?(?:statement|fact)?\s*{fact['id']}\b", f"the {fact['section']} section", text, flags=re.I)
            return re.sub(r"\bthe the\b", "the", re.sub(r"\s+", " ", text)).strip()
        what, why = plain(what), plain(why)
        fix = by_id.get(str(item.get("fix_id")))
        fix = fix if fix in (a, b) else b
        severity = item.get("severity") if item.get("severity") in SEVERITIES else "Moderate"
        category = item.get("category") if item.get("category") in CATEGORY_DIMENSION else "claims"
        sides = []
        for fact in (a, b):
            text = sections[fact["index"]]["text"]
            sides.append({"section": fact["section"], "index": fact["index"], "statement": fact["statement"], "id": fact["id"],
                          "quote": _exact(text, fact["quote"]), "context": _paragraph_around(text, fact["quote"])})
        candidates.append({"a": sides[0], "b": sides[1], "fix": sides[0] if fix is a else sides[1], "what": what.strip(), "why": why.strip(),
                           "severity": severity, "category": category})
    return candidates, await _confirm(candidates)


def _commit(flow: List[Dict[str, Any]], sections: List[Dict[str, Any]], found) -> int:
    """Adds the confirmed mismatches to the report. Returns how many."""
    candidates, confirmed = found
    added = 0
    for number, verdict in confirmed.items():
        c = candidates[number - 1]
        pair = _ensure_pair(flow, sections, c["a"]["index"], c["b"]["index"])
        _add_finding(pair, sections, c, verdict["severity"], verdict["fixed_sentence"])
        added += 1
    if added:   # keep the report in reading order: new pairs slot in where their sections are
        flow.sort(key=lambda p: (p["index_a"], p["index_b"]))
        for position, pair in enumerate(flow, 1):
            pair["order"] = position
    return added
