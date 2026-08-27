"""
Composite "functional metric" manuscript score: Structural Completeness +
Cross-Chapter Coherence + Citation Integrity, replacing the single flat
overall_coherence_score (arithmetic mean of adjacent-section-pair cosine
similarity) that could not tell a student anything diagnostic -- two
completely unrelated academic paragraphs already scored ~55-65 under the
old ((sim + 1) / 2) * 100 rescale.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

# ---------------------------------------------------------------------------
# 1. Structural Completeness
# ---------------------------------------------------------------------------

# Stricter than embedding.py's 5-word "is this section empty enough to
# skip embedding" gate -- that threshold only exists to avoid embedding
# noise. 40 words is a better bar for "a section that was actually written".
STRUCTURAL_MIN_WORDS = 40

REQUIRED_ROLES = {
    "abstract", "introduction", "objectives", "methodology",
    "results", "discussion", "conclusion", "references",
}
REQUIRED_WEIGHT = 1.0
OPTIONAL_WEIGHT = 0.4


@dataclass
class StructuralScoreResult:
    score: float
    present_required: List[str] = field(default_factory=list)
    missing_required: List[str] = field(default_factory=list)
    present_optional: List[str] = field(default_factory=list)
    missing_optional: List[str] = field(default_factory=list)
    stub_sections: List[str] = field(default_factory=list)


def compute_structural_completeness(
    parsed_sections: Dict[str, str],
    section_roles: Dict[str, str],
    detection_confidence: float = 1.0,
) -> StructuralScoreResult:
    weighted_total = 0.0
    weighted_present = 0.0
    present_req: List[str] = []
    missing_req: List[str] = []
    present_opt: List[str] = []
    missing_opt: List[str] = []
    stubs: List[str] = []

    for heading, text in parsed_sections.items():
        role = section_roles.get(heading, heading.lower())
        is_required = role in REQUIRED_ROLES
        weight = REQUIRED_WEIGHT if is_required else OPTIONAL_WEIGHT
        weighted_total += weight

        word_count = len((text or "").split())
        is_present = word_count >= STRUCTURAL_MIN_WORDS
        if is_present:
            weighted_present += weight
            (present_req if is_required else present_opt).append(heading)
        else:
            (missing_req if is_required else missing_opt).append(heading)
            if 0 < word_count < STRUCTURAL_MIN_WORDS:
                stubs.append(heading)

    raw_score = 100.0 * (weighted_present / weighted_total) if weighted_total else 0.0
    # Dampens auto-detect false confidence: a low-confidence auto-detection
    # shouldn't be allowed to claim full structural credit.
    score = raw_score * (0.7 + 0.3 * max(0.0, min(1.0, detection_confidence)))

    return StructuralScoreResult(
        score=round(score, 2),
        present_required=present_req,
        missing_required=missing_req,
        present_optional=present_opt,
        missing_optional=missing_opt,
        stub_sections=stubs,
    )


# ---------------------------------------------------------------------------
# 2. Cross-Chapter Coherence
# ---------------------------------------------------------------------------

# (role_a, role_b, weight) -- pairs chosen because they answer a real
# question about the manuscript's internal logic, not just "are these two
# chapters adjacent in the table of contents". "__document__" is a
# synthetic role: the word-count-weighted mean embedding of every present
# section, used as a stand-in for "the manuscript as a whole" so the
# abstract can be checked against the full document rather than just one
# other chapter.
ROLE_PAIR_WEIGHTS: List[Tuple[str, str, float]] = [
    ("objectives", "methodology", 0.22),
    ("methodology", "results", 0.20),
    ("results", "discussion", 0.16),
    ("objectives", "conclusion", 0.16),
    ("introduction", "objectives", 0.12),
    ("abstract", "__document__", 0.08),
    ("discussion", "conclusion", 0.06),
]

# Calibration anchors replacing the old linear ((sim + 1) / 2) * 100 map.
# all-mpnet-base-v2 cosine similarity is anisotropic: two *unrelated*
# formal academic paragraphs still land around 0.20-0.35 cosine purely
# from shared register and vocabulary, not semantic relatedness -- that
# is exactly why the old linear map scored unrelated pairs ~55-65
# ("(0.30+1)/2*100 = 65"). These anchors are reasoned from that known
# model behavior, not fit to a labeled Resync dataset; treat them as a
# documented starting point to refine once real scanned manuscripts with
# a human coherence judgment exist (see Project_Context.md sprint notes).
_SIM_ANCHORS = [0.00, 0.20, 0.35, 0.55, 0.70, 0.85, 1.00]
_SCORE_ANCHORS = [0.0, 5.0, 30.0, 60.0, 80.0, 100.0, 100.0]


def calibrated_similarity_to_score(sim: float) -> float:
    sim = max(-1.0, min(1.0, sim))
    return round(float(np.interp(sim, _SIM_ANCHORS, _SCORE_ANCHORS)), 2)


# PAR ("par for the course") on the calibrated scale -- the single number
# that partitions a role pair into a defect candidate (needs an XAI
# explanation) or a verification candidate (needs a "is this alignment
# real or just shared vocabulary?" check). Corresponds to cosine ~0.70 on
# _SIM_ANCHORS/_SCORE_ANCHORS above. Shared by reasoning.py so both the
# score and the XAI trigger read the same calibrated scale.
PAR_SCORE = 80.0


@dataclass
class CrossChapterCoherenceResult:
    score: Optional[float]
    pair_scores: List[Dict[str, Any]] = field(default_factory=list)
    unevaluable_weight_fraction: float = 0.0


def _aggregate_role_embeddings(
    embeddings: Dict[str, List[float]],
    section_roles: Dict[str, str],
    parsed_sections: Dict[str, str],
) -> Dict[str, np.ndarray]:
    """Groups per-heading embeddings by role (word-count-weighted mean,
    in case two headings normalize to the same role), then adds a
    synthetic "__document__" role: the word-count-weighted mean of every
    present section's embedding."""
    role_vectors: Dict[str, List[Tuple[np.ndarray, int]]] = {}
    all_weighted: List[Tuple[np.ndarray, int]] = []

    for heading, vec in embeddings.items():
        if vec is None:
            continue
        arr = np.array(vec)
        word_count = max(1, len((parsed_sections.get(heading) or "").split()))
        role = section_roles.get(heading, heading.lower())
        role_vectors.setdefault(role, []).append((arr, word_count))
        all_weighted.append((arr, word_count))

    result: Dict[str, np.ndarray] = {}
    for role, vecs in role_vectors.items():
        total_w = sum(w for _, w in vecs)
        mean_vec = sum(v * w for v, w in vecs) / total_w
        result[role] = mean_vec

    if all_weighted:
        total_w = sum(w for _, w in all_weighted)
        result["__document__"] = sum(v * w for v, w in all_weighted) / total_w

    return result


