"""The AI's verdict on a pair of sections, remembered, so the same text always gets the same answer.

Gemini can word things differently, or judge a borderline pair differently, from one run to the next. Scores must not
depend on that. A pair's verdict is saved under a fingerprint of exactly what the AI was shown (the two text windows, the
section names, the model's ratings and the prompt version). Scanning an unchanged paper again, or a paper where only other
sections changed, reuses the saved verdict instead of asking again, so the score cannot drift and no AI call is spent.

Best effort, like the section cache: a missing table or a slow database only means the AI is asked again.
"""

import asyncio
import copy
import hashlib
import json
import logging
import os
from collections import OrderedDict
from typing import Any, Dict, Iterable, List

from services.db_service import DatabasePersistenceService

logger = logging.getLogger("resync.verdict_cache")

TABLE = "pair_verdict_cache"
# Bump this whenever the pair prompts, the rules or how answers are read change: verdicts saved before no longer match.
PROMPT_VERSION = "2"
MEMORY_LIMIT = 4000
CHUNK = 100   # keys per database lookup

_memory: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()   # verdicts seen by this server, used when the database is not reachable


class VerdictCache:
    @staticmethod
    def enabled() -> bool:
        return os.getenv("VERDICT_CACHE", "on").strip().lower() not in ("off", "false", "0", "no")

    @staticmethod
    def fingerprint(*parts: Any) -> str:
        """The same input always gives the same key; any change to what the AI was shown gives a different one."""
        return hashlib.sha256(json.dumps([PROMPT_VERSION, *parts], sort_keys=True, default=str).encode("utf-8")).hexdigest()

    @classmethod
    async def get_many(cls, keys: Iterable[str]) -> Dict[str, Dict[str, Any]]:
        """{key: saved explanation} for every key that has one."""
        wanted = list(dict.fromkeys(keys))
        if not cls.enabled() or not wanted:
            return {}
        found = {key: copy.deepcopy(_memory[key]) for key in wanted if key in _memory}
        missing = [key for key in wanted if key not in found]
        if missing:
            try:
                client = DatabasePersistenceService.get_client()
                for start in range(0, len(missing), CHUNK):
                    chunk = missing[start:start + CHUNK]
                    response = await asyncio.to_thread(lambda: client.table(TABLE).select("cache_key,explanation")
                                                       .in_("cache_key", chunk).execute())
                    for row in response.data if isinstance(response.data, list) else []:
                        if isinstance(row.get("explanation"), dict) and "severity" in row["explanation"]:
                            found[row["cache_key"]] = row["explanation"]
                            cls._remember(row["cache_key"], row["explanation"])
            except Exception as error:   # a missing table or a slow database only means the AI is asked again
                logger.warning("Verdict cache lookup skipped: %s", error)
        return found

    @classmethod
    async def save_many(cls, verdicts: Dict[str, Dict[str, Any]]) -> None:
        """Only complete verdicts are kept: a pair the AI could not judge (severity None) is never remembered."""
        keep = {key: value for key, value in verdicts.items() if isinstance(value, dict) and value.get("severity") is not None}
        if not cls.enabled() or not keep:
            return
        for key, value in keep.items():
            cls._remember(key, value)
        try:
            client = DatabasePersistenceService.get_client()
            rows = [{"cache_key": key, "prompt_version": PROMPT_VERSION, "explanation": value} for key, value in keep.items()]
            await asyncio.to_thread(lambda: client.table(TABLE).upsert(rows, on_conflict="cache_key").execute())
        except Exception as error:
            logger.warning("Verdict cache save skipped: %s", error)

    @staticmethod
    def _remember(key: str, value: Dict[str, Any]) -> None:
        _memory[key] = copy.deepcopy(value)
        _memory.move_to_end(key)
        while len(_memory) > MEMORY_LIMIT:
            _memory.popitem(last=False)

    @staticmethod
    def forget_everything_in_memory() -> None:
        _memory.clear()
