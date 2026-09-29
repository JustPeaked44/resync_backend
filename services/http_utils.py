import asyncio
import ipaddress
import socket
import httpx

def is_safe_public_host(host: str) -> bool:
    """SSRF guard: reject any hostname that resolves to a private,
    loopback, link-local, or otherwise non-public address.
    """
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

async def ssrf_hook(request: httpx.Request):
    """Event hook for httpx to prevent SSRF by checking the host."""
    host = request.url.host
    if not host or not await asyncio.to_thread(is_safe_public_host, host):
        raise httpx.ConnectError(f"Unsafe host: {host}", request=request)
