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
