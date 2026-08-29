import re
import numpy as np
from dataclasses import dataclass, field
from typing import Dict, List, Any, Optional
from sentence_transformers import SentenceTransformer

# Minimum word count to consider a section as having real content
EMPTY_SECTION_WORD_THRESHOLD = 5

@dataclass
class CoherenceResult:
    overall_score: float
    section_scores: List[Dict[str, Any]]
    empty_sections: List[str] = field(default_factory=list)
    embeddings: Dict[str, List[float]] = field(default_factory=dict)

class EmbeddingService:
    _instance = None
    _model = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(EmbeddingService, cls).__new__(cls)
            # Singleton model initialization: loads once at app startup
            cls._model = SentenceTransformer("all-MiniLM-L6-v2")
        return cls._instance

    def _is_empty_section(self, text: str) -> bool:
        """
        Returns True if a section has no meaningful content.
        A section is empty if it has fewer than EMPTY_SECTION_WORD_THRESHOLD words.
        """
        return len(text.strip().split()) < EMPTY_SECTION_WORD_THRESHOLD

    def _chunk_and_embed(self, text: str) -> Optional[List[float]]:
        """
        Token limit safety: MiniLM-L6 context window is 256 tokens (~200 words).
        If section text exceeds 200 words, split into paragraphs/sentences,
        encode each chunk, and mean-pool the embeddings.
        Returns None if the section is empty (no meaningful content).
        """
        cleaned_text = text.strip()
        if self._is_empty_section(cleaned_text):
            # Signal empty section — caller handles this explicitly
            return None

        words = cleaned_text.split()
        if len(words) <= 200:
            embedding = self._model.encode(cleaned_text, convert_to_numpy=True, batch_size=8)
            return embedding.tolist()

        # Split text by double newlines for chunks of ~200-250 words
        paragraphs = [p.strip() for p in re.split(r'\n\s*\n', cleaned_text) if p.strip()]
        chunks = []
        for paragraph in paragraphs:
            p_words = paragraph.split()
            if len(p_words) > 300:
                # Sub-chunk paragraph if still too long
                for i in range(0, len(p_words), 250):
                    chunks.append(" ".join(p_words[i:i+250]))
            else:
                chunks.append(paragraph)

        if not chunks:
            chunks = [cleaned_text[:1000]]

        chunk_embeddings = self._model.encode(chunks, convert_to_numpy=True, batch_size=8)
        # Mean pooling across chunks to get 384-D section vector
        section_vector = np.mean(chunk_embeddings, axis=0)
        return section_vector.tolist()

    def embed_sections(self, sections: Dict[str, str]) -> Dict[str, Optional[List[float]]]:
        """
        Encodes a dict of section names to text into 384-D vector embeddings.
        Empty sections map to None instead of a zero vector.
        """
        embeddings = {}
        for section_name, section_text in sections.items():
            embeddings[section_name] = self._chunk_and_embed(section_text)
        return embeddings

    def compute_coherence(self, sections: Dict[str, str]) -> CoherenceResult:
        """
        Computes pairwise cosine similarity between adjacent CONTENT sections.
        Empty sections are excluded from scoring and surfaced in empty_sections list.
        overall_score is computed only from valid (non-empty) pairs.
        """
        if not sections:
            return CoherenceResult(overall_score=0.0, section_scores=[], empty_sections=[], embeddings={})

        embeddings = self.embed_sections(sections)
        section_names = list(sections.keys())

        # Identify empty sections
        empty_sections = [name for name, vec in embeddings.items() if vec is None]

        if len(section_names) < 2:
            # Single section: perfect self-coherence
            return CoherenceResult(
                overall_score=100.0,
                section_scores=[],
                empty_sections=empty_sections,
                embeddings={k: v for k, v in embeddings.items() if v is not None}
            )

        pairwise_scores: List[Dict[str, Any]] = []
        similarity_values: List[float] = []

        for i in range(len(section_names) - 1):
            sec_a_name = section_names[i]
            sec_b_name = section_names[i + 1]

            vec_a = embeddings[sec_a_name]
            vec_b = embeddings[sec_b_name]

            # Skip pair if either section is empty — no phantom 50.0 scores
            if vec_a is None or vec_b is None:
                skipped_note = "skipped (empty section)"
                if vec_a is None and vec_b is None:
                    skipped_note = "skipped (both sections empty)"
                elif vec_a is None:
                    skipped_note = f"skipped ('{sec_a_name}' is empty)"
                else:
                    skipped_note = f"skipped ('{sec_b_name}' is empty)"

                pairwise_scores.append({
                    "section_a": sec_a_name,
                    "section_b": sec_b_name,
                    "score": None,
                    "note": skipped_note
                })
                continue

            arr_a = np.array(vec_a)
            arr_b = np.array(vec_b)

            norm_a = np.linalg.norm(arr_a)
            norm_b = np.linalg.norm(arr_b)

            sim = float(np.dot(arr_a, arr_b) / (norm_a * norm_b))
            # Clip similarity to [-1.0, 1.0] range
            sim = max(-1.0, min(1.0, sim))

            # Convert cosine similarity (-1.0 to 1.0) to percentage (0 to 100)
            score_pct = round(((sim + 1.0) / 2.0) * 100.0, 2)
            similarity_values.append(score_pct)

            pairwise_scores.append({
                "section_a": sec_a_name,
                "section_b": sec_b_name,
                "score": score_pct,
                "note": None
            })

        # Overall score based only on valid (non-skipped) pairs
        overall_score = round(float(np.mean(similarity_values)), 2) if similarity_values else 0.0

        return CoherenceResult(
            overall_score=overall_score,
            section_scores=pairwise_scores,
            empty_sections=empty_sections,
            embeddings={k: v for k, v in embeddings.items() if v is not None}
        )

# Module-level singleton instance
embedding_service = EmbeddingService()
