"""Citation integrity is advisory and excluded from the v7 manuscript score."""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

@dataclass
class CitationIntegrityResult:
    score: Optional[float]
    well_formed_ratio: float = 0.0
    link_resolution_rate: float = 0.0
    cross_match_score: float = 0.0
    total_entries: int = 0


def compute_citation_integrity(
    citations: List[Dict[str, Any]],
    crossmatch: Dict[str, List[Dict[str, Any]]],
    total_intext_unique: int,
    references_section_present: bool,
) -> CitationIntegrityResult:
    if not references_section_present:
        # A missing References section has no citation score.
        return CitationIntegrityResult(score=None, total_entries=0)

    if not citations:
        # A References heading exists but nothing parsed out of it is a
        # real red flag (either the section is empty, or the parser
        # couldn't segment it) -- score it, don't exempt it.
        return CitationIntegrityResult(score=0.0, total_entries=0)

    well_formed = sum(
        1 for c in citations
        if c.get("citation_authors_parsed") and c.get("citation_year_parsed")
    )
    well_formed_ratio = well_formed / len(citations)

    linked = [c for c in citations if c.get("citation_status") != "no_link"]
    if linked:
        resolved = sum(
            1 for c in linked
            if c.get("citation_status") in ("verified_metadata", "accessible")
        )
        bot_walled = sum(1 for c in linked if c.get("citation_status") == "bot_wall")
        # bot_wall counts half -- a publisher's bot-defense blocking an
        # automated check is not the student's fault and shouldn't read
        # the same as a genuinely dead link.
        link_resolution_rate = (resolved + 0.5 * bot_walled) / len(linked)
    else:
        # No entries used a link at all -- don't punish a print-only
        # bibliography for having nothing to verify.
        link_resolution_rate = 1.0

    uncited = len(crossmatch.get("uncited_references", []))
    orphans = len(crossmatch.get("orphan_intext_citations", []))
    ref_used_rate = (len(citations) - uncited) / len(citations)
    intext_match_rate = (
        (total_intext_unique - orphans) / total_intext_unique
        if total_intext_unique else 1.0
    )
    cross_match_score = 0.6 * intext_match_rate + 0.4 * ref_used_rate

    score = 100.0 * (
        0.30 * well_formed_ratio
        + 0.40 * link_resolution_rate
        + 0.30 * cross_match_score
    )

    return CitationIntegrityResult(
        score=round(score, 2),
        well_formed_ratio=round(well_formed_ratio, 3),
        link_resolution_rate=round(link_resolution_rate, 3),
        cross_match_score=round(cross_match_score, 3),
        total_entries=len(citations),
    )
