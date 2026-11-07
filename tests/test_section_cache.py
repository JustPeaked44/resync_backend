"""Parsed sections are saved in Supabase and reused. A cache problem must never fail a scan."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import main
from services import section_cache
from services.section_cache import SectionCache

PARSE = {"parsed_sections": {"introduction": "Intro text here.", "methodology": "Method text here."},
         "section_order": ["introduction", "methodology"], "section_roles": {"introduction": "introduction", "methodology": "methodology"},
         "structure": "headings"}
MS_ID = "11111111-1111-4111-8111-111111111111"
OTHER_MS = "22222222-2222-4222-8222-222222222222"
USER, STRANGER = "user-1", "user-2"


class FakeTable:
    """Just enough of the Supabase query builder for the cache: select/eq/order/limit/upsert, on a list of rows."""

    def __init__(self, rows):
        self.rows, self.filters, self.limit_to, self.pending = rows, {}, None, None

    def select(self, *_):
        return self

    def eq(self, column, value):
        self.filters[column] = value
        return self

    def order(self, column, desc=False):
        self.rows.sort(key=lambda row: row.get(column, ""), reverse=desc)
        return self

    def limit(self, count):
        self.limit_to = count
        return self

    def upsert(self, row, on_conflict=None):
        self.pending = (row, on_conflict)
        return self

    def execute(self):
        if self.pending:
            row, key = self.pending
            self.rows[:] = [existing for existing in self.rows if existing.get(key) != row.get(key)] + [dict(row, created_at=str(len(self.rows)))]
            return SimpleNamespace(data=[row])
        found = [row for row in self.rows if all(row.get(column) == value for column, value in self.filters.items())]
        return SimpleNamespace(data=found[:self.limit_to] if self.limit_to else found)


class FakeClient:
    def __init__(self):
        self.rows = []

    def table(self, name):
        assert name == section_cache.TABLE
        return FakeTable(self.rows)


@pytest.fixture
def database(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(section_cache.DatabasePersistenceService, "get_client", lambda: client)
    monkeypatch.delenv("SECTION_CACHE", raising=False)
    return client


def test_the_hash_changes_with_the_content_the_template_and_nothing_else():
    one = SectionCache.content_hash(b"file bytes", None)
    assert one == SectionCache.content_hash(b"file bytes", None)
    assert one != SectionCache.content_hash(b"other bytes", None)
    assert one != SectionCache.content_hash(b"file bytes", ["Introduction"])
    assert SectionCache.content_hash("same text", None) == SectionCache.content_hash(b"same text", None)   # Google Docs text or file bytes


def test_a_saved_parse_is_found_again_for_the_same_user_and_file(database):
    digest = SectionCache.content_hash(b"my thesis")
    assert asyncio.run(SectionCache.find(USER, digest)) is None
    assert asyncio.run(SectionCache.save(MS_ID, USER, digest, PARSE, "the full extracted text")) is True
    found = asyncio.run(SectionCache.find(USER, digest))
    assert found["raw_text"] == "the full extracted text" and found["manuscript_id"] == MS_ID
    assert found["parse_result"]["parsed_sections"] == PARSE["parsed_sections"] and found["parse_result"]["structure"] == "headings"
    assert asyncio.run(SectionCache.find(STRANGER, digest)) is None                          # another person's file is never reused
    assert asyncio.run(SectionCache.find(USER, SectionCache.content_hash(b"changed thesis"))) is None


def test_preview_metadata_round_trips_and_old_rows_still_load(database):
    parsed = dict(PARSE, display_names={"introduction": "Introduction"}, section_blocks={"introduction": [
        {"type": "table", "rows": [["one", ""]]}]})
    asyncio.run(SectionCache.save(MS_ID, USER, "hash", parsed, "text"))
    found = asyncio.run(SectionCache.find(USER, "hash"))["parse_result"]
    assert found["display_names"] == parsed["display_names"]
    assert found["section_blocks"] == parsed["section_blocks"]
    database.rows[0]["parsed"] = dict(PARSE)
    assert asyncio.run(SectionCache.find(USER, "hash"))["parse_result"]["parsed_sections"] == PARSE["parsed_sections"]


def test_saving_the_same_manuscript_twice_keeps_one_row_and_a_rescan_gets_its_own_row(database):
    digest = SectionCache.content_hash(b"my thesis")
    asyncio.run(SectionCache.save(MS_ID, USER, digest, PARSE, "text"))
    asyncio.run(SectionCache.save(MS_ID, USER, digest, PARSE, "text"))
    asyncio.run(SectionCache.save(OTHER_MS, USER, digest, PARSE, "text"))                 # the same file scanned again
    assert sorted(row["manuscript_id"] for row in database.rows) == [MS_ID, OTHER_MS]


def test_a_manuscript_can_be_loaded_by_its_id_only_by_its_owner(database):
    asyncio.run(SectionCache.save(MS_ID, USER, "hash", PARSE, "text"))
    assert asyncio.run(SectionCache.load_for_manuscript(MS_ID, USER))["parse_result"]["section_order"] == ["introduction", "methodology"]
    assert asyncio.run(SectionCache.load_for_manuscript(MS_ID, STRANGER)) is None
    assert asyncio.run(SectionCache.load_for_manuscript(OTHER_MS, USER)) is None


def test_a_cache_failure_never_raises_and_the_switch_turns_it_off(monkeypatch, database):
    def broken():
        raise RuntimeError("relation manuscript_section_cache does not exist")
    monkeypatch.setattr(section_cache.DatabasePersistenceService, "get_client", broken)
    assert asyncio.run(SectionCache.find(USER, "hash")) is None
    assert asyncio.run(SectionCache.load_for_manuscript(MS_ID, USER)) is None
    assert asyncio.run(SectionCache.save(MS_ID, USER, "hash", PARSE, "text")) is False
    monkeypatch.setattr(section_cache.DatabasePersistenceService, "get_client", lambda: database)
    monkeypatch.setenv("SECTION_CACHE", "off")
    assert asyncio.run(SectionCache.save(MS_ID, USER, "hash", PARSE, "text")) is False and database.rows == []


def test_a_row_without_the_parsed_sections_is_ignored(database):
    database.rows.append({"manuscript_id": MS_ID, "user_id": USER, "content_hash": "hash", "parsed": {"nothing": True}, "raw_text": "x"})
    assert asyncio.run(SectionCache.find(USER, "hash")) is None


# ---- /analyze_v7 with a manuscript_id ---------------------------------------------------------------------------

def test_analyze_v7_needs_sections_or_a_manuscript_id():
    with pytest.raises(ValueError):
        main.V7AnalyzeRequest()
    with pytest.raises(ValueError):
        main.V7AnalyzeRequest(sections=[{"name": "A", "text": "only one"}])
    assert main.V7AnalyzeRequest(manuscript_id=MS_ID).sections is None
    assert len(main.V7AnalyzeRequest(sections=[{"name": "A", "text": "a"}, {"name": "B", "text": "b"}]).sections) == 2


def test_analyze_v7_uses_the_saved_sections_instead_of_parsing(monkeypatch, database):
    seen = {}
    parse = dict(PARSE, parsed_sections={"introduction": "A saved introduction with enough words.",
                                         "methodology": "A saved methodology with enough words.",
                                         "references": "Smith (2020) is a reference entry that is not prose."},
                 section_order=["introduction", "methodology", "references"],
                 section_roles={"introduction": "introduction", "methodology": "methodology", "references": "references"})
    asyncio.run(SectionCache.save(MS_ID, USER, "hash", parse, "text"))

    async def no_limit(*_):
        return None

    async def no_explanation(sections, flow):
        return [], [], {"summary": None, "strongest_pair": None, "weakest_pair": None}

    def ml(items):
        seen["sections"] = items
        return {"pairs": [{"scores": {"objectives_alignment": 1, "methodology_fit": 1, "claim_support": 1, "numeric_consistency": 1,
                                      "overall_coherence": 0.9, "pair_weight": 1}}]}

    monkeypatch.setattr(main, "_check_scan_rate_limit", no_limit)
    monkeypatch.setattr(main, "explain_flow", no_explanation)
    monkeypatch.setattr(main.ml_client, "analyze_v7", ml)
    result = asyncio.run(main.analyze_v7(main.V7AnalyzeRequest(manuscript_id=MS_ID), user_id=USER))
    assert result["result_version"] == "v7"
    assert [(s["name"], s["role"]) for s in seen["sections"]] == [("introduction", "introduction"), ("methodology", "methodology")]   # references are not compared

    with pytest.raises(HTTPException) as other_user:
        asyncio.run(main.analyze_v7(main.V7AnalyzeRequest(manuscript_id=MS_ID), user_id=STRANGER))
    assert other_user.value.status_code == 404                       # someone else's manuscript looks exactly like a missing one
    with pytest.raises(HTTPException) as missing:
        asyncio.run(main.analyze_v7(main.V7AnalyzeRequest(manuscript_id=OTHER_MS), user_id=USER))
    assert missing.value.status_code == 404
    with pytest.raises(HTTPException) as malformed:
        asyncio.run(main.analyze_v7(main.V7AnalyzeRequest(manuscript_id="not-a-uuid"), user_id=USER))
    assert malformed.value.status_code == 422
