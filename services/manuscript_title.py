"""Derives a manuscript title from the first meaningful lines of a document."""

import re

from services.v7_explain import plain_text

TITLE_LIMIT = 160
_WHITESPACE = re.compile(r"\s+")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_PAGE_OR_ROMAN = re.compile(r"(?i)\d+|page\s*\d+|x{0,2}(?:ix|iv|v?i{0,3})")
_FRONT_MATTER = re.compile(
    r"(?i)table of contents|contents|list of (?:figures|tables)|abstract|acknowledg\w*|introduction"
    r"|(?:a|an)\s+(?:capstone|thesis|dissertation|research)\s+(?:research\s+)?(?:proposal|paper|report|project|thesis)"
    r"|presented to\b.*|submitted (?:to|by)\b.*|in partial fulfil\w*.*|bachelor of\b.*|college of\b.*|university of\b.*")
# A title that wraps mid-phrase ends on one of these, so the next line may continue it.
CONNECTORS = {"a", "an", "and", "as", "at", "by", "for", "from", "in", "of", "on", "or", "the", "to", "via", "vs", "with"}


def _clean(line):
    return _WHITESPACE.sub(" ", _LINK.sub(r"\1", plain_text(line))).strip()


def _meaningful(text):
    letters = sum(ch.isalpha() for ch in text)
    return letters >= 4 and not _PAGE_OR_ROMAN.fullmatch(text) and not _FRONT_MATTER.fullmatch(text)


def _mostly_upper(text):
    letters = [ch for ch in text if ch.isalpha()]
    return bool(letters) and sum(ch.isupper() for ch in letters) >= 0.7 * len(letters)


def _title_case(text):
    words = [word for word in text.split() if word[0].isalpha()]
    return bool(words) and all(word[0].isupper() or word.lower() in CONNECTORS for word in words)


def _continues(title, following):
    if _mostly_upper(title) and _mostly_upper(following):
        return True
    words = title.split()
    ends_mid_phrase = title[-1].isalnum() and words[-1].lower() in CONNECTORS
    return ends_mid_phrase and len(following.split()) <= 12 and _title_case(following)


def derive_title(text):
    """First meaningful line of the text, joined with the next line when the title wraps. None if nothing usable."""
    lines = (cleaned for cleaned in (_clean(line) for line in (text or "").splitlines()) if _meaningful(cleaned))
    first = next(lines, None)
    if first is None:
        return None
    second = next(lines, None)
    title = f"{first} {second}" if second and _continues(first, second) else first
    return title[:TITLE_LIMIT].rstrip()
