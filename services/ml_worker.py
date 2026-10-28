"""JSON-shaped ML batches shared by local execution and Modal containers."""

from dataclasses import asdict


def analyze_v7(sections):
    from services.v7_logic import analyze_manuscript

    return analyze_manuscript(sections)


def analyze_ai_text(sections):
    from services.embedding import get_ai_text_embedder
    from services.ai_text import compute_manuscript_ai_text_indicator

    return asdict(compute_manuscript_ai_text_indicator(sections, get_ai_text_embedder()))
