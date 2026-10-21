import ipaddress
import socket
import asyncio
import httpx

def _is_safe_public_host(host: str) -> bool:
    """SSRF guard: reject any hostname that resolves to a private,
    loopback, link-local, or otherwise non-public address (e.g. cloud
    metadata endpoints or internal services)."""
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

async def _verify_request_host(request: httpx.Request) -> None:
    """
    HTTPX event hook to validate the host before every request,
    including redirects, preventing SSRF.
    """
    host = request.url.host
    if host and not await asyncio.to_thread(_is_safe_public_host, host):
        raise httpx.ConnectError(
            f"SSRF block: Host {host} is not a safe public address.",
            request=request
        )
