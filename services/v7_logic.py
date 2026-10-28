"""v7 coherence scoring: occlusion masking for culprit sentences and a pair_weight-weighted manuscript score.

Imports torch, so it runs in the ML worker (local or Modal), never in the API process.
"""

import os
from functools import lru_cache
from typing import Dict, List, Optional

import spacy
import torch

from services.v7_model import MultiHeadMiniLM_v7

V7_WEIGHTS_PATH = os.getenv("V7_WEIGHTS_PATH", "./models/v7_final_model.pt")
V7_BACKBONE = os.getenv("V7_BACKBONE", "./models/model_v5b")

CULPRIT_THRESHOLD = 0.65
MAX_CULPRITS = 3
MAX_SENTENCES = 40  # bounds masking passes per side, same shortcut as xai_utils
MAX_TOKENS = 256
BATCH_SIZE = 16
MIN_WORDS = 5

SCORE_KEYS = (
    "objectives_alignment", "methodology_fit", "numeric_consistency",
    "claim_support", "overall_coherence", "pair_weight",
)

_nlp = spacy.blank("en")
_nlp.add_pipe("sentencizer")


@lru_cache(maxsize=1)
def get_model() -> MultiHeadMiniLM_v7:
    if not os.path.exists(V7_WEIGHTS_PATH):
        raise FileNotFoundError(f"v7 weights not found at {V7_WEIGHTS_PATH}")
    model = MultiHeadMiniLM_v7(V7_BACKBONE)
    state = torch.load(V7_WEIGHTS_PATH, map_location="cpu", weights_only=True)
    if isinstance(state, dict):
        state = state.get("model_state_dict") or state.get("state_dict") or state
    model.load_state_dict(state)
    model.eval()
    return model


def _predict_batch(model: MultiHeadMiniLM_v7, texts_a: List[str], texts_b: List[str]) -> List[Dict[str, float]]:
    results: List[Dict[str, float]] = []
    for start in range(0, len(texts_a), BATCH_SIZE):
        a = model.tokenizer(texts_a[start:start + BATCH_SIZE], max_length=MAX_TOKENS,
                            truncation=True, padding=True, return_tensors="pt")
        b = model.tokenizer(texts_b[start:start + BATCH_SIZE], max_length=MAX_TOKENS,
                            truncation=True, padding=True, return_tensors="pt")
        with torch.no_grad():
            out = model(a["input_ids"], a["attention_mask"], b["input_ids"], b["attention_mask"])
        for i in range(out["overall_coherence"].shape[0]):
            results.append({key: float(out[key][i].item()) for key in SCORE_KEYS})
    return results


def predict_pair(text_a: str, text_b: str, model: Optional[MultiHeadMiniLM_v7] = None) -> Dict[str, float]:
    return _predict_batch(model or get_model(), [text_a], [text_b])[0]


def get_culprit_sentences(text_a: str, text_b: str, baseline: Optional[float] = None,
                          model: Optional[MultiHeadMiniLM_v7] = None) -> Dict[str, List[dict]]:
    """Occlusion loop: drop one sentence at a time and re-score the pair.

    A culprit is a sentence whose removal RAISES overall_coherence (it is dragging the pair down).
    Returns the top MAX_CULPRITS per side, largest gain first.
    """
    model = model or get_model()
    if baseline is None:
        baseline = predict_pair(text_a, text_b, model)["overall_coherence"]

    culprits: Dict[str, List[dict]] = {"a": [], "b": []}
    for side, text in (("a", text_a), ("b", text_b)):
        sentences = [s.text.strip() for s in _nlp(text).sents if s.text.strip()][:MAX_SENTENCES]
        if len(sentences) < 2:
            continue  # masking the only sentence leaves nothing to score
        masked = [" ".join(sentences[:i] + sentences[i + 1:]) for i in range(len(sentences))]
        if side == "a":
            scored = _predict_batch(model, masked, [text_b] * len(masked))
        else:
            scored = _predict_batch(model, [text_a] * len(masked), masked)
        deltas = [
            {"sentence": sentence, "delta": round(result["overall_coherence"] - baseline, 4)}
            for sentence, result in zip(sentences, scored)
        ]
        culprits[side] = sorted((d for d in deltas if d["delta"] > 0), key=lambda d: -d["delta"])[:MAX_CULPRITS]
    return culprits


def calculate_manuscript_score(pair_scores: List[Dict[str, float]]) -> Optional[float]:
    """Mean of overall_coherence weighted by each pair's pair_weight head. None when nothing was scored."""
    if not pair_scores:
        return None
    total_weight = sum(p["pair_weight"] for p in pair_scores)
    if total_weight <= 1e-9:
        return round(sum(p["overall_coherence"] for p in pair_scores) / len(pair_scores), 4)
    return round(sum(p["overall_coherence"] * p["pair_weight"] for p in pair_scores) / total_weight, 4)


def analyze_manuscript(sections: List[dict]) -> dict:
    """Scores each adjacent pair of sections in manuscript order. sections: [{"name", "text"}]."""
    model = get_model()
    pairs: List[dict] = []
    scored: List[Dict[str, float]] = []
    for index in range(len(sections) - 1):
        a, b = sections[index], sections[index + 1]
        entry = {"order": index + 1, "section_a": a["name"], "section_b": b["name"]}
        if any(len((s["text"] or "").split()) < MIN_WORDS for s in (a, b)):
            pairs.append({**entry, "skipped": f"A section has fewer than {MIN_WORDS} words."})
            continue
        scores = predict_pair(a["text"], b["text"], model)
        culprits = {"a": [], "b": []}
        if scores["overall_coherence"] < CULPRIT_THRESHOLD:
            culprits = get_culprit_sentences(a["text"], b["text"], scores["overall_coherence"], model)
        scored.append(scores)
        pairs.append({**entry, "scores": scores, "culprits_a": culprits["a"], "culprits_b": culprits["b"]})
    return {"manuscript_score": calculate_manuscript_score(scored), "pairs": pairs}
