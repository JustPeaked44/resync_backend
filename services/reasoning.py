import os
import asyncio
import json
import logging
import httpx
import uuid
import re
from typing import Any, Dict, List, Optional, Tuple
from pydantic import BaseModel, Field
from services.scoring import CrossChapterCoherenceResult, PAR_SCORE

logger = logging.getLogger("resync.reasoning")


class InconsistencyOutput(BaseModel):
    section_a: str = Field(description="Canonical role name of the first section in the pair")
    section_b: str = Field(description="Canonical role name of the second section in the pair")
    coherence_score: float = Field(description="The calibrated cross-chapter coherence score for this pair")
    explanation_what: str = Field(description="XAI Part 1: Clear statement describing the detected inconsistency or structural disconnect")
    explanation_why: str = Field(description="XAI Part 2: Explanation of why this inconsistency harms the academic coherence or validity of the manuscript")
    suggested_fix: str = Field(description="XAI Part 3: Actionable, specific recommendation for the researcher to align the two sections")
    inconsistency_id: str = Field(default_factory=lambda: str(uuid.uuid4()), description="Pre-generated UUID for this inconsistency, used as PK in DB")
    evidence_a: str = Field(default="", description="Verbatim sentence from Section A supporting the inconsistency")
    evidence_b: str = Field(default="", description="Verbatim sentence from Section B supporting the inconsistency")
    evidence_verified: bool = Field(default=False, description="True only if every non-empty evidence quote above was found verbatim in its source section")
    objectives_unaddressed: List[str] = Field(default_factory=list, description="Objective sentences from Section A not answered in Section B")
    finding_status: str = Field(default="material_issue", description="Always 'material_issue' for a surfaced finding -- dismissed findings never reach this model")


class VerificationOutput(BaseModel):
    """One strong (score >= PAR) pair, checked for whether its high
    calibrated score reflects substantive alignment or just shared
    academic vocabulary/register."""
    role_a: str
    role_b: str
    score: float
    alignment: str = Field(description="'substantive' or 'superficial'")
    note: str = Field(description="One sentence explaining the classification")


# ---------------------------------------------------------------------------
# Defect-explanation prompt. Deliberately does NOT tell the model a defect
# has been found -- it states the score neutrally and asks the model to
# determine whether a material inconsistency exists at all. Without this,
# the model has no way to report a false positive: every prior version of
# this prompt asserted the pair "ha[d] been flagged with a LOW coherence
# score," so it always manufactured a finding.
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "You are an expert academic research reviewer and capstone advisor. "
    "You are reviewing two sections from an IT/CS undergraduate capstone manuscript that a "
    "deterministic embedding-similarity model scored as weakly aligned relative to what is "
    "expected for this pair of sections.\n\n"
    "First, determine whether a MATERIAL inconsistency actually exists between the two "
    "excerpts -- a real logical gap, data disconnect, or conceptual mismatch that a human "
    "reviewer would flag. A low similarity score is a signal to investigate, not proof of a "
    "defect: two sections can be low-similarity because they legitimately discuss different "
    "things (e.g. a short section, unconventional phrasing, or content the embedding model "
    "under-weights) without being inconsistent with each other.\n\n"
    "Set finding_status to 'no_material_issue' if you cannot identify a real, specific problem "
    "-- do not manufacture a defect to fill the fields. Only when finding_status is "
    "'material_issue' should what/why/fix contain a genuine finding; otherwise explain in 'why' "
    "briefly why no material issue was found and leave 'what' and 'fix' empty.\n\n"
    "When finding_status is 'material_issue', provide a strict 3-part Explainable AI (XAI) "
    "breakdown:\n"
    "1. WHAT: Identify the specific logical gap, data disconnect, or conceptual mismatch.\n"
    "2. WHY: Explain why this mismatch weakens the manuscript's validity, structural integrity, or methodology.\n"
    "3. FIX: Give a concise, actionable recommendation on how to update either or both sections to resolve it.\n\n"
    "Be direct, constructive, and tailored specifically to academic software engineering & IT research standards. "
    "For evidence_a, copy a sentence VERBATIM from Section A's excerpt that supports your finding -- do not "
    "paraphrase or reconstruct it. For evidence_b, do the same from Section B. If no excerpt sentence "
    "genuinely supports the finding, leave that evidence field empty rather than approximating one."
)

