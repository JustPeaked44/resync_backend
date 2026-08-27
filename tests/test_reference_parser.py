"""
Regression test for the citation segmentation bug shown in the Aug 2026
capstone review screenshot: reference entries beginning mid-word
("tps://doi.org/..."), carrying stray leading page numbers
("92 Abdelmoamen Ahmed, A. et al."), and bleeding the next entry's
author list onto the end of the current one.

Run with: python -m pytest tests/test_reference_parser.py -v
(or, with no pytest installed, `python tests/test_reference_parser.py`).
"""

import re
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.reference_parser import (
    segment_references,
    extract_entry_links,
    parse_entry_metadata,
    extract_intext_citations,
    cross_match_citations,
)

# Same content as SCREENSHOT_FIXTURE (below), but joined with a single
# space -- the shape the production pipeline actually delivers once
# parser.py has walked and re-joined the References section body line by
# line. This is the fixture that reproduces the "Citations checked: 1"
# bug; SCREENSHOT_FIXTURE's newline-joined shape never occurs in prod.
SINGLE_LINE_FIXTURE = (
    "92 Abdelmoamen Ahmed, A. et al. (2020). A distributed system for supporting smart "
    "irrigation using Internet of Things technology. Engineering Reports, 3(7). "
    "https://doi.org/10.1002/eng2.12352 "
    "AgriChain. (2025, March 17). What is precision agriculture? "
    "Kwapong, N., & Ankrah, D. A. (2023). Understanding innovation process within an "
    "interactive social network: Empirical insights from smallholder farmers. "
    "Dabire, G. (2024). PlanteSaine: An artificial intelligent empowered mobile application "
    "for pests and disease management for maize, tomato, and onion farmers in Burkina Faso. "
    "Agriculture, 14(8), 1252. https://doi.org/10.3390/agriculture14081252 "
    "Ashworth, A., & Owens, P. (2025, January 10). Benefits and evolution of precision "
    "agricultural. "
    "Brinkle, C. (2024, February 13). Precision agriculture explained. "
    "Khapovalov, V., Megalinska, A., Zhadan, S., Andruszkiewicz, F., Dolhanczuk-Srodka, A., "
    "& Antonenko, P. (2020). Comparing Google Lens recognition accuracy with other plant "
    "recognition apps (pp. 20-33). https://doi.org/10.5220/0010928000003364"
)

TOC_NOISE_AND_APPENDIX_FIXTURE = (
    "APPENDICES 97 APPENDIX A - Transmittal Letter - Users and Responders 97 APPENDIX B - "
    "Research Instrument - Users 100 "
    "Abdelmoamen Ahmed, A. et al. (2020). A distributed system for supporting smart "
    "irrigation using Internet of Things technology. Engineering Reports, 3(7). "
    "https://doi.org/10.1002/eng2.12352 "
    "AgriChain. (2025, March 17). What is precision agriculture/farming? AgriChain. "
    "https://agrichain.com/precision-farming/ "
    "Bilyk, Z., Shapovalov, Y., & Antonenko, P. (2020). Comparing Google Lens recognition "
    "accuracy with other plant recognition apps. "
    "Burgess, G. L., & Worthington, A. K. (2021). Technology acceptance model. Pressbooks. "
    "APPENDICES Appendix A Transmittal Letter - Virginia Farms JOHN MICHAEL P. EBORDA "
    "Research Leader Cebu City June 8, 2025 MR. ALVIN OLIVEROS Senior Manager Virginia Farms "
    "Inc. Dear Mr. Oliveros: We, the 3rd year Bachelor of Science students are conducting a "
    "study entitled Papaia."
)

