import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

import main
from services.citation_display import add_citation_display_status, citation_display_status
from services.scan_access import locked_scan_view


CASES = [
    ("1", {"citation_status": "no_link", "citation_is_accessible": True, "citation_primary_link": "https://x.org/a", "citation_raw_reference_text": "Smith (2020)"}, "no_link"),
    ("2", {"citation_status": "No Link"}, "no_link"),
    ("3", {"citation_status": "verified_doi", "citation_is_accessible": True, "citation_primary_link": "https://doi.org/10.1234/x", "citation_raw_reference_text": "Smith (2020)"}, "accessible"),
    ("4", {"citation_status": "verified_url", "citation_primary_link": "https://x.org/a", "citation_raw_reference_text": "Smith (2020)"}, "accessible"),
    ("5", {"citation_status": "metadata_mismatch", "citation_primary_link": "https://x.org/a", "citation_raw_reference_text": "Smith (2020)"}, "accessible"),
    ("6", {"citation_status": "accessible", "citation_is_accessible": False, "citation_primary_link": "https://x.org/a", "citation_raw_reference_text": "Smith (2020)"}, "inaccessible"),
    ("7", {"citation_status": "bot_wall", "citation_primary_link": "https://x.org/a", "citation_raw_reference_text": "Smith (2020)"}, "inaccessible"),
    ("8", {"citation_status": "broken_link", "citation_primary_link": "https://x.org/a", "citation_raw_reference_text": "Smith (2020)"}, "inaccessible"),
    ("9", {"citation_status": "", "citation_primary_link": "", "citation_raw_reference_text": "Smith, J. (2020). A study of things. Journal of X."}, "no_link"),
    ("10", {"citation_status": "", "citation_primary_link": "", "citation_raw_reference_text": "Smith (2020) https://example.org/paper"}, "inaccessible"),
    ("11", {"citation_status": "", "citation_primary_link": "https://x.org/a", "citation_raw_reference_text": "Smith (2020)"}, "inaccessible"),
    ("12", {"citation_status": "", "citation_primary_link": "", "citation_raw_reference_text": "Smith (2020) doi: 10.1234/abcd"}, "inaccessible"),
    ("13", {"citation_status": "", "citation_primary_link": "", "citation_raw_reference_text": "see www.example.com for details"}, "inaccessible"),
    ("14", {"citation_is_accessible": True, "citation_primary_link": "https://x.org/a", "citation_raw_reference_text": "Smith (2020)"}, "accessible"),
    ("15", {}, "no_link"),
    ("16", {"citation_status": "verified_metadata", "citation_is_accessible": None, "citation_primary_link": "https://x.org/a", "citation_raw_reference_text": "Smith (2020)"}, "accessible"),
    ("17", {"citation_status": "  no-link  "}, "no_link"),
    ("18", {"citation_status": "unknown_status", "citation_is_accessible": True, "citation_primary_link": "", "citation_raw_reference_text": "Smith (2020)"}, "accessible"),
]


@pytest.mark.parametrize("name,citation,expected", CASES, ids=[case[0] for case in CASES])
def test_shared_cases(name, citation, expected):
    assert citation_display_status(citation) == expected


@pytest.mark.parametrize("citation", [None, "", [], 0, {"citation_status": None, "citation_primary_link": None, "citation_raw_reference_text": None}])
def test_empty_and_non_dict(citation):
    assert citation_display_status(citation) == "no_link"


@pytest.mark.parametrize("result", [{}, {"citations": None}, {"citations": []}])
def test_missing_citations(result):
    original = deepcopy(result)
    assert add_citation_display_status(result) == original


def test_get_old_result_and_locked_view(monkeypatch):
    citation = {"citation_status": "verified_url", "extra": "preserved"}
    stored = {"citations": [citation, None], "access": {"unlocked": True}, "ai_synthesis": {"summary": "text"}}
    original = deepcopy(stored)

    class DB:
        def table(self, *_): return self
        def select(self, *_): return self
        def eq(self, *_): return self
        def execute(self): return SimpleNamespace(data=[{"user_id": "owner", "status": "completed", "result_json": stored}])

    monkeypatch.setattr(main.DatabasePersistenceService, "get_client", DB)
    result = asyncio.run(main.get_scan_status("run", "owner")).result
    assert result["citations"] == [dict(citation, display_status="accessible"), None]
    assert stored == original
    stored["access"]["unlocked"] = False
    expected_locked = locked_scan_view(deepcopy(stored))
    locked = asyncio.run(main.get_scan_status("run", "owner")).result
    assert locked["citations"] == []
    assert locked["access"]["locked_counts"] == expected_locked["access"]["locked_counts"]
    assert stored["citations"] == original["citations"]


def test_js_regex_boundaries_and_strict_boolean():
    assert citation_display_status({"citation_raw_reference_text": "éx.org"}) == "inaccessible"
    assert citation_display_status({"citation_status": "no\ufefflink"}) == "no_link"
    assert citation_display_status({"citation_status": "unknown", "citation_is_accessible": 1}) == "inaccessible"
