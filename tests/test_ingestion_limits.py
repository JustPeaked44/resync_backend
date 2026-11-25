import asyncio
from io import BytesIO

import pytest
from fastapi import HTTPException

from services.ingestion import DocumentIngestionService, MAX_DOCUMENT_CHARS, MAX_TOTAL_CHARS

TOO_LONG = "Document is too large to process. It has more than 600,000 characters of text, which is the limit."
ROW = "Anna Cruz | 25 | 62.5 percent"


def test_only_text_counts_toward_the_600000_limit_and_the_message_does_not_claim_an_exact_size():
    assert MAX_DOCUMENT_CHARS == 600_000
    paragraphs = "\n".join(["a" * 99] * 6_000)   # 600,000 characters of text
    assert DocumentIngestionService.enforce_size_limits(paragraphs) == paragraphs
    with pytest.raises(HTTPException) as refused:
        DocumentIngestionService.enforce_size_limits(paragraphs + "\n" + "b" * 100)
    assert refused.value.status_code == 413 and refused.value.detail == TOO_LONG


def test_table_rows_do_not_count_toward_the_limit_but_a_huge_total_is_still_refused():
    prose = "\n".join(["a" * 99] * 1_000)   # 100,000 characters of text
    tables = "\n".join([ROW] * 40_000)   # about 1.2 million characters of table rows
    assert len(prose + tables) > MAX_DOCUMENT_CHARS
    assert DocumentIngestionService.enforce_size_limits(prose + "\n" + tables)
    with pytest.raises(HTTPException) as refused:
        DocumentIngestionService.enforce_size_limits(prose + "\n" + "\n".join([ROW] * 110_000))
    assert refused.value.status_code == 413 and "tables" in refused.value.detail and f"{MAX_TOTAL_CHARS:,}" in refused.value.detail


def _build(paragraph_count, table_rows):
    from docx import Document
    doc = Document()
    for n in range(paragraph_count):
        doc.add_paragraph(f"Paragraph {n} " + "word " * 60)
    for _ in range(3):
        table = doc.add_table(rows=0, cols=3)
        for r in range(table_rows):
            cells = table.add_row().cells
            for c, text in enumerate((f"Respondent group {r}", "Fourth year students", "62.5 percent of the sample")):
                cells[c].text = text
    buffer = BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def test_a_word_file_with_big_tables_is_read_and_one_with_too_much_text_is_refused():
    # about 100,000 characters of text and about 600,000 in tables: refused at 500,000 before, read now
    text = asyncio.run(DocumentIngestionService.extract_plaintext_from_upload(_build(300, 4_500), "tables.docx"))
    assert len(text) > MAX_DOCUMENT_CHARS and "Respondent group 4499 | Fourth year students" in text
    # about 700,000 characters of text: refused whatever the tables hold
    with pytest.raises(HTTPException) as refused:
        asyncio.run(DocumentIngestionService.extract_plaintext_from_upload(_build(2_100, 1), "long.docx"))
    assert refused.value.status_code == 413 and refused.value.detail == TOO_LONG
