import pytest
from fastapi import HTTPException

from services.ingestion import DocumentIngestionService, MAX_DOCUMENT_CHARS


def test_a_document_up_to_600000_characters_is_accepted_and_more_is_refused_with_its_real_size():
    assert MAX_DOCUMENT_CHARS == 600_000
    assert DocumentIngestionService.enforce_size_limits("a" * 500_316) == "a" * 500_316   # the document that was refused at 500,000
    assert len(DocumentIngestionService.enforce_size_limits("a" * 600_000)) == 600_000
    with pytest.raises(HTTPException) as refused:
        DocumentIngestionService.enforce_size_limits("a" * 600_001)
    assert refused.value.status_code == 413
    assert refused.value.detail == "Document is too large to process (600,001 characters). Max limit is 600,000 characters."
