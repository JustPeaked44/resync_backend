"""v7 coherence scoring: occlusion masking for culprit sentences and a pair_weight-weighted manuscript score.

Imports torch, so it runs in the ML worker (local or Modal), never in the API process.
"""

import itertools
import os
from functools import lru_cache
from typing import Dict, List, Optional

import spacy
import torch

from services.v7_model import MultiHeadMiniLM_v7
from services.v7_scan import manuscript_score, numeric_applicable

V7_WEIGHTS_PATH = os.getenv("V7_WEIGHTS_PATH", "./models/v7_final_model.pt")
V7_BACKBONE = os.getenv("V7_BACKBONE", "./models/model_v5b")

CULPRIT_THRESHOLD = 0.65
MAX_CULPRITS = 3
MAX_SENTENCES = 40  # bounds masking passes per side, same shortcut as xai_utils
MAX_TOKENS = 256
BATCH_SIZE = 16
MIN_WORDS = 5
# The model reads about 256 tokens, which is a fraction of a real section. A link between two sections lives at their
# boundary, so each pair is rated on the END of the first section against the START of the next (not start vs start).
WINDOW_WORDS = 170
HOLISTIC_PAIR_LIMIT = 15  # how many of the lowest-rated pairs are kept for the AI review
NEIGHBOR_SLOTS = 8        # of those, up to this many go to the weakest transitions between neighboring sections
# "lowest" keeps the 15 lowest-rated pairs overall; on real theses those are all pairs of sections about different
# topics (nothing to contradict), so the default "balanced" splits the slots between transitions and cross-chapter checks.
HOLISTIC_SELECTION = os.getenv("HOLISTIC_SELECTION", "balanced")
# Section roles that are supposed to agree with each other even when they are far apart in the paper.
CROSS_ROLE_PAIRS = {frozenset(pair) for pair in (
    ("objectives", "methodology"), ("objectives", "results"), ("objectives", "discussion"), ("objectives", "conclusion"),
    ("methodology", "results"), ("methodology", "discussion"), ("methodology", "conclusion"),
    ("introduction", "conclusion"), ("abstract", "results"), ("abstract", "conclusion"),
    ("abstract", "methodology"), ("abstract", "objectives"))}

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


def _sentences(text: str) -> List[str]:
    return [s.text.strip() for s in _nlp(text or "").sents if s.text.strip()]


def head_window(text: str, limit: int = WINDOW_WORDS) -> str:
    """The first sentences of a section, up to about `limit` words."""
    taken, count = [], 0
    for sentence in _sentences(text):
        words = len(sentence.split())
        if taken and count + words > limit:
            break
        taken.append(sentence)
        count += words
        if count >= limit:
            break
    return " ".join(taken)


def tail_window(text: str, limit: int = WINDOW_WORDS) -> str:
    """The last sentences of a section, up to about `limit` words, starting on a sentence boundary."""
    taken, count = [], 0
    for sentence in reversed(_sentences(text)):
        words = len(sentence.split())
        if taken and count + words > limit:
            break
        taken.insert(0, sentence)
        count += words
        if count >= limit:
            break
    words = " ".join(taken).split()
    return " ".join(words[-limit:]) if len(words) > limit else " ".join(taken)   # one huge sentence: keep its end


def calculate_manuscript_score(pair_scores: List[Dict[str, float]]) -> Optional[float]:
    """Model-based score: the calibrated average of the four dimensions over all pairs (0 to 100), minus a flat 10 once
    if any pair is a critical mismatch (overall rating below 0.40). None when nothing was scored."""
    if not pair_scores:
        return None
    return manuscript_score([{"scores": p, "numeric_applicable": True} for p in pair_scores])[0]


def _embed(model: MultiHeadMiniLM_v7, texts: List[str]) -> "torch.Tensor":
    """One encoding per text. The model encodes each side of a pair on its own, so a section is encoded once and reused
    in every pair it belongs to."""
    chunks = []
    for start in range(0, len(texts), BATCH_SIZE):
        batch = model.tokenizer(texts[start:start + BATCH_SIZE], max_length=MAX_TOKENS, truncation=True,
                                padding=True, return_tensors="pt")
        with torch.no_grad():
            output = model.backbone(input_ids=batch["input_ids"], attention_mask=batch["attention_mask"])
            chunks.append(model.mean_pooling(output, batch["attention_mask"]))
    return torch.cat(chunks)


def _score_embeddings(model: MultiHeadMiniLM_v7, embed_a: "torch.Tensor", embed_b: "torch.Tensor") -> List[Dict[str, float]]:
    """The model's heads on pairs of encodings: the same numbers predict_pair gives, without re-encoding the text."""
    heads = {"objectives_alignment": model.head_obj, "methodology_fit": model.head_meth,
             "numeric_consistency": model.head_num, "claim_support": model.head_claim,
             "overall_coherence": model.head_overall, "pair_weight": model.head_weight}
    with torch.no_grad():
        shared = model.shared(torch.cat([embed_a, embed_b, torch.abs(embed_a - embed_b)], dim=1))
        outputs = {key: model.sigmoid(head(shared)).squeeze(-1) for key, head in heads.items()}
    return [{key: float(outputs[key][row]) for key in SCORE_KEYS} for row in range(embed_a.shape[0])]


