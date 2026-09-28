import pytest
import httpx
from services.citation import _ssrf_request_hook

@pytest.mark.asyncio
async def test_ssrf_request_hook_rejects_internal_ip():
    request = httpx.Request("GET", "http://169.254.169.254/latest/meta-data/")
    with pytest.raises(httpx.ConnectError):
        await _ssrf_request_hook(request)

@pytest.mark.asyncio
async def test_ssrf_request_hook_allows_public_ip():
    request = httpx.Request("GET", "https://example.com/")
    # Should not raise an exception
    await _ssrf_request_hook(request)
