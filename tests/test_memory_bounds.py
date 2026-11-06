import asyncio
import io
import os
import subprocess
import sys
import zipfile

import pytest
from fastapi import HTTPException

from services import citation, ingestion
from services.parser import ManuscriptParserService


def test_parser_sections_and_sentence_split_are_unchanged():
    text = 'Introduction\n' + ('A sentence. Another sentence? ' * 250) + '\nConclusion\nThe end.'
    result = ManuscriptParserService.parse_manuscript_sections(text, ['Introduction', 'Conclusion'])
    assert result['section_roles']['Introduction'] == 'introduction'
    assert result['parsed_sections']['Introduction'] == ('A sentence. Another sentence? ' * 250).strip()
    assert result['parsed_sections']['Conclusion'] == 'The end.'


def test_parser_does_not_import_spacy():
    code = "import sys; from services.parser import ManuscriptParserService as P; P.parse_manuscript_sections('Introduction\\nA sentence.'); assert 'spacy' not in sys.modules"
    subprocess.run([sys.executable, '-c', code], check=True, env={**os.environ, 'PYTHON_DOTENV_DISABLED': '1'})


def test_bounded_read_stops_at_limit_and_returns_mutable_buffer():
    stream = io.BytesIO(b'abcdefghij')
    assert ingestion.bounded_read(io.BytesIO(b'abc'), 3) == bytearray(b'abc')
    with pytest.raises(ValueError):
        ingestion.bounded_read(stream, 4)
    assert stream.tell() == 5


def test_download_stops_during_stream_and_returns_bytearray():
    class Response:
        consumed = 0
        async def aiter_bytes(self, size):
            for chunk in (b'ab', b'cd', b'ef', b'gh'):
                self.consumed += 1
                yield chunk
    async def run():
        response = Response()
        with pytest.raises(HTTPException) as exc:
            await ingestion.bounded_download(response, 3)
        assert exc.value.status_code == 413
        assert response.consumed == 2
        assert isinstance(await ingestion.bounded_download(Response(), 8), bytearray)
    asyncio.run(run())


def test_zip_declared_size_limits_and_safe_message(monkeypatch):
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('one', b'a' * 8)
        archive.writestr('two', b'b' * 8)
    monkeypatch.setattr(ingestion, 'MAX_ARCHIVE_MEMBER_BYTES', 7)
    with pytest.raises(HTTPException) as exc:
        ingestion.validate_docx_archive(data.getvalue())
    assert exc.value.status_code == 413 and 'too large' in exc.value.detail
    monkeypatch.setattr(ingestion, 'MAX_ARCHIVE_MEMBER_BYTES', 8)
    monkeypatch.setattr(ingestion, 'MAX_DECOMPRESSED_BYTES', 15)
    with pytest.raises(HTTPException) as exc:
        ingestion.validate_docx_archive(data.getvalue())
    assert exc.value.status_code == 413 and 'too large' in exc.value.detail


def test_verification_cache_evicts_least_recently_used(monkeypatch):
    monkeypatch.setattr(citation, 'MAX_VERIFICATION_CACHE_ENTRIES', 2)
    async def run():
        cache = citation.VerificationCache()
        await cache.set('a', {'v': 1}); await cache.set('b', {'v': 2})
        await cache.get('a'); await cache.set('c', {'v': 3})
        assert await cache.get('b') is None
        assert await cache.get('a') == {'v': 1}
    asyncio.run(run())


def test_domain_throttle_evicts_lru_and_preserves_active_domains(monkeypatch):
    monkeypatch.setattr(citation, 'MAX_THROTTLE_DOMAINS', 2)
    async def run():
        throttle = citation.DomainThrottle(min_interval=0)
        async with throttle.acquire('a'): pass
        async with throttle.acquire('b'): pass
        async with throttle.acquire('a'): pass
        async with throttle.acquire('c'): pass
        assert list(throttle._sems) == ['a', 'c']
        assert 'b' not in throttle._last
        async with throttle.acquire('a'):
            async with throttle.acquire('c'):
                async with throttle.acquire('d'):
                    assert len(throttle._sems) == 2
                    assert throttle._overflow_active == 1
        assert throttle._overflow_active == 0
    asyncio.run(run())


def test_google_text_export_streams_and_refuses_oversize(monkeypatch):
    class Response:
        status_code = 200
        encoding = 'utf-8'
        consumed = 0
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def aiter_bytes(self, size):
            for chunk in (b'hello', b' world', b' extra'):
                self.consumed += 1
                yield chunk
    response = Response()
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def stream(self, method, url): return response
    monkeypatch.setattr(ingestion.httpx, 'AsyncClient', Client)
    original = ingestion.bounded_download
    async def small_limit(response): return await original(response, 6)
    monkeypatch.setattr(ingestion, 'bounded_download', small_limit)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(ingestion.DocumentIngestionService.fetch_plaintext_from_gdoc('https://docs.google.com/document/d/abc/edit'))
    assert exc.value.status_code == 413
    assert response.consumed == 2


def test_upload_refuses_oversized_archive_before_docx_parsing(monkeypatch):
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('word/document.xml', b'a' * 16)
    monkeypatch.setattr(ingestion, 'MAX_ARCHIVE_MEMBER_BYTES', 15)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(ingestion.DocumentIngestionService.extract_plaintext_from_upload(data.getvalue(), 'paper.docx'))
    assert exc.value.status_code == 413
    assert 'too large' in exc.value.detail


def test_legacy_nlp_import_loads_once_when_requested(monkeypatch):
    import types
    from services import parser
    calls = []
    class Pipeline:
        def add_pipe(self, name): calls.append(name)
    pipeline = Pipeline()
    def blank(language):
        calls.append(language)
        return pipeline
    monkeypatch.setitem(sys.modules, 'spacy', types.SimpleNamespace(blank=blank))
    monkeypatch.delitem(parser.__dict__, 'nlp', raising=False)
    try:
        assert parser.nlp is pipeline
        assert parser.nlp is pipeline
        assert calls == ['en', 'sentencizer']
    finally:
        parser.__dict__.pop('nlp', None)
