"""
Reference list segmentation, link extraction, and in-text citation
cross-matching.

This module replaces the old approach in services/citation.py of finding
every URL in the whole references blob and slicing a fixed
[index-200 : index+len(url)+50] character window around it as the
"reference text". That approach never located entry boundaries, so
reference text routinely began mid-word (a URL's own scheme got cut off
by the previous entry's window), carried stray leading page numbers from
the source PDF/Docx export, and bled the next entry's author list onto
the end of the current one.

Everything here is pure/synchronous and does not touch the network, so it
can be unit-tested directly against a pasted references block.
"""

import re
import difflib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Pre-clean: strip PDF/Docx export noise before any segmentation runs.
# ---------------------------------------------------------------------------

# A stray running-header/page-number line on its own, e.g. "92" or "- 14 -".
STANDALONE_PAGE_NUMBER_RE = re.compile(r'^\s*[-–—]?\s*\d{1,4}\s*[-–—]?\s*$')

# A leading page/line number glued onto the start of an entry by a lossy
# export, e.g. "92 Abdelmoamen Ahmed, A. et al. (2020)." -> the "92 " is
# noise. Deliberately requires NO period/bracket after the digits (a real
# numbered-entry marker like "12." or "[12]" always has one) and requires
# a capitalized word immediately after, so this never eats a real
# numbered list marker.
LEADING_PAGE_NOISE_RE = re.compile(r"^\s*\d{1,4}\s+(?=[A-ZÀ-Ý])")


