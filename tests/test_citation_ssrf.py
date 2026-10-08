import pytest
import httpx
from unittest.mock import patch, MagicMock

@pytest.mark.asyncio
async def test_ssrf_hook_rejects_private_ips():
    from services.citation import _ssrf_request_hook

    # Create a dummy request representing a target of a redirect
    request = httpx.Request("GET", "http://127.0.0.1/admin")

    with patch("services.citation._is_safe_public_host", return_value=False):
        with pytest.raises(httpx.ConnectError) as exc_info:
            await _ssrf_request_hook(request)

        assert "Unsafe or unresolvable host: 127.0.0.1" in str(exc_info.value)
        assert exc_info.value.request == request

@pytest.mark.asyncio
async def test_ssrf_hook_allows_public_ips():
    from services.citation import _ssrf_request_hook

    request = httpx.Request("GET", "http://example.com")

    with patch("services.citation._is_safe_public_host", return_value=True):
        # Should not raise any exception
        await _ssrf_request_hook(request)
