"""The original Word file of a scan, kept so a report can show the manuscript exactly as written on any device.

Off unless SOURCE_FILES_BUCKET names a PRIVATE Supabase Storage bucket that already exists (nothing here creates it). Files live at
<user id>/<scan id>.docx and are only ever handed back to the user who owns the scan. Everything is best effort: a storage problem is
logged and never stops a scan or a report from opening.
"""
import asyncio
import logging
import os

logger = logging.getLogger("resync.source_files")

DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def bucket():
    return (os.getenv("SOURCE_FILES_BUCKET") or "").strip()


def enabled():
    return bool(bucket())


def path_for(user_id, scan_id):
    return f"{user_id}/{scan_id}.docx"


def looks_like_docx(data):
    """A .docx is a zip archive; anything else is refused before it is stored."""
    return len(data) > 4 and bytes(data[:2]) == b"PK"


async def save(client, user_id, scan_id, data):
    """True when the file was stored."""
    if not enabled():
        return False
    try:
        await asyncio.to_thread(lambda: client.storage.from_(bucket()).upload(
            path_for(user_id, scan_id), bytes(data), {"content-type": DOCX_TYPE, "upsert": "true"}))
        return True
    except Exception as exc:   # storage is optional: say so in the log, never in the scan
        logger.warning("Could not store the source file of scan %s: %s", scan_id, type(exc).__name__)
        return False


async def load(client, user_id, scan_id):
    """The stored bytes, or None when there is none."""
    if not enabled():
        return None
    try:
        data = await asyncio.to_thread(lambda: client.storage.from_(bucket()).download(path_for(user_id, scan_id)))
        return bytes(data) if data else None
    except Exception as exc:
        logger.info("No stored source file for scan %s (%s)", scan_id, type(exc).__name__)
        return None