OBJECTIVES_INSTRUCTION = (
    "\n\nSection A is the manuscript's Objectives section. Additionally check whether every "
    "research objective listed in Section A is answered in Section B. For any objective not "
    "explicitly addressed, include its exact sentence (verbatim from Section A) in "
    "objectives_unaddressed."
)

TRUNCATION_NOTICE = (
    "\n\n(Note: this excerpt was truncated for length; base your analysis only on what is shown.)"
)


def _xai_schema(include_objectives: bool) -> Dict[str, Any]:
    """Builds the Gemini structured-output schema per call. objectives_unaddressed
    is only requested when Section A is the objectives role -- it is meaningless
    for any other pair and was previously requested (and silently empty) on every
    single pair regardless of role."""
    properties = {
        "finding_status": {
            "type": "STRING",
            "enum": ["material_issue", "no_material_issue"],
            "description": "Whether a real, specific inconsistency was found",
        },
        "what": {
            "type": "STRING",
            "description": "Clear statement of what inconsistency exists between the two sections (empty if no_material_issue)",
        },
        "why": {
            "type": "STRING",
            "description": "Why the gap harms academic validity (or, if no_material_issue, why nothing material was found)",
        },
        "fix": {
            "type": "STRING",
            "description": "Concrete recommendation on how to edit or align the sections (empty if no_material_issue)",
        },
        "evidence_a": {
            "type": "STRING",
            "description": "A sentence copied verbatim from Section A's excerpt, or empty if none supports the finding",
        },
        "evidence_b": {
            "type": "STRING",
            "description": "A sentence copied verbatim from Section B's excerpt, or empty if none supports the finding",
        },
    }
    required = ["finding_status", "what", "why", "fix", "evidence_a", "evidence_b"]
    if include_objectives:
        properties["objectives_unaddressed"] = {
            "type": "ARRAY",
            "items": {"type": "STRING"},
            "description": "Objective sentences from Section A that are not addressed in Section B",
        }
        required.append("objectives_unaddressed")
    return {"type": "OBJECT", "properties": properties, "required": required}


# ---------------------------------------------------------------------------
# Strong-pair verification prompt: a single batched call that treats a high
# calibrated score as a hypothesis to test, not praise to generate. Two
# sections can score high purely from shared academic register/vocabulary
# (see scoring.py's anisotropy note) without being substantively aligned --
# that is a scoring false positive, and this pass is the check for it.
# ---------------------------------------------------------------------------
VERIFICATION_SYSTEM_PROMPT = (
    "You are an expert academic research reviewer. Each pair below was scored as highly "
    "aligned by a deterministic embedding-similarity model. For each pair, determine whether "
    "that alignment is SUBSTANTIVE -- the two excerpts share real argument, data, or claims -- "
    "or SUPERFICIAL -- they merely share academic terminology and register without actually "
    "reinforcing each other. Superficial alignment is a scoring false positive, not a strength: "
    "do not default to 'substantive' out of politeness. Write one concise, specific sentence "
    "per pair explaining your classification."
)

VERIFICATION_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "role_a": {"type": "STRING"},
            "role_b": {"type": "STRING"},
            "alignment": {"type": "STRING", "enum": ["substantive", "superficial"]},
            "note": {"type": "STRING", "description": "One sentence explaining the classification"},
        },
        "required": ["role_a", "role_b", "alignment", "note"],
    },
}

_WS_RE = re.compile(r"\s+")
_CURLY_QUOTES_TABLE = str.maketrans({
    "‘": "'", "’": "'", "“": '"', "”": '"',
})