# Reconstructed from the screenshot: a Google Docs plaintext export
# preserves each reference paragraph as its own line. The leading "92 "
# on the first entry is the stray page-number noise seen in the bug
# report.
SCREENSHOT_FIXTURE = "\n".join([
    "92 Abdelmoamen Ahmed, A. et al. (2020). A distributed system for supporting smart "
    "irrigation using Internet of Things technology. Engineering Reports, 3(7). "
    "https://doi.org/10.1002/eng2.12352",
    "AgriChain. (2025, March 17). What is precision agriculture?",
    "Kwapong, N., & Ankrah, D. A. (2023). Understanding innovation process within an "
    "interactive social network: Empirical insights from smallholder farmers.",
    "Dabire, G. (2024). PlanteSaine: An artificial intelligent empowered mobile application "
    "for pests and disease management for maize, tomato, and onion farmers in Burkina Faso. "
    "Agriculture, 14(8), 1252. https://doi.org/10.3390/agriculture14081252",
    "Ashworth, A., & Owens, P. (2025, January 10). Benefits and evolution of precision "
    "agricultural.",
    "Brinkle, C. (2024, February 13). Precision agriculture explained.",
    "Khapovalov, V., Megalinska, A., Zhadan, S., Andruszkiewicz, F., Dolhanczuk-Srodka, A., "
    "& Antonenko, P. (2020). Comparing Google Lens recognition accuracy with other plant "
    "recognition apps (pp. 20-33). https://doi.org/10.5220/0010928000003364",
])


def test_no_leading_page_number_noise():
    entries, _ = segment_references(SCREENSHOT_FIXTURE)
    assert entries, "expected at least one segmented entry"
    assert entries[0].startswith("Abdelmoamen Ahmed"), (
        f"leading page-number noise was not stripped: {entries[0][:40]!r}"
    )
    assert not any(re.match(r"^\d", e) for e in entries), "an entry still starts with a bare digit"


def test_no_truncated_mid_word_url():
    entries, _ = segment_references(SCREENSHOT_FIXTURE)
    for e in entries:
        for m in re.finditer(r"tps://", e):
            assert e[max(0, m.start() - 2):m.start()] == "ht", (
                f"found a truncated URL fragment in: {e!r}"
            )


def test_entries_do_not_bleed_into_each_other():
    entries, _ = segment_references(SCREENSHOT_FIXTURE)
    # The Kwapong entry must not contain the AgriChain entry's text, and
    # vice versa -- each is its own independent entry.
    kwapong = next((e for e in entries if e.startswith("Kwapong")), None)
    assert kwapong is not None, "Kwapong entry was not segmented out on its own"
    assert "AgriChain" not in kwapong
    assert "Dabire" not in kwapong


def test_extended_date_form_does_not_get_merged_as_noise():
    entries, _ = segment_references(SCREENSHOT_FIXTURE)
    agrichain = next((e for e in entries if e.startswith("AgriChain")), None)
    assert agrichain is not None, (
        "an entry with the '(2025, March 17)' extended date form and no URL "
        "was incorrectly treated as a noise fragment and merged away"
    )


def test_doi_link_extraction_per_entry():
    entries, _ = segment_references(SCREENSHOT_FIXTURE)
    dabire = next((e for e in entries if e.startswith("Dabire")), None)
    assert dabire is not None
    links = extract_entry_links(dabire)
    assert any(l["url"] == "https://doi.org/10.3390/agriculture14081252" for l in links)


def test_bare_doi_is_normalized():
    links = extract_entry_links("Smith, J. (2021). A paper. Journal, 1(1). 10.1234/abc.123")
    assert links and links[0]["url"] == "https://doi.org/10.1234/abc.123"


def test_entry_metadata_parses_author_and_year():
    meta = parse_entry_metadata("Kwapong, N., & Ankrah, D. A. (2023). Understanding innovation process.")
    assert meta["first_author_surname"] == "Kwapong"
    assert meta["year_parsed"] == 2023


def test_intext_crossmatch_finds_orphan_and_uncited():
    body = "This aligns with prior work (Kwapong & Ankrah, 2023) and (Nobody, 2099)."
    intext = extract_intext_citations(body)
    ref_entries = [
        {"first_author_surname": "Kwapong", "year_parsed": 2023, "citation_raw_reference_text": "Kwapong..."},
        {"first_author_surname": "Dabire", "year_parsed": 2024, "citation_raw_reference_text": "Dabire..."},
    ]
    result = cross_match_citations(intext, ref_entries)
    orphan_surnames = {o["surname"] for o in result["orphan_intext_citations"]}
    assert "Nobody" in orphan_surnames
    uncited_surnames = {r["first_author_surname"] for r in result["uncited_references"]}
    assert "Dabire" in uncited_surnames
    assert "Kwapong" not in uncited_surnames


