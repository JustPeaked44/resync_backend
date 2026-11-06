"""The outline shows the sections a scan would compare, without starting a scan."""

import asyncio

import pytest
from fastapi import HTTPException

import main

PAPER = ("Title of Study\n\nCHAPTER 1\nINTRODUCTION\n" + "This study is about things. " * 80
         + "\nCHAPTER 2\nREVIEW OF RELATED LITERATURE\n" + "Prior work says stuff. " * 80
         + "\nCHAPTER 3\nMETHODOLOGY\n" + "We used a survey. " * 80 + "\nREFERENCES\nSmith 2020.")


def outline(**kwargs):
    return asyncio.run(main.manuscript_outline(file=kwargs.get("file"), doc_url=kwargs.get("doc_url"), authenticated_user_id="user"))


def test_a_google_doc_shows_its_title_length_and_sections(monkeypatch):
    async def fetch(url):
        return PAPER
    monkeypatch.setattr(main.DocumentIngestionService, "fetch_plaintext_from_gdoc", staticmethod(fetch))
    result = outline(doc_url="https://docs.google.com/document/d/abc/edit")
    assert result.title == "Title of Study" and result.structure == "headings" and result.pages >= 1
    assert [section.name for section in result.sections][:3] == ["introduction", "review of related literature", "methodology"]
    assert result.sections[0].words > 300


def test_sending_nothing_is_refused():
    with pytest.raises(HTTPException) as error:
        outline()
    assert error.value.status_code == 400


def test_the_same_link_is_not_fetched_twice_in_a_row(monkeypatch):
    calls = []

    async def fetch(url):
        calls.append(url)
        return PAPER
    monkeypatch.setattr(main.DocumentIngestionService, "fetch_plaintext_from_gdoc", staticmethod(fetch))
    main._outline_cache.clear()
    first = outline(doc_url="https://docs.google.com/document/d/zzz/edit")
    second = outline(doc_url="https://docs.google.com/document/d/zzz/edit")
    assert calls == ["https://docs.google.com/document/d/zzz/edit"] and second.words == first.words


def test_a_google_doc_that_never_answers_gives_a_clear_timeout(monkeypatch):
    async def never(url):
        await asyncio.sleep(5)
    monkeypatch.setattr(main.DocumentIngestionService, "fetch_plaintext_from_gdoc", staticmethod(never))
    monkeypatch.setattr(main, "OUTLINE_FETCH_SECONDS", 0.05)
    main._outline_cache.clear()
    with pytest.raises(HTTPException) as error:
        outline(doc_url="https://docs.google.com/document/d/slow/edit")
    assert error.value.status_code == 504


class _Stream:
    """A fake streamed HTTP reply."""

    def __init__(self, status_code, headers, content):
        self.status_code, self.headers, self._content = status_code, headers, content

    async def __aenter__(self): return self
    async def __aexit__(self, *_): return False

    async def aiter_bytes(self, size):
        for start in range(0, len(self._content), size):
            yield self._content[start:start + size]


def _fake_google(monkeypatch, reply):
    class Client:
        def __init__(self, **_): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False
        def stream(self, method, url):
            assert url.endswith("/export?format=docx")
            return reply

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", Client)
    main._gdoc_docx_cache.clear()


def test_a_google_doc_comes_back_as_a_word_file(monkeypatch):
    _fake_google(monkeypatch, _Stream(200, {"content-type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                            "content-disposition": "attachment; filename=\"My Capstone.docx\""}, b"PK\x03\x04docx-bytes"))
    response = asyncio.run(main.google_doc_as_docx(doc_url="https://docs.google.com/document/d/abcDEF123456/edit", authenticated_user_id="u"))
    assert response.body.startswith(b"PK") and response.headers["x-doc-filename"] == "My%20Capstone.docx"


def test_a_private_google_doc_is_refused_with_a_sharing_hint(monkeypatch):
    _fake_google(monkeypatch, _Stream(200, {"content-type": "text/html"}, b"<html>sign in</html>"))
    with pytest.raises(HTTPException) as error:
        asyncio.run(main.google_doc_as_docx(doc_url="https://docs.google.com/document/d/privateDOC1234/edit", authenticated_user_id="u"))
    assert error.value.status_code == 403


def test_a_google_doc_over_25mb_is_opened_once_its_pictures_are_removed(monkeypatch):
    import io
    import zipfile
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:
        z.writestr("word/document.xml", "<w:document>text</w:document>")
        z.writestr("word/media/image1.png", b"\x00" * (30 * 1024 * 1024))   # a 30 MB picture
    _fake_google(monkeypatch, _Stream(200, {"content-type": "application/octet-stream"}, out.getvalue()))
    response = asyncio.run(main.google_doc_as_docx(doc_url="https://docs.google.com/document/d/hugeDOC123456/edit", authenticated_user_id="u"))
    assert len(response.body) < 100_000


def test_a_google_link_is_read_as_the_same_word_export_an_upload_would_be(monkeypatch):
    monkeypatch.setenv("GDOC_AS_DOCX", "on")
    seen = {}

    async def download(doc_id):
        seen["downloaded"] = doc_id
        return b"PK-bytes", "Paper.docx"

    async def extract(data, name):
        return "Introduction" + chr(10) + "Text read from the Word export."

    async def plain(url):
        raise AssertionError("the plain-text export must not be used when the Word export works")

    monkeypatch.setattr(main, "download_google_docx", download)
    monkeypatch.setattr(main.DocumentIngestionService, "extract_plaintext_from_upload", staticmethod(extract))
    monkeypatch.setattr(main.DocumentIngestionService, "fetch_plaintext_from_gdoc", staticmethod(plain))
    text = asyncio.run(main.google_doc_text("https://docs.google.com/document/d/abcDEF123456/edit"))
    assert "Word export" in text and seen["downloaded"] == "abcDEF123456"


def test_a_google_link_falls_back_to_the_text_export(monkeypatch):
    monkeypatch.setenv("GDOC_AS_DOCX", "on")

    async def download(doc_id):
        raise HTTPException(status_code=504, detail="slow")

    async def plain(url):
        return "Introduction" + chr(10) + "Fallback text."

    monkeypatch.setattr(main, "download_google_docx", download)
    monkeypatch.setattr(main.DocumentIngestionService, "fetch_plaintext_from_gdoc", staticmethod(plain))
    assert asyncio.run(main.google_doc_text("https://docs.google.com/document/d/abcDEF123456/edit")) == "Introduction" + chr(10) + "Fallback text."
