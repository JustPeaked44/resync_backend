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


def source_order_output(data):
    return old_output(data).replace("\nAfter table\nfirst | second\nmerged", "\nfirst | second\nmerged\nAfter table")


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


def test_docx_output_places_tables_in_source_order():
    data = make_document()
    actual = asyncio.run(DocumentIngestionService.extract_plaintext_from_upload(data, "test.docx"))
    assert actual == source_order_output(data)


def test_picture_heavy_docx_matches_old_output():
    data = make_document(picture=True)
    assert len(data) >= 20_000_000
    actual = asyncio.run(DocumentIngestionService.extract_plaintext_from_upload(data, "test.docx"))
    assert actual == source_order_output(data)


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


def test_tables_follow_their_headings_in_template_sections():
    from services.parser import ManuscriptParserService

    doc = Document()
    doc.add_heading("Introduction", 1)
    doc.add_paragraph("Opening text gives enough context for this section.")
    table = doc.add_table(rows=2, cols=3)
    table.cell(0, 0).text = "40"
    table.cell(0, 1).text = "3"
    table.cell(0, 2).text = "Agile Sprint Plan"
    table.cell(1, 0).text = "same"
    table.cell(1, 1).text = "same"
    doc.add_heading("Methodology", 1)
    doc.add_paragraph("A second section contains its own description.")
    last = doc.add_table(rows=1, cols=21)
    for index, cell in enumerate(last.rows[0].cells):
        cell.text = f"column {index}"
    output = io.BytesIO()
    doc.save(output)
    text = asyncio.run(DocumentIngestionService.extract_plaintext_from_upload(output.getvalue(), "test.docx"))
    sections = ManuscriptParserService.parse_manuscript_sections(text, ["Introduction", "Methodology"])["parsed_sections"]
    assert "40 | 3 | Agile Sprint Plan" in sections["Introduction"]
    assert "same" in sections["Introduction"]
    assert "column 20" in sections["Methodology"]
    assert "column 20" not in sections["Introduction"]


def test_docx_metadata_keeps_source_cells_without_changing_text():
    from services.ingestion import extract_docx_text, extract_docx_with_metadata

    doc = Document()
    doc.add_paragraph("Before")
    table = doc.add_table(rows=2, cols=3)
    for cell, value in zip(table.rows[0].cells, ["same", "same", ""]):
        cell.text = value
    table.rows[1].cells[1].text = "second"
    doc.add_paragraph("After")
    out = io.BytesIO()
    doc.save(out)
    text, records = extract_docx_with_metadata(out.getvalue())
    assert text == extract_docx_text(out.getvalue()) == "Before\nsame\nsecond\nAfter"
    assert [record["text"] for record in records] == ["Before", "same", "second", "After"]
    assert records[1]["cells"] == ["same", "same", ""]
    assert records[2]["cells"] == ["", "second", ""]
    assert records[1]["table"] == records[2]["table"]
    assert records[0]["cells"] is None


def test_parser_retains_tables_and_original_headings():
    from services.parser import ManuscriptParserService

    doc = Document()
    for heading in ("Introduction", "Methodology", "Results"):
        doc.add_heading(heading, 1)
        doc.add_paragraph("This section has enough words to be considered useful.")
        table = doc.add_table(rows=1, cols=3)
        table.cell(0, 0).text = heading + " cell"
        table.cell(0, 2).text = "end"
    out = io.BytesIO()
    doc.save(out)
    text, records = asyncio.run(DocumentIngestionService.extract_plaintext_from_upload(out.getvalue(), "test.docx", include_metadata=True))
    result = ManuscriptParserService.parse_manuscript_sections(text, None, records)
    assert result["display_names"]["introduction"] == "Introduction"
    assert result["section_blocks"]["introduction"][1]["rows"] == [["Introduction cell", "", "end"]]
    assert result["section_blocks"]["results"][1]["rows"] == [["Results cell", "", "end"]]
    template = ManuscriptParserService.parse_manuscript_sections(text, ["INTRODUCTION", "Methodology", "Results"], records)
    assert template["display_names"]["INTRODUCTION"] == "INTRODUCTION"


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
    import main
    assert main._failure_message("12345678", error.value) == DOCX_COMPLEX_MESSAGE
    assert main._safe_refusal(DOCX_COMPLEX_MESSAGE)


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


def test_table_stream_stops_early_on_oversized_text():
    import pytest
    from fastapi import HTTPException
    from services.ingestion import _check_element_text, DOCX_COMPLEX_MESSAGE
    xml = (b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           b'<w:body><w:tbl><w:tr><w:tc><w:p><w:r><w:t>' + b'x' * 600_000
           + b'</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>' + b' ' * 200_000)
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
    assert actual == source_order_output(data)
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
