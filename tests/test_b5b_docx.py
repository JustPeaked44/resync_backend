import asyncio
import io
import os
import struct

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches

from services.ingestion import DocumentIngestionService


def old_output(data):
    doc = Document(io.BytesIO(data))
    lines = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            unique = []
            for cell in cells:
                if not unique or cell != unique[-1]:
                    unique.append(cell)
            if unique:
                lines.append(" | ".join(unique))
    return DocumentIngestionService.strip_document_noise("\n".join(lines))


def make_document(picture=False):
    doc = Document()
    doc.add_heading("Heading", 1)
    doc.add_paragraph("List entry", style="List Bullet")
    p = doc.add_paragraph("Before ")
    hyperlink = OxmlElement("w:hyperlink")
    run = OxmlElement("w:r")
    text = OxmlElement("w:t")
    text.text = "linked text"
    run.append(text)
    hyperlink.append(run)
    p._p.append(hyperlink)
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "first"
    table.cell(0, 1).text = "second"
    table.cell(1, 0).merge(table.cell(1, 1)).text = "merged"
    doc.add_paragraph("After table")
    if picture:
        width, height = 2800, 2500
        pixel_data = os.urandom(width * height * 3)
        header = b"BM" + struct.pack("<IHHI", len(pixel_data) + 54, 0, 0, 54)
        header += struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0, len(pixel_data), 2835, 2835, 0, 0)
        doc.add_picture(io.BytesIO(header + pixel_data), width=Inches(2))
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def test_docx_output_matches_old_paragraphs_tables_and_hyperlinks():
    data = make_document()
    actual = asyncio.run(DocumentIngestionService.extract_plaintext_from_upload(data, "test.docx"))
    assert actual == old_output(data)
    assert actual.index("After table") < actual.index("first")


def test_picture_heavy_docx_matches_old_output():
    data = make_document(picture=True)
    assert len(data) >= 20_000_000
    actual = asyncio.run(DocumentIngestionService.extract_plaintext_from_upload(data, "test.docx"))
    assert actual == old_output(data)


def test_docx_without_tables_matches_old_output():
    doc = Document()
    doc.add_heading("Title", 1)
    doc.add_paragraph("List item", style="List Number")
    doc.add_paragraph("Text with\ttabs and\nline breaks.")
    out = io.BytesIO()
    doc.save(out)
    data = out.getvalue()
    actual = asyncio.run(DocumentIngestionService.extract_plaintext_from_upload(data, "test.docx"))
    assert actual == old_output(data)


def test_docx_above_character_cap_is_refused():
    from fastapi import HTTPException
    import pytest
    doc = Document()
    doc.add_paragraph("x" * 500_001)
    out = io.BytesIO()
    doc.save(out)
    with pytest.raises(HTTPException) as error:
        asyncio.run(DocumentIngestionService.extract_plaintext_from_upload(out.getvalue(), "test.docx"))
    assert error.value.status_code == 413


def test_oversized_document_xml_is_refused():
    import zipfile
    import pytest
    from fastapi import HTTPException
    from services.ingestion import MAX_DOCUMENT_XML_BYTES, DOCX_COMPLEX_MESSAGE
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b" " * (MAX_DOCUMENT_XML_BYTES + 1))
    with pytest.raises(HTTPException) as error:
        asyncio.run(DocumentIngestionService.extract_plaintext_from_upload(out.getvalue(), "large.docx"))
    assert (error.value.status_code, error.value.detail) == (413, DOCX_COMPLEX_MESSAGE)


def test_element_stream_stops_early_on_oversized_text():
    import pytest
    from fastapi import HTTPException
    from services.ingestion import _check_element_text, DOCX_COMPLEX_MESSAGE
    xml = (b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           b'<w:body><w:p><w:r><w:t>' + b'x' * 600_000 + b'</w:t></w:r></w:p></w:body></w:document>' + b' ' * 200_000)
    stream = io.BytesIO(xml)
    with pytest.raises(HTTPException) as error:
        _check_element_text(stream)
    assert (error.value.status_code, error.value.detail) == (413, DOCX_COMPLEX_MESSAGE)
    assert stream.tell() < len(xml)


def test_bytearray_input_is_viewed_without_full_copy(monkeypatch):
    import services.ingestion as ingestion
    data = bytearray(make_document())
    original = ingestion.BufferReader
    seen = []
    class InspectReader(original):
        def __init__(self, source):
            super().__init__(source)
            seen.append(self.data.obj is source)
    monkeypatch.setattr(ingestion, "BufferReader", InspectReader)
    actual = asyncio.run(DocumentIngestionService.extract_plaintext_from_upload(data, "test.docx"))
    assert actual == old_output(data)
    assert seen == [True, True]


def test_xml_heavy_document_matches_old_output():
    doc = Document()
    for i in range(1200):
        doc.add_paragraph(f"Paragraph {i}: text for comparing extraction.")
    out = io.BytesIO()
    doc.save(out)
    data = out.getvalue()
    actual = asyncio.run(DocumentIngestionService.extract_plaintext_from_upload(data, "test.docx"))
    assert actual == old_output(data)
