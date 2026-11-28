"""A Google Docs link and an upload of the same Word file must be read the same way."""
import asyncio
import io
import random
import zipfile

import main
from services.ingestion import DocumentIngestionService
from services.page_check import page_pictures
from services.parser import ManuscriptParserService

_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _docx(paragraphs, pictures) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:
        z.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        z.writestr("word/document.xml", f"<w:document {_NS}><w:body>{body}</w:body></w:document>")
        for name, data in pictures.items():
            z.writestr(f"word/media/{name}", data)
    return out.getvalue()


def _noise(size: int, seed: int) -> bytes:
    return random.Random(seed).randbytes(size)   # does not compress, like a real photograph


def _as_downloaded(data: bytes) -> bytes:
    """What the server hands on after fetching the same file from Google Docs."""
    return main._slim_download(io.BytesIO(data), len(data)) or data


def test_a_link_to_page_pictures_keeps_the_pages_that_say_what_is_wrong():
    pictures = {f"image{n}.png": _noise(300_000 + n, n) for n in range(1, 7)}
    uploaded = _docx(["Scanned copy"], pictures)
    downloaded = _as_downloaded(uploaded)
    assert page_pictures(downloaded) == page_pictures(uploaded)
    assert len(page_pictures(downloaded)) == 4
    assert len(downloaded) < len(uploaded)


def test_a_link_to_a_written_paper_drops_pictures_and_reads_the_same_text_and_sections():
    prose = "The study examines how clinical instructors guide nursing students during their hospital rotations every week. "
    paragraphs = []
    for heading in ("Introduction", "Methodology", "Results and Discussion", "Conclusion"):
        paragraphs += [heading, prose * 8]
    uploaded = _docx(paragraphs, {"image1.png": _noise(1_500_000, 7)})
    downloaded = _as_downloaded(uploaded)
    assert page_pictures(downloaded) == []      # a written paper's pictures are never needed
    read = lambda data: asyncio.run(DocumentIngestionService.extract_plaintext_from_upload(data, "paper.docx"))
    assert read(downloaded) == read(uploaded)
    assert ManuscriptParserService.parse_for_scan(read(downloaded)) == ManuscriptParserService.parse_for_scan(read(uploaded))


def test_the_direct_link_reading_matches_an_upload(monkeypatch):
    uploaded = _docx(["Introduction", "The study explains why guidance matters to students. " * 10], {})

    async def download(doc_id):
        return uploaded, "paper.docx"

    monkeypatch.setattr(main, "download_google_docx", download)
    monkeypatch.setenv("GDOC_AS_DOCX", "on")   # the test settings switch it off
    from_link = asyncio.run(main.google_doc_text("https://docs.google.com/document/d/abcdefghijklmnop/edit"))
    assert from_link == asyncio.run(DocumentIngestionService.extract_plaintext_from_upload(uploaded, "paper.docx"))
