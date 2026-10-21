import asyncio
import ipaddress
import socket
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

def _is_safe_public_host(host: str) -> bool:
    """
    SSRF guard: reject any hostname that resolves to a private,
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

async def _verify_request_host(request: httpx.Request) -> None:
    """
    Async event hook to validate the host before every request, including redirects.
    This prevents SSRF via HTTP redirects by checking every hop.
    """
    host = request.url.host
    if not host:
        raise httpx.ConnectError("No host provided", request=request)

    # Safely wrap the synchronous socket resolution in asyncio.to_thread
    is_safe = await asyncio.to_thread(_is_safe_public_host, host)
    if not is_safe:
        logger.warning(f"SSRF attempt blocked: unsafe host {host}")
        raise httpx.ConnectError(f"Host {host} is not a safe public host", request=request)