def aggregate_role_texts(
    parsed_sections: Dict[str, str],
    section_roles: Dict[str, str],
) -> Dict[str, str]:
    """Groups section body text by canonical role, mirroring
    _aggregate_role_embeddings' grouping above so the text handed to the
    XAI reasoning step is the same text that produced the calibrated
    score -- two headings normalizing to one role are concatenated rather
    than one silently overwriting the other. Also adds a synthetic
    "__document__" role (the full manuscript text) so the
    ("abstract", "__document__") pair in ROLE_PAIR_WEIGHTS has something
    to compare against."""
    role_texts: Dict[str, List[str]] = {}
    all_texts: List[str] = []

    for heading, text in parsed_sections.items():
        if not text or not text.strip():
            continue
        role = section_roles.get(heading, heading.lower())
        role_texts.setdefault(role, []).append(text)
        all_texts.append(text)

    result: Dict[str, str] = {role: "\n\n".join(chunks) for role, chunks in role_texts.items()}
    if all_texts:
        result["__document__"] = "\n\n".join(all_texts)
    return result


def compute_cross_chapter_coherence(
    embeddings: Dict[str, List[float]],
    section_roles: Dict[str, str],
    parsed_sections: Dict[str, str],
) -> CrossChapterCoherenceResult:
    role_vec = _aggregate_role_embeddings(embeddings, section_roles, parsed_sections)

    weighted_sum = 0.0
    total_weight = 0.0
    total_possible_weight = 0.0
    pair_scores: List[Dict[str, Any]] = []

    for role_a, role_b, weight in ROLE_PAIR_WEIGHTS:
        total_possible_weight += weight
        va = role_vec.get(role_a)
        vb = role_vec.get(role_b)
        if va is None or vb is None:
            pair_scores.append({
                "role_a": role_a, "role_b": role_b, "weight": weight,
                "included": False, "reason": "role missing or empty",
            })
            continue

        norm_a = np.linalg.norm(va)
        norm_b = np.linalg.norm(vb)
        if norm_a == 0 or norm_b == 0:
            pair_scores.append({
                "role_a": role_a, "role_b": role_b, "weight": weight,
                "included": False, "reason": "role missing or empty",
            })
            continue

        sim = float(np.dot(va, vb) / (norm_a * norm_b))
        score = calibrated_similarity_to_score(sim)
        weighted_sum += weight * score
        total_weight += weight
        pair_scores.append({
            "role_a": role_a, "role_b": role_b, "weight": weight,
            "included": True, "raw_similarity": round(sim, 4), "score": score,
        })

    unevaluable_fraction = (
        1.0 - (total_weight / total_possible_weight) if total_possible_weight else 1.0
    )

    if total_weight <= 0:
        return CrossChapterCoherenceResult(score=None, pair_scores=pair_scores,
                                            unevaluable_weight_fraction=1.0)

    overall = weighted_sum / total_weight
    # You cannot score "strong coherence" with more than 30% of the
    # comparison weight missing -- e.g. a manuscript with no Results
    # section yet can't claim its Methodology->Results link is solid just
    # because the pairs it *does* have scored well.
    if unevaluable_fraction > 0.30:
        overall = min(overall, 85.0)

    return CrossChapterCoherenceResult(
        score=round(overall, 2),
        pair_scores=pair_scores,
        unevaluable_weight_fraction=round(unevaluable_fraction, 3),
    )


