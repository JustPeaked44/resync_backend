"""Parsed manuscripts kept in Supabase.

A scan reads the uploaded file and parses it into sections before anything is compared. The result is saved here, so the
same file scanned again by the same person skips both steps, and /analyze_v7 can take a manuscript_id instead of the
sections. Every call is best effort: if the table is missing or Supabase is slow, the scan simply parses the file as it
always did, and a cache problem never fails a scan.
"""

import asyncio
import hashlib
import json
import logging
import os
from typing import Any, Dict, Optional

from services.db_service import DatabasePersistenceService

logger = logging.getLogger("resync.section_cache")

TABLE = "manuscript_section_cache"
# Bump this when the parser changes what it finds, so rows saved by the old parser stop matching.
CACHE_VERSION = "1"
# Only these parts of a parse result are kept; they are everything a scan reads from it.
PARSE_KEYS = ("parsed_sections", "section_order", "section_roles", "structure")

_pending: set = set()   # background saves, kept referenced until they finish


class SectionCache:
    @staticmethod
    def enabled() -> bool:
        return os.getenv("SECTION_CACHE", "on").strip().lower() not in ("off", "false", "0", "no")

    @staticmethod
    def content_hash(content, template_toc: Optional[list] = None) -> str:
        """Identifies one manuscript as parsed: its bytes (or fetched text), the template used, and the parser version."""
        digest = hashlib.sha256()
        digest.update(CACHE_VERSION.encode())
        digest.update(json.dumps(template_toc, sort_keys=True).encode())
        digest.update(content if isinstance(content, (bytes, bytearray, memoryview)) else str(content).encode("utf-8"))
        return digest.hexdigest()

    @staticmethod
    def _row_to_cached(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        parsed = row.get("parsed")
        if not isinstance(parsed, dict) or not isinstance(parsed.get("parsed_sections"), dict):
            return None
        return {"manuscript_id": row.get("manuscript_id"), "raw_text": row.get("raw_text") or "",
                "parse_result": {**parsed, "structure": parsed.get("structure") or row.get("structure") or "headings",
                                 "section_order": parsed.get("section_order") or list(parsed["parsed_sections"]),
                                 "section_roles": parsed.get("section_roles") or {}}}

    @classmethod
    async def find(cls, user_id: str, content_hash: str) -> Optional[Dict[str, Any]]:
        """The newest saved parse of this exact manuscript for this user, or None."""
        if not cls.enabled() or not user_id or not content_hash:
            return None
        try:
            client = DatabasePersistenceService.get_client()
            response = await asyncio.to_thread(lambda: client.table(TABLE)
                                               .select("manuscript_id,structure,parsed,raw_text")
                                               .eq("user_id", user_id).eq("content_hash", content_hash)
                                               .order("created_at", desc=True).limit(1).execute())
            rows = response.data if isinstance(response.data, list) else []
            return cls._row_to_cached(rows[0]) if rows else None
        except Exception as error:   # a missing table or a slow database never stops a scan
            logger.warning("Section cache lookup skipped: %s", error)
            return None

    @classmethod
    async def load_for_manuscript(cls, manuscript_id: str, user_id: str) -> Optional[Dict[str, Any]]:
        """The saved parse of one manuscript, only if it belongs to this user."""
        if not cls.enabled():
            return None
        try:
            client = DatabasePersistenceService.get_client()
            response = await asyncio.to_thread(lambda: client.table(TABLE)
                                               .select("manuscript_id,structure,parsed,raw_text")
                                               .eq("manuscript_id", manuscript_id).eq("user_id", user_id).limit(1).execute())
            rows = response.data if isinstance(response.data, list) else []
            return cls._row_to_cached(rows[0]) if rows else None
        except Exception as error:
            logger.warning("Section cache load skipped: %s", error)
            return None

    @classmethod
    async def save(cls, manuscript_id: str, user_id: str, content_hash: str, parse_result: Dict[str, Any], raw_text: str) -> bool:
        if not cls.enabled() or not (manuscript_id and user_id and content_hash):
            return False
        try:
            client = DatabasePersistenceService.get_client()
            row = {"manuscript_id": manuscript_id, "user_id": user_id, "content_hash": content_hash,
                   "structure": parse_result.get("structure"), "raw_text": raw_text,
                   "parsed": {key: parse_result.get(key) for key in PARSE_KEYS}}
            await asyncio.to_thread(lambda: client.table(TABLE).upsert(row, on_conflict="manuscript_id").execute())
            return True
        except Exception as error:
            logger.warning("Section cache save skipped: %s", error)
            return False

    @classmethod
    def save_in_background(cls, *args, **kwargs) -> None:
        """Saves without making the scan wait for the database."""
        try:
            task = asyncio.get_running_loop().create_task(cls.save(*args, **kwargs))
        except RuntimeError:   # no running loop: nothing to do
            return
        _pending.add(task)
        task.add_done_callback(_pending.discard)
