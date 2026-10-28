import asyncio
from types import SimpleNamespace

import httpx

import main
from services.ingestion import looks_like_html_document


def test_html_login_detection_requires_document_opening():
    assert looks_like_html_document("\ufeff  <!DOCTYPE html><html>login")
    assert looks_like_html_document("<html>login")
    assert not looks_like_html_document("Title\nExample contains <html in prose.")


def test_preview_keeps_literal_html_text_and_explains_fetch_failure(monkeypatch):
    class DB:
        def table(self, *_): return self
        def select(self, *_): return self
        def eq(self, *_): return self
        def execute(self):
            return SimpleNamespace(data=[{"user_id": "owner", "result_json": {
                "doc_url": "https://docs.google.com/document/d/example/edit"}}])

    monkeypatch.setattr(main.DatabasePersistenceService, "get_client", lambda: DB())
    original_client = httpx.AsyncClient
    state = {"fail": False}

    def handler(request):
        if state["fail"]:
            raise httpx.ConnectError("blocked", request=request)
        return httpx.Response(200, text="\ufeffTitle\nThis text contains <html literally.")

    def client(**kwargs):
        return original_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(main.httpx, "AsyncClient", client)
    first = asyncio.run(main.get_manuscript_preview("preview-a", True, "owner"))
    assert first["available"] is True
    assert first["text"] == "Title\nThis text contains <html literally."
    state["fail"] = True
    second = asyncio.run(main.get_manuscript_preview("preview-b", True, "owner"))
    assert second == {"available": False, "reason": "google_fetch_failed"}