# ---------------------------------------------------------------------------
# 3. Citation Integrity
# ---------------------------------------------------------------------------

@dataclass
class CitationIntegrityResult:
    score: Optional[float]
    well_formed_ratio: float = 0.0
    link_resolution_rate: float = 0.0
    cross_match_score: float = 0.0
    total_entries: int = 0


def compute_citation_integrity(
    citations: List[Dict[str, Any]],
    crossmatch: Dict[str, List[Dict[str, Any]]],
    total_intext_unique: int,
    references_section_present: bool,
) -> CitationIntegrityResult:
    if not references_section_present:
        # Missing References section is a structural problem, already
        # penalized by Structural Completeness -- don't double-count it
        # here by scoring citation integrity as zero too.
        return CitationIntegrityResult(score=None, total_entries=0)

    if not citations:
        # A References heading exists but nothing parsed out of it is a
        # real red flag (either the section is empty, or the parser
        # couldn't segment it) -- score it, don't exempt it.
        return CitationIntegrityResult(score=0.0, total_entries=0)

    well_formed = sum(
        1 for c in citations
        if c.get("citation_authors_parsed") and c.get("citation_year_parsed")
    )
    well_formed_ratio = well_formed / len(citations)

    linked = [c for c in citations if c.get("citation_status") != "no_link"]
    if linked:
        resolved = sum(
            1 for c in linked
            if c.get("citation_status") in ("verified_metadata", "accessible")
        )
        bot_walled = sum(1 for c in linked if c.get("citation_status") == "bot_wall")
        # bot_wall counts half -- a publisher's bot-defense blocking an
        # automated check is not the student's fault and shouldn't read
        # the same as a genuinely dead link.
        link_resolution_rate = (resolved + 0.5 * bot_walled) / len(linked)
    else:
        # No entries used a link at all -- don't punish a print-only
        # bibliography for having nothing to verify.
        link_resolution_rate = 1.0

    uncited = len(crossmatch.get("uncited_references", []))
    orphans = len(crossmatch.get("orphan_intext_citations", []))
    ref_used_rate = (len(citations) - uncited) / len(citations)
    intext_match_rate = (
        (total_intext_unique - orphans) / total_intext_unique
        if total_intext_unique else 1.0
    )
    cross_match_score = 0.6 * intext_match_rate + 0.4 * ref_used_rate

    score = 100.0 * (
        0.30 * well_formed_ratio
        + 0.40 * link_resolution_rate
        + 0.30 * cross_match_score
    )

    return CitationIntegrityResult(
        score=round(score, 2),
        well_formed_ratio=round(well_formed_ratio, 3),
        link_resolution_rate=round(link_resolution_rate, 3),
        cross_match_score=round(cross_match_score, 3),
        total_entries=len(citations),
    )


