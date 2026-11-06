import asyncio

import pytest
from fastapi import HTTPException

import main


@pytest.fixture(autouse=True)
def reset_reservations():
    main._scan_reservations.clear()
    main._scan_reservations_by_id.clear()
    yield
    main._scan_reservations.clear()
    main._scan_reservations_by_id.clear()


def test_count_bytes_and_user_limits(monkeypatch):
    monkeypatch.setattr(main, "MAX_QUEUED_SCANS", 2)
    monkeypatch.setattr(main, "MAX_QUEUED_BYTES", 10)
    a = main._reserve_scan("a", 4)
    main._reserve_scan("b", 6)
    with pytest.raises(HTTPException) as exc:
        main._reserve_scan("c", 0)
    assert (exc.value.status_code, exc.value.detail) == (429, main.QUEUE_FULL_MESSAGE)
    main._release_reservation(a)
    with pytest.raises(HTTPException) as exc:
        main._reserve_scan("c", 5)
    assert (exc.value.status_code, exc.value.detail) == (429, main.QUEUE_FULL_MESSAGE)
    main._reserve_scan("b", 0, queued=False)
    with pytest.raises(HTTPException) as exc:
        main._reserve_scan("b", 0)
    assert (exc.value.status_code, exc.value.detail) == (429, main.USER_FULL_MESSAGE)


@pytest.mark.asyncio
async def test_refusal_precedes_provision(monkeypatch):
    monkeypatch.setattr(main, "MAX_QUEUED_SCANS", 0)
    async def provision(*args, **kwargs):
        pytest.fail("refused scan created a row")
    monkeypatch.setattr(main, "_provision_analysis_run", provision)
    req = main.ScanRequest(user_id="a", doc_url="https://example.com")
    with pytest.raises(HTTPException) as exc:
        await main.start_scan(req, authenticated_user_id="a")
    assert (exc.value.status_code, exc.value.detail) == (429, main.QUEUE_FULL_MESSAGE)
    assert not main._scan_reservations


@pytest.mark.asyncio
async def test_worker_releases_queued_bytes_at_start_and_reservation_after(monkeypatch):
    token = main._reserve_scan("a", 10)
    main._scan_reservations_by_id["id"] = token
    req = main.ScanRequest(user_id="a", doc_url="https://example.com")
    async def run(*args):
        assert main._scan_reservations[token]["bytes"] == 0
        assert not main._scan_reservations[token]["queued"]
    monkeypatch.setattr(main, "_run_scan_job", run)
    main._scan_queue.append((req, "id", None, None, bytearray(10), "x.docx"))
    await main._drain_scan_queue()
    assert not main._scan_reservations
    assert not main._scan_reservations_by_id
    assert not main._scan_queue
