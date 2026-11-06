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


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["start", "run", "upload", "upload_run"])
@pytest.mark.parametrize("limit", ["count", "bytes", "user"])
async def test_every_scan_route_refuses_before_creating_row(monkeypatch, route, limit):
    import io
    from starlette.datastructures import UploadFile

    rows = []
    async def provision(*args, **kwargs):
        rows.append("created")
        raise AssertionError("provisioning must not be called")
    monkeypatch.setattr(main, "_provision_analysis_run", provision)
    monkeypatch.setattr(main, "MAX_QUEUED_SCANS", 8)
    monkeypatch.setattr(main, "MAX_QUEUED_BYTES", 100)
    expected = "Many scans are waiting right now. Please try again in a few minutes."
    if limit == "count":
        monkeypatch.setattr(main, "MAX_QUEUED_SCANS", 0)
    elif limit == "bytes":
        monkeypatch.setattr(main, "MAX_QUEUED_BYTES", 3)
    else:
        main._reserve_scan("user", 0, queued=False)
        main._reserve_scan("user", 0, queued=False)
        expected = "You already have scans in progress. Please wait for them to finish."

    if route in ("start", "run"):
        req = main.ScanRequest(user_id="user", file_content_base64="AAAA")
        call = (main.start_scan if route == "start" else main.run_scan)(req, authenticated_user_id="user")
    else:
        upload = UploadFile(file=io.BytesIO(b"1234"), filename="test.docx")
        handler = main.upload_and_start_scan if route == "upload" else main.upload_and_run_scan
        call = handler(file=upload, user_id="user", manuscript_id=None, manuscript_title=None,
                       template_toc=None, style_reference_url=None, authenticated_user_id="user")
    with pytest.raises(HTTPException) as exc:
        await call
    assert (exc.value.status_code, exc.value.detail) == (429, expected)
    assert rows == []


@pytest.mark.asyncio
async def test_concurrent_requests_reserve_before_provision(monkeypatch):
    monkeypatch.setattr(main, "MAX_QUEUED_SCANS", 1)
    entered = asyncio.Event()
    release = asyncio.Event()
    rows = []
    async def provision(*args, **kwargs):
        rows.append("created")
        entered.set()
        await release.wait()
        raise RuntimeError("fake provisioning stopped")
    monkeypatch.setattr(main, "_provision_analysis_run", provision)
    first = main.ScanRequest(user_id="first", doc_url="https://example.com")
    second = main.ScanRequest(user_id="second", doc_url="https://example.com")
    task = asyncio.create_task(main.start_scan(first, authenticated_user_id="first"))
    await entered.wait()
    with pytest.raises(HTTPException) as exc:
        await main.start_scan(second, authenticated_user_id="second")
    assert (exc.value.status_code, exc.value.detail) == (429, "Many scans are waiting right now. Please try again in a few minutes.")
    assert rows == ["created"]
    release.set()
    with pytest.raises(RuntimeError):
        await task
    assert not main._scan_reservations
