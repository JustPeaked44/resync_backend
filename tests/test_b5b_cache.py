import main


def test_preview_cache_expiry_count_and_bytes(monkeypatch):
    cache = main._manuscript_cache
    cache.clear()
    monkeypatch.setattr(main, "CACHE_MAX_ENTRIES", 2)
    monkeypatch.setattr(main, "CACHE_MAX_BYTES", 100000)
    cache.update({"old": {"expires_at": 1, "text": "x"}, "a": {"expires_at": 10, "text": "a"}, "b": {"expires_at": 10, "text": "b"}})
    main._preview_cache_prune(2)
    assert list(cache) == ["a", "b"]
    cache["c"] = {"expires_at": 10, "text": "c"}
    main._preview_cache_prune(2)
    assert list(cache) == ["b", "c"]
    monkeypatch.setattr(main, "CACHE_MAX_BYTES", 1)
    main._preview_cache_prune(2)
    assert not cache


def test_outline_cache_expiry_and_bytes(monkeypatch):
    cache = main._outline_cache
    cache.clear()
    response = main.OutlineResponse(title="a", words=1, pages=1, structure="headings", sections=[], excerpt=[])
    cache["expired"] = (0, response)
    cache["new"] = (300, response)
    monkeypatch.setattr(main, "CACHE_MAX_BYTES", 100000)
    main._outline_cache_prune(301)
    assert list(cache) == ["new"]
    monkeypatch.setattr(main, "CACHE_MAX_BYTES", 1)
    main._outline_cache_prune(301)
    assert not cache