def _normalize_for_match(s: str) -> str:
    return _WS_RE.sub(" ", s.translate(_CURLY_QUOTES_TABLE)).strip().lower()


class ReasoningService:
    def __init__(self, default_threshold: float = PAR_SCORE):
        # "default_threshold" name kept for the /api/test-reasoning debug
        # endpoint's existing query param; it is PAR on the calibrated
        # cross-chapter scale, not the deprecated linear adjacent-pair scale.
        self.default_threshold = default_threshold
        # Using 3.5-flash-lite because it has a 15 RPM limit on the free tier
        self.api_url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent"

    def _truncate_text(self, text: str, max_chars: int = 6000) -> str:
        """Truncates section text to avoid token overflow while keeping core context."""
        cleaned = text.strip()
        if len(cleaned) <= max_chars:
            return cleaned
        return cleaned[:max_chars] + "... [truncated]"

    def _build_prompt(
        self,
        role_a: str,
        text_a: str,
        role_b: str,
        text_b: str,
        score: float,
    ) -> str:
        excerpt_a = self._truncate_text(text_a)
        excerpt_b = self._truncate_text(text_b)
        prompt = (
            f"{SYSTEM_PROMPT}\n\n"
            f"Section A ({role_a}):\n{excerpt_a}"
            f"{TRUNCATION_NOTICE if excerpt_a.endswith('[truncated]') else ''}\n\n"
            f"Section B ({role_b}):\n{excerpt_b}"
            f"{TRUNCATION_NOTICE if excerpt_b.endswith('[truncated]') else ''}\n\n"
            f"Calibrated coherence score for this pair: {score}/100\n\n"
        )
        if role_a == "objectives":
            prompt += OBJECTIVES_INSTRUCTION + "\n\n"
        prompt += "Analyze Section A and Section B and output the structured response."
        return prompt

    @staticmethod
    def _verify_quote(quote: str, text: str) -> Tuple[str, bool]:
        """Returns (quote, True) only when the quote genuinely appears in
        the section (tolerating whitespace and curly-quote differences).
        Otherwise ("", False) -- never substitutes a sentence the model
        did not actually cite; a missing quote is safer than a fabricated
        one for an explainability feature where evidence is the trust
        anchor. An empty input quote (no claim made) trivially verifies."""
        if not quote:
            return "", True
        if not text:
            return "", False
        if _normalize_for_match(quote) in _normalize_for_match(text):
            return quote, True
        return "", False

    async def _post_gemini(self, payload: Dict[str, Any]) -> httpx.Response:
        api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        url = f"{self.api_url}?key={api_key}"
        headers = {"Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=30.0) as client:
            return await client.post(url, json=payload, headers=headers)

    async def _post_gemini_with_retry(self, payload: Dict[str, Any]) -> httpx.Response:
        """A 429 gets one retry after honoring Retry-After (default 5s).
        A second failure of any kind is returned as-is for the caller to
        handle -- this method never raises on HTTP status, only on a
        genuine transport failure, so callers can always fall back to a
        placeholder instead of losing the whole scan."""
        response = await self._post_gemini(payload)
        if response.status_code == 429:
            try:
                retry_after = float(response.headers.get("Retry-After", 5.0))
            except (TypeError, ValueError):
                retry_after = 5.0
            await asyncio.sleep(retry_after)
            response = await self._post_gemini(payload)
        return response

    async def _analyze_pair(
        self,
        role_a: str,
        text_a: str,
        role_b: str,
        text_b: str,
        score: float,
    ) -> Tuple[Optional[InconsistencyOutput], Optional[Dict[str, str]]]:
        """Calls Gemini directly via HTTP REST to bypass SDK bugs with the
        new 'AQ.' keys.

        Returns (finding, dismissal): exactly one is non-None. A dismissal
        means the model determined no material inconsistency exists despite
        the low calibrated score -- this is the false-positive detector for
        the scoring model. Any API/parse error (including an exhausted
        429 retry) degrades to a placeholder finding, matching prior
        behavior -- it never raises, so one failed pair cannot lose the
        rest of the scan.
        """
        include_objectives = role_a == "objectives"
        prompt = self._build_prompt(role_a, text_a, role_b, text_b, score)

        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "responseMimeType": "application/json",
                "responseSchema": _xai_schema(include_objectives),
            },
        }

        try:
            response = await self._post_gemini_with_retry(payload)
            response.raise_for_status()

            data = response.json()
            raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
            parsed = json.loads(raw_text)

            if parsed.get("finding_status") == "no_material_issue":
                return None, {
                    "role_a": role_a,
                    "role_b": role_b,
                    "score": score,
                    "reason": parsed.get("why", "") or "No material inconsistency found.",
                }

            evidence_a, verified_a = self._verify_quote(parsed.get("evidence_a", ""), text_a)
            evidence_b, verified_b = self._verify_quote(parsed.get("evidence_b", ""), text_b)

            return InconsistencyOutput(
                section_a=role_a,
                section_b=role_b,
                coherence_score=score,
                explanation_what=parsed.get("what", ""),
                explanation_why=parsed.get("why", ""),
                suggested_fix=parsed.get("fix", ""),
                evidence_a=evidence_a,
                evidence_b=evidence_b,
                evidence_verified=verified_a and verified_b,
                objectives_unaddressed=parsed.get("objectives_unaddressed", []) if include_objectives else [],
            ), None

        except Exception as e:
            err_msg = str(e)
            if isinstance(e, httpx.HTTPStatusError):
                err_msg = e.response.text
            return InconsistencyOutput(
                section_a=role_a,
                section_b=role_b,
                coherence_score=score,
                explanation_what=f"Semantic discrepancy detected between '{role_a}' and '{role_b}'.",
                explanation_why=f"Coherence score ({score}/100) is below par. (XAI Error: {err_msg})",
                suggested_fix="Review and align the conceptual dependencies between these two sections.",
                evidence_a="",
                evidence_b="",
                evidence_verified=False,
                objectives_unaddressed=[],
            ), None

    async def _verify_strong_pairs(
        self,
        pairs: List[Dict[str, Any]],
        role_texts: Dict[str, str],
        excerpt_chars: int = 800,
    ) -> List[VerificationOutput]:
        """One batched call covering every pair scoring >= PAR. Verification
        is advisory (it never gates the score or blocks a scan), so any
        failure here degrades to an empty list rather than propagating --
        the same "one failure can't lose the whole scan" principle applied
        to this new call, not just the pre-existing defect-explanation path.
        """
        if not pairs:
            return []

        blocks = []
        for p in pairs:
            role_a, role_b = p["role_a"], p["role_b"]
            excerpt_a = self._truncate_text(role_texts.get(role_a, ""), max_chars=excerpt_chars)
            excerpt_b = self._truncate_text(role_texts.get(role_b, ""), max_chars=excerpt_chars)
            blocks.append(
                f"Pair: {role_a} <-> {role_b} (calibrated score {p['score']}/100)\n"
                f"Excerpt {role_a}:\n{excerpt_a}\n\n"
                f"Excerpt {role_b}:\n{excerpt_b}"
            )

        prompt = (
            f"{VERIFICATION_SYSTEM_PROMPT}\n\n"
            + "\n\n---\n\n".join(blocks)
            + "\n\nClassify each pair above, in the same order, as one JSON array entry."
        )

        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "responseMimeType": "application/json",
                "responseSchema": VERIFICATION_SCHEMA,
            },
        }

        try:
            response = await self._post_gemini_with_retry(payload)
            response.raise_for_status()
            data = response.json()
            raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
            items = json.loads(raw_text)

            score_by_pair = {(p["role_a"], p["role_b"]): p["score"] for p in pairs}
            results: List[VerificationOutput] = []
            for item in items:
                role_a = item.get("role_a", "")
                role_b = item.get("role_b", "")
                results.append(VerificationOutput(
                    role_a=role_a,
                    role_b=role_b,
                    score=score_by_pair.get((role_a, role_b), 0.0),
                    alignment=item.get("alignment", "substantive"),
                    note=item.get("note", ""),
                ))
            return results
        except Exception as e:
            logger.warning("Strong-pair verification failed, degrading to empty: %s", e)
            return []

    async def analyze_inconsistencies(
        self,
        cross_chapter_result: CrossChapterCoherenceResult,
        role_texts: Dict[str, str],
        par: Optional[float] = None,
        max_pairs: int = 4,
    ) -> Tuple[List[InconsistencyOutput], List[VerificationOutput], List[Dict[str, Any]]]:
        """
        Partitions cross_chapter_result.pair_scores at PAR:
        - score < PAR: defect candidate, ranked by weight * (PAR - score)
          (importance-weighted deficit) descending, top max_pairs explained.
        - score >= PAR: verification candidate, all batched into one call.

        Explanation calls run a few requests in flight at once, while pacing
        new request *starts* to stay under Google's free-tier rate limit
        (15 RPM => >=4s apart). This overlaps network latency across calls
        instead of processing one pair fully before starting the next,
        cutting wall-clock time without exceeding the RPM budget.

        Returns (findings, verifications, dismissed_pairs). dismissed_pairs
        records role names + the model's one-line reason for any pair the
        model determined has no material inconsistency despite a low score
        -- the false-positive detector for the underlying scoring model.
        """
        cutoff = par if par is not None else self.default_threshold
        included = [p for p in cross_chapter_result.pair_scores if p.get("included")]

        # "__document__" has no second real section to quote against --
        # skip it for defect explanation, but it may still be verified.
        defect_candidates = [
            p for p in included
            if p["score"] < cutoff and "__document__" not in (p["role_a"], p["role_b"])
        ]
        defect_candidates.sort(key=lambda p: p["weight"] * (cutoff - p["score"]), reverse=True)
        defect_candidates = defect_candidates[:max_pairs]

        strong_pairs = [p for p in included if p["score"] >= cutoff]

        findings: List[InconsistencyOutput] = []
        dismissed: List[Dict[str, Any]] = []

        if defect_candidates:
            min_start_interval = 4.2  # seconds; keeps starts under 15 RPM with margin
            concurrency = min(3, len(defect_candidates))
            sem = asyncio.Semaphore(concurrency)
            pacing_lock = asyncio.Lock()
            state = {"next_allowed_start": 0.0}
            loop = asyncio.get_event_loop()
            loop_start = loop.time()

            async def _paced_analyze(pair: Dict[str, Any]) -> Tuple[Optional[InconsistencyOutput], Optional[Dict[str, Any]]]:
                async with sem:
                    async with pacing_lock:
                        now = loop.time() - loop_start
                        wait = max(0.0, state["next_allowed_start"] - now)
                        state["next_allowed_start"] = max(state["next_allowed_start"], now) + min_start_interval
                    if wait > 0:
                        await asyncio.sleep(wait)

                    role_a, role_b = pair["role_a"], pair["role_b"]
                    return await self._analyze_pair(
                        role_a, role_texts.get(role_a, ""),
                        role_b, role_texts.get(role_b, ""),
                        pair["score"],
                    )

            results = await asyncio.gather(*(_paced_analyze(pair) for pair in defect_candidates))
            for finding, dismissal in results:
                if finding is not None:
                    findings.append(finding)
                elif dismissal is not None:
                    dismissed.append(dismissal)

        verifications = await self._verify_strong_pairs(strong_pairs, role_texts)

        return findings, verifications, dismissed


# Module-level singleton
reasoning_service = ReasoningService()