def _select_pairs(items: List[tuple], sections: List[dict]) -> List[tuple]:
    """Keeps at most HOLISTIC_PAIR_LIMIT (pair, scores) items, lowest-rated first within each group."""
    lowest_first = lambda item: (item[1]["overall_coherence"], item[0])
    if HOLISTIC_SELECTION == "lowest":
        return sorted(items, key=lowest_first)[:HOLISTIC_PAIR_LIMIT]
    near = sorted((item for item in items if item[0][1] == item[0][0] + 1), key=lowest_first)
    far = [item for item in items if item[0][1] != item[0][0] + 1]
    role = lambda index: sections[index].get("role")
    if any(role(index) for index in range(len(sections))):   # keep only the cross-chapter pairs that should agree
        far = [item for item in far if frozenset((role(item[0][0]), role(item[0][1]))) in CROSS_ROLE_PAIRS]
    far.sort(key=lowest_first)
    near_target = min(len(near), NEIGHBOR_SLOTS, HOLISTIC_PAIR_LIMIT)
    far_take = max(0, min(len(far), HOLISTIC_PAIR_LIMIT - near_target))
    near_take = max(0, min(len(near), HOLISTIC_PAIR_LIMIT - far_take))
    return near[:near_take] + far[:far_take]


def _transition_averages(sections: List[dict], combos: List[tuple], rated: List[Dict[str, float]]) -> Dict[str, Optional[float]]:
    """The raw average rating of EVERY neighbor transition for each dimension (not just the kept ones), so the score has a
    fair starting point that does not depend on how many pairs the AI review sees. Numeric consistency only counts
    transitions where both sections have figures; None when there is nothing to average."""
    values: Dict[str, List[float]] = {key: [] for key in SCORE_KEYS if key != "pair_weight"}
    for (i, j), scores in zip(combos, rated):
        if j != i + 1:
            continue
        has_figures = numeric_applicable(sections[i]["text"], sections[j]["text"])
        for key in values:
            if key != "numeric_consistency" or has_figures:
                values[key].append(scores[key])
    return {key: (sum(items) / len(items) if items else None) for key, items in values.items()}


def analyze_manuscript(sections: List[dict]) -> dict:
    """Holistic analysis: rates EVERY pair of sections (itertools.combinations), then keeps the lowest-rated few.

    A pair of neighbors is rated on the end of the first against the start of the next (the transition); any other pair
    on the starts of both, which carry each section's topic. The lowest HOLISTIC_PAIR_LIMIT pairs go on to the AI review,
    which keeps cost and rate limits down. sections: [{"name", "text"}]; kept pairs come back in manuscript order.
    """
    model = get_model()
    usable = [index for index, section in enumerate(sections) if len((section["text"] or "").split()) >= MIN_WORDS]
    combos = list(itertools.combinations(usable, 2))
    if not combos:
        return {"manuscript_score": None, "pairs": [], "pairs_considered": 0}
    heads = {index: head_window(sections[index]["text"]) for index in usable}
    tails = {index: tail_window(sections[index]["text"]) for index in usable}
    order = {index: row for row, index in enumerate(usable)}
    head_vectors = _embed(model, [heads[index] for index in usable])
    tail_vectors = _embed(model, [tails[index] for index in usable])
    embed_a = torch.stack([(tail_vectors if j == i + 1 else head_vectors)[order[i]] for i, j in combos])
    embed_b = torch.stack([head_vectors[order[j]] for _, j in combos])
    rated = _score_embeddings(model, embed_a, embed_b)

    ranked = _select_pairs(list(zip(combos, rated)), sections)
    pairs: List[dict] = []
    for (i, j), scores in sorted(ranked, key=lambda item: item[0]):
        adjacent = j == i + 1
        entry = {"index_a": i, "index_b": j, "adjacent": adjacent,
                 "section_a": sections[i]["name"], "section_b": sections[j]["name"],
                 "scores": scores, "culprits_a": [], "culprits_b": []}
        if adjacent and scores["overall_coherence"] < CULPRIT_THRESHOLD:  # sentence hints for transitions only
            culprits = get_culprit_sentences(tails[i], heads[j], scores["overall_coherence"], model)
            entry["culprits_a"], entry["culprits_b"] = culprits["a"], culprits["b"]
        pairs.append(entry)
    return {"manuscript_score": calculate_manuscript_score([pair["scores"] for pair in pairs]),
            "pairs": pairs, "pairs_considered": len(combos),
            "transition_averages": _transition_averages(sections, combos, rated)}