# ---------------------------------------------------------------------------
# 4. Composite score, band, "biggest lever"
# ---------------------------------------------------------------------------

DEFAULT_WEIGHTS: Dict[str, float] = {
    "structural": 0.25,
    "coherence": 0.50,
    "citation": 0.25,
}

_LEVER_LABELS = {
    "structural": "structural_completeness",
    "coherence": "cross_chapter_coherence",
    "citation": "citation_integrity",
}


@dataclass
class FunctionalMetricResult:
    overall_score: float
    band: str
    structural: StructuralScoreResult
    coherence: CrossChapterCoherenceResult
    citation_integrity: CitationIntegrityResult
    biggest_lever: Optional[Dict[str, Any]]
    weights_used: Dict[str, float]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "overall_score": self.overall_score,
            "band": self.band,
            "structural_completeness_score": self.structural.score,
            "cross_chapter_coherence_score": self.coherence.score,
            "citation_integrity_score": self.citation_integrity.score,
            "biggest_lever": self.biggest_lever,
            "weights_used": self.weights_used,
            "structural_detail": {
                "present_required": self.structural.present_required,
                "missing_required": self.structural.missing_required,
                "present_optional": self.structural.present_optional,
                "missing_optional": self.structural.missing_optional,
                "stub_sections": self.structural.stub_sections,
            },
            "coherence_detail": {
                "pair_scores": self.coherence.pair_scores,
                "unevaluable_weight_fraction": self.coherence.unevaluable_weight_fraction,
            },
            "citation_detail": {
                "well_formed_ratio": self.citation_integrity.well_formed_ratio,
                "link_resolution_rate": self.citation_integrity.link_resolution_rate,
                "cross_match_score": self.citation_integrity.cross_match_score,
                "total_entries": self.citation_integrity.total_entries,
            },
        }


def _band_for(score: float) -> str:
    if score >= 85:
        return "Strong"
    if score >= 70:
        return "Solid"
    if score >= 55:
        return "Needs Revision"
    return "Major Revision"


def _lever_reason(key: str, structural: StructuralScoreResult,
                   coherence: CrossChapterCoherenceResult,
                   citation: CitationIntegrityResult) -> str:
    if key == "structural":
        if structural.missing_required:
            return f"Missing or under-developed required section(s): {', '.join(structural.missing_required)}."
        return "Structural completeness is the largest drag on the overall score."
    if key == "coherence":
        weak_pairs = sorted(
            [p for p in coherence.pair_scores if p.get("included")],
            key=lambda p: p["score"],
        )[:2]
        if weak_pairs:
            names = ", ".join(f"{p['role_a']} ↔ {p['role_b']}" for p in weak_pairs)
            return f"Weakest chapter-to-chapter alignment: {names}."
        return "Cross-chapter coherence is the largest drag on the overall score."
    if key == "citation":
        if citation.total_entries == 0:
            return "No references could be parsed from the References section."
        if citation.link_resolution_rate < 0.6:
            return "Many reference links could not be verified as reachable."
        return "Citation formatting or in-text/reference-list matching needs attention."
    return ""


def compute_functional_metric(
    structural: StructuralScoreResult,
    coherence: CrossChapterCoherenceResult,
    citation: CitationIntegrityResult,
    weights: Dict[str, float] = DEFAULT_WEIGHTS,
) -> FunctionalMetricResult:
    subscores = {
        "structural": structural.score,
        "coherence": coherence.score,
        "citation": citation.score,
    }
    active = {k: v for k, v in subscores.items() if v is not None}
    active_weight_sum = sum(weights[k] for k in active)
    overall = (
        sum(weights[k] * active[k] for k in active) / active_weight_sum
        if active_weight_sum else 0.0
    )

    losses = {k: weights[k] * (100.0 - v) for k, v in active.items()}
    lever_key = max(losses, key=losses.get) if losses else None
    biggest_lever = None
    if lever_key:
        biggest_lever = {
            "criterion": _LEVER_LABELS[lever_key],
            "current_score": subscores[lever_key],
            "potential_point_gain": round(losses[lever_key], 1),
            "reason": _lever_reason(lever_key, structural, coherence, citation),
        }

    return FunctionalMetricResult(
        overall_score=round(overall, 2),
        band=_band_for(overall),
        structural=structural,
        coherence=coherence,
        citation_integrity=citation,
        biggest_lever=biggest_lever,
        weights_used=weights,
    )
