import asyncio
import ipaddress
import socket
import httpx

def _is_safe_public_host(host: str) -> bool:
    """SSRF guard: checks if a hostname resolves to a public IP.
    Rejects any hostname that resolves to a private, loopback, link-local,
    or otherwise non-public address (e.g., cloud metadata endpoints or
    internal services)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True

async def _verify_request_host(request: httpx.Request):
    """
    HTTPX event hook to validate the host before every request, including redirects.
    This prevents SSRF via HTTP redirects by ensuring that even if the initial URL is safe,
    a redirect to a private IP (e.g. 127.0.0.1 or 169.254.169.254) will be blocked.
    """
    host = request.url.host
    if host:
        is_safe = await asyncio.to_thread(_is_safe_public_host, host)
        if not is_safe:
            raise httpx.ConnectError(f"SSRF block: unsafe host '{host}'", request=request)
