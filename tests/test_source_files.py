import asyncio
import io

import pytest
from fastapi import HTTPException, UploadFile

import main
from services import source_files

DOCX = b"PK\x03\x04" + b"\x00" * 64


class FakeBucket:
    def __init__(self, store):
        self.store = store

    def upload(self, path, data, options):
        self.store[path] = data

    def download(self, path):
        if path not in self.store:
            raise KeyError(path)
        return self.store[path]


class FakeClient:
    def __init__(self, owner="user-1", store=None):
        self.owner, self.store = owner, {} if store is None else store
        self.storage = type("S", (), {"from_": lambda _self, name: FakeBucket(self.store)})()

    def table(self, _name):
        return self

    def select(self, *_):
        return self

    def eq(self, *_):
        return self

    def execute(self):
        return type("R", (), {"data": [{"user_id": self.owner}]})()


def upload(name="thesis.docx", data=DOCX):
    return UploadFile(filename=name, file=io.BytesIO(data))


@pytest.fixture
def client(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(main.DatabasePersistenceService, "get_client", classmethod(lambda cls: fake))
    monkeypatch.setenv("SOURCE_FILES_BUCKET", "manuscript-sources")
    return fake


def test_it_is_off_until_a_bucket_is_named(monkeypatch):
    monkeypatch.delenv("SOURCE_FILES_BUCKET", raising=False)
    assert asyncio.run(main.put_source_file("scan-1", upload(), "user-1")) == {"stored": False, "reason": "disabled"}
    with pytest.raises(HTTPException) as caught:
        asyncio.run(main.get_source_file("scan-1", "user-1"))
    assert caught.value.status_code == 404


def test_the_owner_can_store_and_get_back_the_original_file(client):
    assert asyncio.run(main.put_source_file("scan-1", upload(), "user-1")) == {"stored": True}
    assert client.store["user-1/scan-1.docx"] == DOCX
    response = asyncio.run(main.get_source_file("scan-1", "user-1"))
    assert response.body == DOCX and response.headers["cache-control"] == "private, no-store"


def test_someone_else_cannot_store_or_read_a_scans_file(client):
    with pytest.raises(HTTPException) as caught:
        asyncio.run(main.put_source_file("scan-1", upload(), "intruder"))
    assert caught.value.status_code == 404
    asyncio.run(main.put_source_file("scan-1", upload(), "user-1"))
    with pytest.raises(HTTPException) as caught:
        asyncio.run(main.get_source_file("scan-1", "intruder"))
    assert caught.value.status_code == 404


def test_only_real_word_files_are_kept(client):
    with pytest.raises(HTTPException) as caught:
        asyncio.run(main.put_source_file("scan-1", upload(name="thesis.pdf"), "user-1"))
    assert caught.value.status_code == 400
    with pytest.raises(HTTPException) as caught:
        asyncio.run(main.put_source_file("scan-1", upload(data=b"just some text"), "user-1"))
    assert caught.value.status_code == 400
    assert client.store == {}


def test_a_storage_failure_never_raises_and_a_missing_file_is_a_clean_404(client, monkeypatch):
    class Broken:
        def from_(self, _name):
            raise RuntimeError("storage down")

    client.storage = Broken()
    assert asyncio.run(main.put_source_file("scan-1", upload(), "user-1")) == {"stored": False}
    with pytest.raises(HTTPException) as caught:
        asyncio.run(main.get_source_file("scan-1", "user-1"))
    assert caught.value.status_code == 404
    assert source_files.looks_like_docx(DOCX) and not source_files.looks_like_docx(b"%PDF-1.4")
