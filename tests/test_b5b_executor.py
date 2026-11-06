import asyncio
import threading

import pytest

import main


@pytest.mark.asyncio
async def test_lifespan_bounds_and_stops_executor(monkeypatch):
    monkeypatch.setattr(main, "INTERRUPTED_GRACE_SECONDS", 3600)
    async with main.application_lifespan(main.app):
        executor = asyncio.get_running_loop()._default_executor
        assert executor._max_workers == 4
        assert await asyncio.to_thread(threading.get_ident)
    assert executor._shutdown


def test_buffer_reader_reuses_original_storage():
    data = bytearray(b"abcdef")
    reader = main._BufferReader(data)
    data[1] = ord("Z")
    assert reader.read(3) == b"aZc"
    assert reader.seek(-2, 2) == 4
    assert reader.read() == b"ef"
