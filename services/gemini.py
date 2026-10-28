"""Shared Gemini transport and source-quote verification for v7 explanations."""

import asyncio
import os
import re
import logging
import httpx

_WHITESPACE = re.compile(r"\s+")
_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})


def verify_quote(quote, text):
    if not quote:
        return "", True
    if not text:
        return "", False
    normalized = lambda value: _WHITESPACE.sub(" ", value.translate(_QUOTES)).strip().lower()
    return (quote, True) if normalized(quote) in normalized(text) else ("", False)


def truncate_text(text, max_chars=6000):
    cleaned = text.strip()
    return cleaned if len(cleaned) <= max_chars else cleaned[:max_chars] + "... [truncated]"


async def post_gemini_with_retry(payload):
    key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent"

    async def send():
        async with httpx.AsyncClient(timeout=30.0) as client:
            return await client.post(url, json=payload,
                                     headers={"Content-Type": "application/json", "x-goog-api-key": key})

    response = await send()
    if response.status_code == 429:
        logging.getLogger("resync.gemini").warning("Gemini returned 429")
        try:
            delay = float(response.headers.get("Retry-After", 5.0))
        except (TypeError, ValueError):
            delay = 5.0
        await asyncio.sleep(delay)
        response = await send()
        if response.status_code == 429:
            logging.getLogger("resync.gemini").warning("Gemini returned 429")
    return response