def _preclean(text: str) -> str:
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    text = re.sub(r'\f', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    lines = []
    for line in text.split('\n'):
        if STANDALONE_PAGE_NUMBER_RE.match(line):
            continue
        lines.append(LEADING_PAGE_NOISE_RE.sub('', line))
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# APA entry-start detection
# ---------------------------------------------------------------------------

# À-ÿ (Latin-1 Supplement) covers a surname's first letter fine, but
# excludes Latin Extended-A (U+0100-U+017F), where common European
# diacritics like ł/ń/ś/ź/ż live mid-surname (e.g. "Dołhańczuk-Śródka").
# A surname missing from this continuation class doesn't just fail to
# match itself -- it breaks the whole author-list match for every
# co-author around it, corrupting entry segmentation.
_SURNAME_REST = r"[A-Za-zÀ-ÿĀ-ſ'’\-]"

# "Surname, I." / "Surname, I. I." -- 1-3 initials, hyphenated/apostrophe surnames.
_APA_AUTHOR_UNIT = rf"[A-ZÀ-Ý]{_SURNAME_REST}+,\s*(?:[A-Z]\.[\s\-]*){{1,3}}"

# Full author list: first author, then more "Surname, I." separated by
# commas, optionally ending in "& Surname, I.", or "et al." after the
# first author.
_APA_AUTHOR_LIST = (
    rf"(?:{_APA_AUTHOR_UNIT})"
    rf"(?:(?:,\s*{_APA_AUTHOR_UNIT})*(?:,?\s*&\s*{_APA_AUTHOR_UNIT})?"
    rf"|,?\s*et\s*al\.\s*)?"
)

# Organizational/corporate author: capitalized multi-word phrase ending in
# a period, not matching the "Surname, Initial" comma pattern. A '.' is
# only permitted as an initialism dot (immediately after a single
# uppercase letter) -- an unrestricted '.' here let this pattern span a
# full sentence boundary and swallow the previous entry's title plus the
# next entry's author list (e.g. "...recognition accuracy. Burgess, G.").
_APA_CORPORATE_AUTHOR = r"[A-Z](?:[A-Za-z&,'’\- ]|(?<=\b[A-Z])\.){1,80}?\."

# Date forms: "(2023)." / "(2020a)." / "(2025, March 17)." / "(n.d.)."
_APA_YEAR_PAREN = r"\(\s*(?:\d{4}[a-z]?(?:,\s*[A-Za-z]+\.?\s*\d{1,2})?|n\.d\.)\s*\)\s*\."

APA_ENTRY_START_RE = re.compile(
    rf"^\s*(?:{_APA_AUTHOR_LIST}|{_APA_CORPORATE_AUTHOR})\s*{_APA_YEAR_PAREN}",
    re.MULTILINE,
)

# Title-first entries (no author, e.g. anonymous reports/legislation):
# "Title Of The Work. (2021). Publisher." Deliberately lower-confidence
# than APA_ENTRY_START_RE -- only used as a fallback anchor.
APA_TITLE_FIRST_RE = re.compile(
    r"^\s*[A-Z][A-Za-z0-9:,'’\"\- ]{9,150}?\.\s*\(\s*(?:\d{4}[a-z]?|n\.d\.)\s*\)\s*\.",
    re.MULTILINE,
)

NUMBERED_MARKER_RE = re.compile(r'^\s*(?:\[\d{1,3}\]|\(\d{1,3}\)|\d{1,3}[.)])\s+')


# ---------------------------------------------------------------------------
# Segmentation
# ---------------------------------------------------------------------------

def _split_on_blank_lines(raw_lines: List[str]) -> List[str]:
    blocks: List[str] = []
    buf: List[str] = []
    for line in raw_lines:
        if line.strip():
            buf.append(line.strip())
        elif buf:
            blocks.append(' '.join(buf))
            buf = []
    if buf:
        blocks.append(' '.join(buf))
    return blocks


def _segment_by_markers(lines: List[str]) -> List[str]:
    entries: List[str] = []
    buf: List[str] = []
    for line in lines:
        if NUMBERED_MARKER_RE.match(line):
            if buf:
                entries.append(' '.join(buf))
            buf = [NUMBERED_MARKER_RE.sub('', line, count=1)]
        else:
            buf.append(line)
    if buf:
        entries.append(' '.join(buf))
    return entries


def _segment_by_apa_wrap(lines: List[str]) -> List[str]:
    """Walks physical lines, starting a new entry buffer whenever a line
    matches an entry-start anchor at position 0. This is the path that
    handles a continuous run of hard-wrapped lines with no numbering and
    no reliable blank-line separators -- the common shape for a Google
    Docs plaintext export of an APA reference list."""
    entries: List[str] = []
    buf: List[str] = []
    for line in lines:
        is_start = bool(APA_ENTRY_START_RE.match(line) or APA_TITLE_FIRST_RE.match(line))
        if is_start and buf:
            entries.append(' '.join(buf))
            buf = [line]
        else:
            buf.append(line)
    if buf:
        entries.append(' '.join(buf))
    return entries


# Entry-start anchor scan that is NOT anchored to a line start -- the
# rescue path for a references blob that reached this module as (or
# collapsed into) a single physical line, so no line-based strategy above
# ever got a foothold. Scans the whole text for anchors and keeps only
# non-overlapping ones (skipping any anchor that starts before the
# previous match's end), which both stops the author-list regex from
# firing on every surname *inside* one entry, and -- as a side effect --
# drops any front-matter noise before the first real entry.
INLINE_ENTRY_RE = re.compile(
    rf"(?:{_APA_AUTHOR_LIST}|{_APA_CORPORATE_AUTHOR})\s*{_APA_YEAR_PAREN}"
)

# An appendix / curriculum-vitae section glued onto the end of the
# references blob with no surviving boundary always lands inside the
# final segmented entry -- this trims it there.
APPENDIX_TAIL_RE = re.compile(r'\b(?:APPENDICES|APPENDIX\s+[A-Z]\b|CURRICULUM\s+VITAE)\b', re.I)


def _segment_inline(text: str) -> List[str]:
    starts: List[int] = []
    last_end = -1
    for m in INLINE_ENTRY_RE.finditer(text):
        if m.start() >= last_end:
            starts.append(m.start())
            last_end = m.end()
    if not starts:
        return []
    bounds = starts + [len(text)]
    return [text[bounds[i]:bounds[i + 1]].strip() for i in range(len(starts))]


# Reuses the same date-form coverage as _APA_YEAR_PAREN (bare "(2023).",
# "(2020a).", the extended "(2025, March 17)." form, and "(n.d.)."), so a
# well-formed entry with an extended date and no URL is never mistaken
# for a noise fragment and merged into the previous entry.
_HAS_YEAR_RE = re.compile(r'\(\s*(?:\d{4}[a-z]?(?:,\s*[A-Za-z]+\.?\s*\d{1,2})?|n\.d\.)\s*\)')


def _looks_like_reference(entry: str) -> bool:
    """Reject noise fragments: too short, or no year and no link at all."""
    if len(entry) < 25:
        return False
    has_year = bool(_HAS_YEAR_RE.search(entry))
    has_link = bool(re.search(r'https?://|10\.\d{4,9}/', entry))
    return has_year or has_link


def segment_references(references_text: str) -> Tuple[List[str], str]:
    """Segments a raw references blob into individual entry strings.

    Returns (entries, strategy_used) -- the strategy name is kept for
    debugging/logging, not surfaced to the frontend.
    """
    if not references_text or not references_text.strip():
        return [], "empty"

    cleaned = _preclean(references_text)
    raw_lines = cleaned.split('\n')
    non_blank = [l for l in raw_lines if l.strip()]
    if not non_blank:
        return [], "empty"

    numbered_hits = sum(1 for l in non_blank if NUMBERED_MARKER_RE.match(l))
    numbered_ratio = numbered_hits / len(non_blank)

    blank_blocks = _split_on_blank_lines(raw_lines)
    apa_validated = sum(
        1 for b in blank_blocks
        if APA_ENTRY_START_RE.match(b) or APA_TITLE_FIRST_RE.match(b)
    )
    blank_block_ratio = (apa_validated / len(blank_blocks)) if len(blank_blocks) >= 3 else 0.0

    if numbered_ratio >= 0.5 and numbered_hits >= 3:
        entries, strategy = _segment_by_markers(non_blank), "numbered"
    elif blank_block_ratio >= 0.6:
        entries, strategy = blank_blocks, "blank_line"
    else:
        entries, strategy = _segment_by_apa_wrap(non_blank), "apa_wrap"

    # Validate and fall back: the chosen strategy's per-line anchoring can
    # fail to get a foothold at all -- typically because a newline-losing
    # upstream step delivered the whole references list as a single
    # physical line, collapsing it into one giant entry (or a couple of
    # giant merged entries, for a shorter list). Re-scan the joined text
    # for entry-start anchors wherever they occur, not just at line
    # starts, and take it whenever it finds strictly more real entries --
    # a length/count threshold here would (and did) miss short lists.
    rescued = _segment_inline(' '.join(non_blank))
    if len(rescued) > len(entries):
        entries, strategy = rescued, "inline_fallback"

    cleaned_entries = [re.sub(r'\s+', ' ', e).strip() for e in entries]
    filtered = [e for e in cleaned_entries if _looks_like_reference(e)]

    # Merge orphan fragments (rejected by _looks_like_reference) back into
    # the previous surviving entry rather than silently dropping them --
    # this prevents losing a URL/DOI that landed in its own tiny fragment.
    merged: List[str] = []
    for e in cleaned_entries:
        if _looks_like_reference(e):
            merged.append(e)
        elif merged:
            merged[-1] = f"{merged[-1]} {e}"
    result = merged if merged else filtered

    # Trim a glued-on appendix/CV tail off the final entry only -- real
    # appendices always follow the reference list, so this can't damage
    # an earlier, legitimate entry.
    if result:
        tail_m = APPENDIX_TAIL_RE.search(result[-1])
        if tail_m:
            result[-1] = result[-1][:tail_m.start()].strip()
            if not _looks_like_reference(result[-1]):
                result.pop()

    return result, strategy


# ---------------------------------------------------------------------------
# Per-entry link extraction
# ---------------------------------------------------------------------------

URL_RE = re.compile(r'https?://[^\s<>"\')\]]+')
DOI_RE = re.compile(r'\b10\.\d{4,9}/[^\s]+', re.IGNORECASE)


def _clean_terminator(s: str) -> str:
    """Strip trailing sentence punctuation, then only strip a trailing
    closing bracket/paren if it is unbalanced against opens inside the
    candidate -- DOIs legally contain '.', '(', ')', so a naive
    unconditional rstrip corrupts them."""
    s = s.strip()
    while s and s[-1] in '.,;:\'"':
        s = s[:-1]
    for open_c, close_c in (('(', ')'), ('[', ']')):
        while s.endswith(close_c) and s.count(close_c) > s.count(open_c):
            s = s[:-1]
            while s and s[-1] in '.,;:\'"':
                s = s[:-1]
    return s


def normalize_doi(bare_doi: str) -> str:
    return f"https://doi.org/{_clean_terminator(bare_doi)}"


def extract_entry_links(entry_text: str) -> List[Dict[str, Any]]:
    """Returns [] when the entry has no link at all (legitimate for a
    print-only book or a source cited without a URL/DOI)."""
    links: List[Dict[str, Any]] = []
    seen_dois: set = set()
    seen_urls: set = set()

    for m in URL_RE.finditer(entry_text):
        raw_url = _clean_terminator(m.group())
        doi_in_url = re.search(r'doi\.org/(10\.\d{4,9}/\S+)', raw_url, re.IGNORECASE)
        if doi_in_url:
            doi = _clean_terminator(doi_in_url.group(1))
            if doi.lower() not in seen_dois:
                seen_dois.add(doi.lower())
                links.append({"kind": "doi", "doi": doi, "url": f"https://doi.org/{doi}"})
        elif raw_url and raw_url not in seen_urls:
            seen_urls.add(raw_url)
            links.append({"kind": "url", "doi": None, "url": raw_url})

    for m in DOI_RE.finditer(entry_text):
        doi = _clean_terminator(m.group())
        if doi.lower() not in seen_dois:
            seen_dois.add(doi.lower())
            links.append({"kind": "doi", "doi": doi, "url": f"https://doi.org/{doi}"})

    return links


# ---------------------------------------------------------------------------
# Entry metadata parsing (author surname / year, for cross-matching)
# ---------------------------------------------------------------------------

# Up to 2 leading capitalized words before the comma, so a compound
# surname ("Abdelmoamen Ahmed, A.") keys as a whole rather than matching
# nothing.
_FIRST_AUTHOR_RE = re.compile(
    rf"^\s*((?:[A-ZÀ-Ý]{_SURNAME_REST}+\s+){{0,2}}[A-ZÀ-Ý]{_SURNAME_REST}+),"
)
_ENTRY_YEAR_RE = re.compile(r'\(\s*(\d{4})[a-z]?\s*(?:,[^)]*)?\)')


def parse_entry_metadata(entry_text: str) -> Dict[str, Optional[str]]:
    author_m = _FIRST_AUTHOR_RE.match(entry_text)
    year_m = _ENTRY_YEAR_RE.search(entry_text)
    authors_block = None
    if year_m:
        authors_block = entry_text[: year_m.start()].strip().rstrip('.')

    surname = author_m.group(1) if author_m else None
    if not surname and authors_block and authors_block[0].isupper():
        # Corporate/organizational author with no "Surname, I." comma form
        # (e.g. "AgriChain.", "GSMA.", "University of Florida, IFAS
        # Extension (UF/IFAS).") -- use the phrase before the year paren
        # as the key.
        surname = authors_block

    return {
        "first_author_surname": surname,
        "authors_parsed": authors_block,
        "year_parsed": int(year_m.group(1)) if year_m else None,
    }


# ---------------------------------------------------------------------------
# In-text citation extraction and cross-matching
# ---------------------------------------------------------------------------

_INTEXT_PAREN_RE = re.compile(r'\(([^()]{0,200}?\d{4}[a-z]?[^()]{0,50})\)')
_CITATION_UNIT_RE = re.compile(
    rf"([A-ZÀ-Ý]{_SURNAME_REST}+)"
    rf"(?:\s*,?\s*(?:&|and)\s*[A-ZÀ-Ý]{_SURNAME_REST}+)?"
    r"(?:\s*,?\s*et\s*al\.)?"
    r",?\s*(\d{4}[a-z]?|n\.d\.)"
)
_NARRATIVE_RE = re.compile(
    rf"\b([A-ZÀ-Ý]{_SURNAME_REST}+)"
    rf"(?:\s+(?:&|and)\s+[A-ZÀ-Ý]{_SURNAME_REST}+)?"
    r"(?:\s+et\s+al\.)?"
    r"\s*\((\d{4}[a-z]?|n\.d\.)\)"
)


def _sentence_around(pos: int, text: str, radius: int = 120) -> str:
    start = max(0, pos - radius)
    end = min(len(text), pos + radius)
    return re.sub(r'\s+', ' ', text[start:end]).strip()


def extract_intext_citations(body_text: str) -> List[Dict[str, str]]:
    found: List[Dict[str, str]] = []
    for m in _INTEXT_PAREN_RE.finditer(body_text):
        for clause in m.group(1).split(';'):
            u = _CITATION_UNIT_RE.search(clause.strip())
            if u:
                found.append({
                    "surname": u.group(1),
                    "year": u.group(2),
                    "context": _sentence_around(m.start(), body_text),
                })
    for m in _NARRATIVE_RE.finditer(body_text):
        found.append({
            "surname": m.group(1),
            "year": m.group(2),
            "context": _sentence_around(m.start(), body_text),
        })
    return found


def surname_key_variants(surname: str) -> List[str]:
    """Lowercase cross-match key variants for a parsed first-author
    surname: the full surname, plus its last token for a compound surname
    -- a body citing "Abdelmoamen Ahmed, A. et al. (2020)" is commonly
    abbreviated in-text to "Ahmed et al.", and the narrative in-text
    citation regex only captures the last capitalized word before the
    year anyway. Shared by cross_match_citations() and citation.py's
    per-entry is_cited check so both stay consistent."""
    surname = surname.strip()
    variants = [surname.lower()]
    if " " in surname:
        variants.append(surname.split()[-1].lower())
    return variants


def cross_match_citations(
    intext: List[Dict[str, str]],
    reference_entries: List[Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    """reference_entries: list of dicts each containing at least
    'first_author_surname', 'year_parsed', and 'citation_raw_reference_text'."""
    ref_index: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for ref in reference_entries:
        surname = ref.get("first_author_surname")
        year = ref.get("year_parsed")
        if surname and year:
            for variant in surname_key_variants(surname):
                ref_index[(variant, str(year))] = ref

    matched_ids: set = set()
    orphans: List[Dict[str, Any]] = []
    seen_orphan_keys: set = set()
    for c in intext:
        if c["year"] == "n.d.":
            continue
        key = (c["surname"].lower(), c["year"])
        ref = ref_index.get(key)
        if ref is not None:
            matched_ids.add(id(ref))
        elif key not in seen_orphan_keys:
            seen_orphan_keys.add(key)
            orphans.append(c)

    uncited: List[Dict[str, Any]] = []
    seen_uncited_ids: set = set()
    for ref in ref_index.values():
        if id(ref) not in matched_ids and id(ref) not in seen_uncited_ids:
            seen_uncited_ids.add(id(ref))
            uncited.append(ref)
    return {"orphan_intext_citations": orphans, "uncited_references": uncited}


# ---------------------------------------------------------------------------
# Fuzzy metadata comparison (used by citation.py's Crossref cross-check)
# ---------------------------------------------------------------------------

def fuzzy_title_match(crossref_title: str, entry_text: str, threshold: float = 0.55) -> Tuple[bool, float]:
    """Zero-dependency fuzzy match (stdlib difflib) -- deliberately avoids
    pulling in a new fuzzy-matching library on a memory-constrained
    Render free-tier deploy."""
    norm_title = re.sub(r'[^a-z0-9 ]', '', crossref_title.lower())
    norm_entry = re.sub(r'[^a-z0-9 ]', '', entry_text.lower())
    if not norm_title:
        return True, 1.0
    ratio = difflib.SequenceMatcher(None, norm_title, norm_entry).ratio()
    title_tokens = set(norm_title.split())
    entry_tokens = set(norm_entry.split())
    overlap = len(title_tokens & entry_tokens) / max(1, len(title_tokens))
    score = max(ratio, overlap)
    return score >= threshold, round(score, 3)


@dataclass
class ParsedReferenceEntry:
    """Convenience container bundling segmentation + link extraction +
    metadata for one reference entry, before network verification runs."""
    index: int
    raw_text: str
    links: List[Dict[str, Any]] = field(default_factory=list)
    first_author_surname: Optional[str] = None
    authors_parsed: Optional[str] = None
    year_parsed: Optional[int] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "citation_entry_index": self.index,
            "citation_raw_reference_text": self.raw_text,
            "citation_links": self.links,
            "citation_authors_parsed": self.authors_parsed,
            "citation_year_parsed": self.year_parsed,
            "first_author_surname": self.first_author_surname,
        }


def build_parsed_entries(references_text: str) -> Tuple[List[ParsedReferenceEntry], str]:
    entries, strategy = segment_references(references_text)
    parsed: List[ParsedReferenceEntry] = []
    for i, entry_text in enumerate(entries):
        links = extract_entry_links(entry_text)
        meta = parse_entry_metadata(entry_text)
        parsed.append(ParsedReferenceEntry(
            index=i,
            raw_text=entry_text,
            links=links,
            first_author_surname=meta["first_author_surname"],
            authors_parsed=meta["authors_parsed"],
            year_parsed=meta["year_parsed"],
        ))
    return parsed, strategy
