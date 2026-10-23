"""JSON-shaped ML batches shared by local execution and Modal containers."""

from dataclasses import asdict


def analyze_batch(parsed_sections, role_texts, role_pairs):
    from services.embedding import embedding_service, predict_coherence
    from services.ai_text import compute_manuscript_ai_text_indicator

    return {
        "coherence": asdict(embedding_service.compute_coherence(parsed_sections)),
        "v6_dimensions": {
            f"{role_a}::{role_b}": predict_coherence(role_texts.get(role_a), role_texts.get(role_b))
            for role_a, role_b in role_pairs
        },
        "ai_text": asdict(compute_manuscript_ai_text_indicator(parsed_sections, embedding_service._model)),
    }


def explain_batch(pairs):
    from services.xai_utils import explain_pair

    return {key: explain_pair(text_a, text_b) for key, text_a, text_b in pairs}
