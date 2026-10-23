import spacy

from services.embedding import predict_coherence


_nlp = spacy.load("en_core_web_sm")


def explain_pair(text_a: str, text_b: str) -> dict:
    baseline = predict_coherence(text_a, text_b)
    result = {
        "baseline": baseline,
        "evidence_a": {"top_supporting": [], "top_undermining": []},
        "evidence_b": {"top_supporting": [], "top_undermining": []},
    }
    if baseline["overall_coherence"] is None:
        return result

    # shortcut: first 40 sentences per side bound latency; raise if real sections need broader coverage.
    sentences_a = [s.text.strip() for s in _nlp(text_a).sents][:40]
    sentences_b = [s.text.strip() for s in _nlp(text_b).sents][:40]
    for side, sentences in (("evidence_a", sentences_a), ("evidence_b", sentences_b)):
        deltas = []
        for index, sentence in enumerate(sentences):
            masked = " ".join(sentences[:index] + sentences[index + 1:])
            score = predict_coherence(masked, text_b) if side == "evidence_a" else predict_coherence(text_a, masked)
            if score["overall_coherence"] is not None:
                deltas.append({"sentence": sentence, "delta": baseline["overall_coherence"] - score["overall_coherence"]})
        result[side]["top_supporting"] = sorted((d for d in deltas if d["delta"] > 0), key=lambda d: -d["delta"])[:3]
        result[side]["top_undermining"] = sorted((d for d in deltas if d["delta"] < 0), key=lambda d: d["delta"])[:3]
    return result
