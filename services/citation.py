import asyncio
import ipaddress
import logging
import re
import socket
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

from services.reference_parser import (
    ParsedReferenceEntry,
    build_parsed_entries,
    cross_match_citations,
    extract_intext_citations,
    fuzzy_title_match,
    surname_key_variants,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Status ladder -- replaces the old plain accessible/broken boolean.
# ---------------------------------------------------------------------------


class CitationStatus(str, Enum):
    NO_LINK = "no_link"
    VERIFIED_METADATA = "verified_metadata"   # Crossref resolved + title/year match
    METADATA_MISMATCH = "metadata_mismatch"   # Crossref resolved but details disagree
    ACCESSIBLE = "accessible"                 # HTTP 2xx, no DOI metadata available
    BOT_WALL = "bot_wall"                     # 401/403/429/999 -- publisher paywall/bot-defense
    BROKEN = "broken"                         # 404/410/DNS failure/timeout after retry
    UNKNOWN_ERROR = "unknown_error"           # 5xx / transient / unexpected


BOT_WALL_CODES = {401, 403, 429, 999}
DEAD_CODES = {404, 410, 400, 405, 501}

# Back-compat boolean mapping for the legacy citation_is_accessible column
# and any older frontend code path still keyed off it.
_ACCESSIBLE_BOOL = {
    CitationStatus.VERIFIED_METADATA: True,
    CitationStatus.ACCESSIBLE: True,
    CitationStatus.BOT_WALL: True,       # not proven dead -- benefit of the doubt
    CitationStatus.NO_LINK: True,        # never claimed to be a live link
    CitationStatus.METADATA_MISMATCH: False,
    CitationStatus.BROKEN: False,
    CitationStatus.UNKNOWN_ERROR: False,
}

CROSSREF_HEADERS = {
    "User-Agent": "ResyncCapstoneBot/1.0 (mailto:resync-support@example.edu)"
}
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Hard ceiling on the references blob passed in -- purely a DoS/latency
# guard, not a functional limit. The old 20,000-char cap was already too
# small for a real 150-reference APA list and was silently truncating
# real bibliographies mid-entry.
MAX_REFERENCES_TEXT_CHARS = 150_000


# ---------------------------------------------------------------------------
# Per-domain politeness + process-lifetime verification cache
# ---------------------------------------------------------------------------


class VerificationCache:
    """In-memory, process-lifetime cache. Doesn't need to survive a Render
    dyno restart -- it only needs to de-duplicate repeated DOIs/URLs
    within a single scan (and ideally across scans while the dyno is warm)."""

    def __init__(self) -> None:
        self._store: Dict[str, Dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Optional[Dict[str, Any]]:
        async with self._lock:
            return self._store.get(key)

    async def set(self, key: str, value: Dict[str, Any]) -> None:
        async with self._lock:
            self._store[key] = value


class DomainThrottle:
    """Caps concurrency per-domain (on top of the global semaphore) and
    enforces a minimum interval between requests to the same host, so a
    reference list with 15 links to the same publisher doesn't hammer it."""

    def __init__(self, max_concurrent_per_domain: int = 2, min_interval: float = 0.5) -> None:
        self._sems: Dict[str, asyncio.Semaphore] = {}
        self._last: Dict[str, float] = {}
        self._lock = asyncio.Lock()
        self.max_concurrent = max_concurrent_per_domain
        self.min_interval = min_interval

    def acquire(self, domain: str) -> "_DomainSlot":
        return _DomainSlot(self, domain)


class _DomainSlot:
    def __init__(self, throttle: DomainThrottle, domain: str) -> None:
        self.throttle = throttle
        self.domain = domain
        self._sem: Optional[asyncio.Semaphore] = None

    async def __aenter__(self) -> "_DomainSlot":
        async with self.throttle._lock:
            sem = self.throttle._sems.setdefault(
                self.domain, asyncio.Semaphore(self.throttle.max_concurrent)
            )
        await sem.acquire()
        loop = asyncio.get_event_loop()
        last = self.throttle._last.get(self.domain, 0.0)
        wait = self.throttle.min_interval - (loop.time() - last)
        if wait > 0:
            await asyncio.sleep(wait)
        self.throttle._last[self.domain] = loop.time()
        self._sem = sem
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._sem is not None:
            self._sem.release()


_verification_cache = VerificationCache()
_domain_throttle = DomainThrottle()


# ---------------------------------------------------------------------------
# Crossref metadata resolution
# ---------------------------------------------------------------------------


async def _resolve_doi_metadata(doi: str, client: httpx.AsyncClient) -> Optional[Dict[str, Any]]:
    url = f"https://api.crossref.org/works/{quote(doi, safe='/')}"
    try:
        resp = await client.get(url, headers=CROSSREF_HEADERS, timeout=8.0)
        if resp.status_code != 200:
            return None
        msg = resp.json().get("message", {})
        date_parts = (
            msg.get("published-print", {}).get("date-parts")
            or msg.get("published-online", {}).get("date-parts")
            or msg.get("issued", {}).get("date-parts")
            or [[None]]
        )
        titles = msg.get("title") or [""]
        return {
            "title": titles[0] if titles else "",
            "year": date_parts[0][0] if date_parts and date_parts[0] else None,
            "authors": [a.get("family", "") for a in msg.get("author", []) if a.get("family")],
        }
    except Exception:
        return None


def _is_safe_public_host(host: str) -> bool:
    """SSRF guard: reference links come from user-submitted manuscript text,
    so before fetching one, reject any hostname that resolves to a private,
    loopback, link-local, or otherwise non-public address (e.g. cloud
    metadata endpoints or internal services). Only the initial URL is
    checked -- a redirect to an internal address is a known residual gap,
    acceptable here since httpx's automatic redirect handling doesn't
    expose a per-hop hook without materially more code."""
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


async def _check_http_reachability(url: str, client: httpx.AsyncClient) -> int:
    """Returns a real HTTP status code, or a synthetic negative code for a
    network-level failure (so classify_http_status can still bucket it)."""
    try:
        host = httpx.URL(url).host
        if not host or not await asyncio.to_thread(_is_safe_public_host, host):
            return -4
    except Exception:
        return -4
    try:
        resp = await client.head(url, headers=BROWSER_HEADERS, timeout=10.0, follow_redirects=True)
        if resp.status_code in (405, 501) or resp.status_code >= 500:
            resp = await client.get(url, headers=BROWSER_HEADERS, timeout=12.0, follow_redirects=True)
        return resp.status_code
    except httpx.TimeoutException:
        try:
            resp = await client.get(url, headers=BROWSER_HEADERS, timeout=15.0, follow_redirects=True)
            return resp.status_code
        except Exception:
            return -1
    except httpx.ConnectError:
        return -2
    except Exception:
        return -3


def _classify_http_status(code: int) -> CitationStatus:
    if 200 <= code < 300:
        return CitationStatus.ACCESSIBLE
    if code in BOT_WALL_CODES:
        return CitationStatus.BOT_WALL
    if code in DEAD_CODES or code in (-1, -2, -4):
        return CitationStatus.BROKEN
    return CitationStatus.UNKNOWN_ERROR


async def _verify_entry_link(
    entry_text: str,
    links: List[Dict[str, Any]],
    client: httpx.AsyncClient,
) -> Dict[str, Any]:
    if not links:
        return {"status": CitationStatus.NO_LINK, "tier": "none"}

    doi_link = next((l for l in links if l["kind"] == "doi"), None)
    if doi_link:
        cached = await _verification_cache.get(doi_link["doi"])
        if cached is not None:
            return cached
        meta = await _resolve_doi_metadata(doi_link["doi"], client)
        if meta and meta.get("title"):
            matched, score = fuzzy_title_match(meta["title"], entry_text)
            entry_year_m = re.search(r'\((\d{4})[a-z]?\)', entry_text)
            entry_year = int(entry_year_m.group(1)) if entry_year_m else None
            year_ok = (
                entry_year is None or meta.get("year") is None
                or abs(entry_year - meta["year"]) <= 1
            )
            result = {
                "status": (
                    CitationStatus.VERIFIED_METADATA if (matched and year_ok)
                    else CitationStatus.METADATA_MISMATCH
                ),
                "tier": "crossref_metadata",
                "crossref_title": meta["title"],
                "crossref_year": meta.get("year"),
                "title_match_score": score,
                "primary_link": doi_link["url"],
            }
            await _verification_cache.set(doi_link["doi"], result)
            return result
        # Crossref didn't resolve or had no usable title -- fall through to
        # a plain HTTP reachability check on the DOI URL itself.

    primary = doi_link["url"] if doi_link else links[0]["url"]
    cached = await _verification_cache.get(primary)
    if cached is not None:
        return cached

    try:
        domain = httpx.URL(primary).host or "unknown"
    except Exception:
        domain = "unknown"

    async with _domain_throttle.acquire(domain):
        code = await _check_http_reachability(primary, client)

    result = {
        "status": _classify_http_status(code),
        "tier": "http",
        "http_status_code": code,
        "primary_link": primary,
    }
    await _verification_cache.set(primary, result)
    return result


# ---------------------------------------------------------------------------
# Public service
# ---------------------------------------------------------------------------


class CitationAuditService:
    """Segments a references block into individual entries, verifies each
    entry's link (Crossref metadata first, then HTTP reachability), and
    cross-matches in-text citations against the reference list."""

    @classmethod
    async def audit_citations(
        cls,
        references_text: str,
        body_text: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Returns {"citations": [...], "crossmatch": {...}}. "citations" has
        one dict per parsed reference entry (not per URL found), each
        carrying the real segmented reference text, extracted links,
        verification status, and Crossref cross-check result.
        "crossmatch" (only populated when body_text is supplied) has
        "orphan_intext_citations" and "uncited_references".
        """
        empty_crossmatch = {"orphan_intext_citations": [], "uncited_references": []}
        if not references_text or not isinstance(references_text, str) or not references_text.strip():
            return {"citations": [], "crossmatch": empty_crossmatch}

        references_text = references_text[:MAX_REFERENCES_TEXT_CHARS]

        parsed_entries, _strategy = build_parsed_entries(references_text)
        if not parsed_entries:
            return {"citations": [], "crossmatch": empty_crossmatch}

        cross_match: Dict[str, List[Dict[str, Any]]] = {
            "orphan_intext_citations": [],
            "uncited_references": [],
        }
        cited_keys: set = set()
        if body_text:
            intext = extract_intext_citations(body_text)
            ref_dicts = [
                {
                    "first_author_surname": e.first_author_surname,
                    "year_parsed": e.year_parsed,
                    "citation_raw_reference_text": e.raw_text,
                }
                for e in parsed_entries
            ]
            cross_match = cross_match_citations(intext, ref_dicts)
            for c in intext:
                if c["year"] != "n.d.":
                    cited_keys.add((c["surname"].lower(), c["year"]))

        sem = asyncio.Semaphore(10)

        async with httpx.AsyncClient(follow_redirects=True) as client:

            async def _audit_one(entry: ParsedReferenceEntry) -> Dict[str, Any]:
                async with sem:
                    try:
                        verification = await _verify_entry_link(entry.raw_text, entry.links, client)
                    except Exception as exc:
                        logger.warning("Citation verification failed for entry %d: %s", entry.index, exc)
                        verification = {"status": CitationStatus.UNKNOWN_ERROR, "tier": "error"}

                is_cited = False
                if entry.first_author_surname and entry.year_parsed:
                    year_str = str(entry.year_parsed)
                    is_cited = any(
                        (variant, year_str) in cited_keys
                        for variant in surname_key_variants(entry.first_author_surname)
                    )

                status: CitationStatus = verification["status"]
                return {
                    "citation_id": str(uuid.uuid4()),
                    "citation_entry_index": entry.index,
                    "citation_raw_reference_text": entry.raw_text,
                    "citation_authors_parsed": entry.authors_parsed,
                    "citation_year_parsed": entry.year_parsed,
                    "citation_links": entry.links,
                    "citation_primary_link": verification.get("primary_link"),
                    "citation_status": status.value,
                    "citation_is_accessible": _ACCESSIBLE_BOOL.get(status, False),
                    "citation_verification_tier": verification.get("tier"),
                    "citation_crossref_title": verification.get("crossref_title"),
                    "citation_crossref_year": verification.get("crossref_year"),
                    "citation_title_match_score": verification.get("title_match_score"),
                    "citation_http_status_code": verification.get("http_status_code"),
                    "citation_is_cited_in_text": is_cited if body_text else None,
                    "citation_verified_at": datetime.now(timezone.utc).isoformat(),
                    "citation_error_detail": None,
                }

            results = await asyncio.gather(*(_audit_one(e) for e in parsed_entries))

        return {"citations": list(results), "crossmatch": cross_match}


# Global instance (kept for parity with the existing singleton pattern
# used elsewhere in services/).
citation_service = CitationAuditService()
