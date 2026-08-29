"""
Advisory, dependency-light AI-generated-text stylometric indicator.

Deliberately uses only what the pipeline already has loaded: the spaCy
en_core_web_sm model (imported from services.parser so it is never loaded
twice in the same process) and the all-mpnet-base-v2 sentence embeddings
already computed by services.embedding. No new model downloads -- a
GPT-2-class perplexity model would not fit the Render free-tier memory
budget this project deploys to.

IMPORTANT, read before wiring this into any UI copy: this measures
surface stylistic uniformity (sentence-length burstiness, lexical
diversity, transition-phrase density, opening-word repetition, and
sentence-embedding self-similarity). It is NOT a watermark detector.
Real generative-model watermarks (e.g. Google SynthID) are cryptographic
signals embedded at the token-sampling level and cannot be recovered from
output text by any third party without the issuing model's own detection
service. Formal academic writing already exhibits many of the same
surface features this indicator looks for (frequent transition words,
domain-jargon repetition, uniform APA-style sentence structure), so a
well-written human capstone chapter can legitimately score 40-60 here.
This must never be used to block a submission or as a standalone
plagiarism/academic-integrity charge -- advisory only, excluded from the
manuscript score, always shown with DISCLAIMER attached.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np

from services.parser import nlp

DISCLAIMER = (
    "Advisory only, not an academic-integrity determination. This is a stylometric "
    "heuristic over surface features (sentence-length uniformity, lexical diversity, "
    "transition-phrase density, opening-word repetition, and sentence-embedding "
    "self-similarity) -- it cannot verify authorship and cannot detect real generative-"
    "model watermarks (e.g. Google SynthID), which are cryptographic signals embedded "
    "at the token-sampling level and are not recoverable from output text by any third "
    "party without the issuing model's own detection service. Formal academic prose "
    "already exhibits many of these same surface features, so well-written human "
    "capstone chapters can score in the 40-60 range; this indicator must never be used "
    "to block a submission or as a plagiarism/integrity charge on its own."
)

_TRANSITION_PHRASES = [
    "furthermore", "moreover", "in addition", "additionally", "it is important to note",
    "it should be noted", "in conclusion", "to conclude", "this underscores",
    "this highlights", "plays a crucial role", "plays a vital role", "delve into",
    "by leveraging", "a testament to", "in today's world", "in the realm of",
    "it is worth noting", "on the other hand", "as a result", "consequently",
    "thus", "hence", "overall", "in summary", "notably", "significantly",
]
_TRANSITION_RE = re.compile(
    r'\b(' + '|'.join(re.escape(p) for p in _TRANSITION_PHRASES) + r')\b',
    re.IGNORECASE,
)

# Reasoned, deliberately wide reference bands -- NOT fit to a labeled
# human-vs-AI corpus (none exists for this domain). Recalibrate once
# enough real scanned manuscripts with an honest self-reported ground
# truth accumulate.
_REFERENCE_BANDS = {
    "burstiness": (0.30, 0.75),
    "lexical_diversity": (0.55, 0.85),
    "transition_per_1000w": (1.0, 18.0),
    "opening_entropy_norm": (0.5, 1.0),
    "self_similarity": (0.20, 0.60),
}

_FEATURE_WEIGHTS = {
    "inverse_burstiness": 0.20,
    "inverse_lexical_diversity": 0.15,
    "transition_density": 0.25,
    "inverse_opening_entropy": 0.15,
    "sentence_self_similarity": 0.25,
}

MIN_SENTENCES_FOR_SIGNAL = 5


@dataclass
class AITextIndicatorResult:
    score: Optional[float]                        # 0-100, higher = more AI-like signal; None if too short
    feature_breakdown: Dict[str, float] = field(default_factory=dict)
    disclaimer: str = DISCLAIMER


@dataclass
class ManuscriptAITextResult:
    overall_score: Optional[float]
    section_scores: Dict[str, Optional[float]] = field(default_factory=dict)
    flagged_sections: List[str] = field(default_factory=list)
    disclaimer: str = DISCLAIMER


def _minmax(value: float, lo: float, hi: float, invert: bool = False) -> float:
    v = max(lo, min(hi, value))
    norm = (v - lo) / (hi - lo) if hi > lo else 0.0
    return 1.0 - norm if invert else norm


def _mattr(text: str, window: int = 50) -> float:
    """Moving-average type-token ratio -- avoids the length bias of raw
    TTR by averaging the type-token ratio over sliding windows."""
    tokens = [t.lower() for t in re.findall(r"[A-Za-z']+", text)]
    if len(tokens) < window:
        if not tokens:
            return 0.0
        return len(set(tokens)) / len(tokens)
    ratios = []
    for i in range(0, len(tokens) - window + 1, max(1, window // 2)):
        chunk = tokens[i:i + window]
        ratios.append(len(set(chunk)) / len(chunk))
    return float(np.mean(ratios)) if ratios else 0.0


def _transition_density(text: str) -> float:
    words = max(1, len(text.split()))
    hits = len(_TRANSITION_RE.findall(text))
    return hits / words * 1000.0


def _opening_entropy(sentences: List[str]) -> float:
    openers = []
    for s in sentences:
        m = re.match(r"[A-Za-z']+", s.strip())
        openers.append(m.group(0).lower() if m else "")
    if not openers:
        return 1.0
    counts: Dict[str, int] = {}
    for o in openers:
        counts[o] = counts.get(o, 0) + 1
    n = len(openers)
    probs = [c / n for c in counts.values()]
    entropy = -sum(p * np.log2(p) for p in probs if p > 0)
    max_entropy = np.log2(len(counts)) if len(counts) > 1 else 1.0
    return float(entropy / max_entropy) if max_entropy > 0 else 1.0


def compute_ai_text_indicator(
    section_text: str,
    embedder: Any = None,
) -> AITextIndicatorResult:
    """embedder must expose .encode(list[str]) -> np.ndarray, matching the
    sentence-transformers SentenceTransformer interface (the pipeline
    should pass in embedding_service._model). If omitted, the
    sentence-embedding self-similarity feature is skipped and its weight
    is redistributed across the remaining features."""
    text = (section_text or "").strip()
    if not text:
        return AITextIndicatorResult(score=None, feature_breakdown={})

    doc = nlp(text)
    sents = [s.text.strip() for s in doc.sents if s.text.strip()]
    if len(sents) < MIN_SENTENCES_FOR_SIGNAL:
        return AITextIndicatorResult(score=None, feature_breakdown={})

    lens = np.array([max(1, len(s.split())) for s in sents])
    burstiness = float(np.std(lens) / np.mean(lens)) if np.mean(lens) else 0.0
    lexical_diversity = _mattr(text, window=50)
    transition_per_1000w = _transition_density(text)
    opening_entropy_norm = _opening_entropy(sents)

    feats = {
        "inverse_burstiness": _minmax(burstiness, *_REFERENCE_BANDS["burstiness"], invert=True),
        "inverse_lexical_diversity": _minmax(lexical_diversity, *_REFERENCE_BANDS["lexical_diversity"], invert=True),
        "transition_density": _minmax(transition_per_1000w, *_REFERENCE_BANDS["transition_per_1000w"]),
        "inverse_opening_entropy": _minmax(opening_entropy_norm, *_REFERENCE_BANDS["opening_entropy_norm"], invert=True),
    }
    weights = dict(_FEATURE_WEIGHTS)

    if embedder is not None and len(sents) >= MIN_SENTENCES_FOR_SIGNAL:
        try:
            sent_vecs = np.array(embedder.encode(sents, convert_to_numpy=True, batch_size=8))
            norms = np.linalg.norm(sent_vecs, axis=1, keepdims=True)
            norms[norms == 0] = 1e-9
            unit = sent_vecs / norms
            sim_matrix = unit @ unit.T
            n = len(sents)
            self_similarity = float((sim_matrix.sum() - n) / (n * (n - 1))) if n > 1 else 0.0
            feats["sentence_self_similarity"] = _minmax(self_similarity, *_REFERENCE_BANDS["self_similarity"])
        except Exception:
            weights.pop("sentence_self_similarity", None)
    else:
        weights.pop("sentence_self_similarity", None)

    weight_sum = sum(weights[k] for k in feats if k in weights)
    if weight_sum <= 0:
        return AITextIndicatorResult(score=None, feature_breakdown=feats)

    score = 100.0 * sum(weights[k] * feats[k] for k in feats if k in weights) / weight_sum
    return AITextIndicatorResult(score=round(score, 1), feature_breakdown={k: round(v, 3) for k, v in feats.items()})


def compute_manuscript_ai_text_indicator(
    parsed_sections: Dict[str, str],
    embedder: Any = None,
    flag_threshold_delta: float = 15.0,
) -> ManuscriptAITextResult:
    """Runs the per-section indicator across every section, then flags
    sections that are outliers relative to the *same manuscript's own*
    mean -- a more defensible per-document signal than an absolute
    universal cutoff with no calibration data behind it."""
    section_scores: Dict[str, Optional[float]] = {}
    for heading, text in parsed_sections.items():
        result = compute_ai_text_indicator(text, embedder=embedder)
        section_scores[heading] = result.score

    valid = {h: s for h, s in section_scores.items() if s is not None}
    if not valid:
        return ManuscriptAITextResult(overall_score=None, section_scores=section_scores)

    mean_score = float(np.mean(list(valid.values())))
    flagged = [h for h, s in valid.items() if s - mean_score >= flag_threshold_delta]

    return ManuscriptAITextResult(
        overall_score=round(mean_score, 1),
        section_scores=section_scores,
        flagged_sections=flagged,
    )
