"""The retained v5 sentence embedder serves only the advisory AI-text indicator."""

from functools import lru_cache

@lru_cache(maxsize=1)
def get_ai_text_embedder():
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer("./models/model_v5b")