def test_single_line_blob_segments():
    # Regression: this is the exact bug from the Aug 2026 capstone review
    # screenshot ("Citations checked: 1"). A single-line blob must not
    # collapse into one giant entry.
    entries, strategy = segment_references(SINGLE_LINE_FIXTURE)
    assert len(entries) >= 6, f"expected >=6 entries, got {len(entries)} via {strategy!r}: {entries}"
    assert any(e.startswith("Abdelmoamen Ahmed") or e.startswith("Ahmed") for e in entries)
    kwapong = next((e for e in entries if e.startswith("Kwapong")), None)
    assert kwapong is not None
    assert "AgriChain" not in kwapong
    assert "Dabire" not in kwapong


def test_toc_noise_before_first_entry_is_dropped():
    entries, _ = segment_references(TOC_NOISE_AND_APPENDIX_FIXTURE)
    assert entries, "expected at least one segmented entry"
    assert "APPENDICES" not in entries[0]
    assert "APPENDIX" not in entries[0]
    assert entries[0].startswith("Abdelmoamen Ahmed") or entries[0].startswith("Ahmed")


def test_appendix_tail_is_trimmed():
    entries, _ = segment_references(TOC_NOISE_AND_APPENDIX_FIXTURE)
    joined = " ".join(entries)
    assert "Transmittal Letter" not in joined
    assert "Dear Mr. Oliveros" not in joined
    assert "JOHN MICHAEL" not in joined


def test_corporate_author_does_not_swallow_previous_entry():
    entries, _ = segment_references(TOC_NOISE_AND_APPENDIX_FIXTURE)
    bilyk = next((e for e in entries if e.startswith("Bilyk")), None)
    assert bilyk is not None, f"Bilyk entry missing: {entries}"
    assert "Comparing Google Lens" in bilyk
    assert "Burgess" not in bilyk
    burgess = next((e for e in entries if e.startswith("Burgess")), None)
    assert burgess is not None, f"Burgess entry missing: {entries}"
    assert "Bilyk" not in burgess


def test_compound_and_corporate_author_keys():
    cases = [
        ("Abdelmoamen Ahmed, A. et al. (2020). A distributed system.", "Abdelmoamen Ahmed"),
        ("AgriChain. (2025, March 17). What is precision agriculture?", "AgriChain"),
        ("GSMA. (2025). Detecting and managing crop pests.", "GSMA"),
    ]
    for text, expected in cases:
        meta = parse_entry_metadata(text)
        assert meta["first_author_surname"] == expected, (
            f"{text!r} -> {meta['first_author_surname']!r}, expected {expected!r}"
        )


def test_short_acronym_corporate_author_is_not_dropped():
    # Regression: a >=4-char lower bound on the corporate-author pattern
    # meant any org name with <=4 letters ("GSMA.", "WHO.", "UN.") could
    # never anchor as an entry start and silently vanished into whichever
    # adjacent entry did match.
    text = (
        "Carolan, M. (2020). Acting like an algorithm. https://doi.org/10.1007/s10460-020-10032-w "
        "GSMA. (2025). Detecting and managing crop pests. https://www.gsma.com/blog/detecting "
        "Plantix. (n.d.). Your crop doctor app. https://plantix.net/en/"
    )
    entries, _ = segment_references(text)
    gsma = next((e for e in entries if e.startswith("GSMA")), None)
    assert gsma is not None, f"GSMA entry was dropped: {entries}"
    assert "Carolan" not in gsma
    assert "Plantix" not in gsma


def test_intext_alias_matches_compound_surname():
    body = "This aligns with prior work (Ahmed et al., 2020) on smart irrigation."
    intext = extract_intext_citations(body)
    ref_entries = [
        {
            "first_author_surname": "Abdelmoamen Ahmed",
            "year_parsed": 2020,
            "citation_raw_reference_text": "Abdelmoamen Ahmed, A. et al. (2020)...",
        },
    ]
    result = cross_match_citations(intext, ref_entries)
    assert result["orphan_intext_citations"] == []
    assert result["uncited_references"] == []


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
