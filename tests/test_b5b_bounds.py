import io
import zipfile

import pytest
from fastapi import HTTPException

import main
from services.ingestion import bounded_download, validate_docx_archive


class Upload:
    def __init__(self, data):
        self.stream = io.BytesIO(data)

    async def read(self, size):
        return self.stream.read(size)


@pytest.mark.asyncio
async def test_upload_stops_at_limit_plus_one(monkeypatch):
    monkeypatch.setattr(main, "MAX_UPLOAD_BYTES", 8)
    assert await main._read_upload(Upload(b"12345678"), "large") == b"12345678"
    upload = Upload(b"123456789extra")
    with pytest.raises(HTTPException) as exc:
        await main._read_upload(upload, "large")
    assert exc.value.status_code == 413
    assert upload.stream.tell() == 9


@pytest.mark.asyncio
async def test_download_stops_after_overflow():
    class Response:
        async def aiter_bytes(self, size):
            yield b"1234"
            yield b"5678"
            pytest.fail("read past overflow")
    with pytest.raises(HTTPException) as exc:
        await bounded_download(Response(), 7)
    assert exc.value.status_code == 413


def test_docx_entry_count_refused():
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        for i in range(20001):
            archive.writestr(str(i), b"")
    with pytest.raises(HTTPException) as exc:
        validate_docx_archive(data.getvalue())
    assert exc.value.status_code == 413


def test_cache_hash_matches_for_mutable_upload_buffer():
    from services.section_cache import SectionCache
    assert SectionCache.content_hash(bytearray(b"abc")) == SectionCache.content_hash(b"abc")


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", [main.start_scan, main.run_scan])
async def test_large_base64_refused_before_provision(monkeypatch, endpoint):
    monkeypatch.setattr(main, "MAX_UPLOAD_BYTES", 6)
    async def provision(*args, **kwargs):
        pytest.fail("oversized base64 reached provisioning")
    monkeypatch.setattr(main, "_provision_analysis_run", provision)
    req = main.ScanRequest(user_id="a", file_content_base64="A" * 9)
    with pytest.raises(HTTPException) as exc:
        await endpoint(req, authenticated_user_id="a")
    assert exc.value.status_code == 413
    assert not main._scan_reservations
