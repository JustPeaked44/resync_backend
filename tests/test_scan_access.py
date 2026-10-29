import json
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import main
from services.scan_access import locked_scan_view


def test_locked_view_withholds_report_content_everywhere():
    full = {
        "access": {"unlocked": False, "unlock_cost": 1, "locked_counts": {}},
        "manuscript_sections": [
            {"name": "Results", "text": "Visible first section. First source. Second source. Third source."},
            {"name": "Discussion", "text": "HIDDEN_SECTION_TEXT"},
            {"name": "References", "text": "HIDDEN_REFERENCE_TEXT"}],
        "sections_analyzed": ["Results", "Discussion", "References"],
        "recommendations": [
            {"id": "r-2-1", "section": "Discussion", "original_sentence": "HIDDEN_ORIGINAL",
             "recommended_revision": "HIDDEN_REVISION"},
            {"id": "r-1-3", "section": "Results", "original_sentence": "Third source.",
             "recommended_revision": "HIDDEN_THIRD_REVISION"},
            {"id": "r-1-2", "section": "Results", "original_sentence": "Second source.",
             "recommended_revision": "Visible second revision.", "category": "claims"},
            {"id": "r-1-1", "section": "Results", "original_sentence": "First source.",
             "recommended_revision": "Visible first revision.", "category": "objectives"}],
        "citations": [{"raw": "HIDDEN_CITATION_ENTRY"}], "citations_audited": 1,
        "inconsistencies_found": 1,
        "inconsistencies": [{"evidence_a": "HIDDEN_FINDING_QUOTE", "explanation_why": "HIDDEN_FINDING_EXPLANATION"}],
        "manuscript_flow": [{"order": 1, "section_a": "Results", "section_b": "Discussion",
                             "scores": {"overall_coherence": 0.4}, "pair_weight": 0.5,
                             "culprit_sentences": {"section_a": [{"sentence": "HIDDEN_CULPRIT"}], "section_b": []},
                             "explanation": {"summary": "HIDDEN_PAIR_EXPLANATION"}}],
        "ai_synthesis": {"summary": "HIDDEN_SUMMARY", "strongest_pair": None, "weakest_pair": None},
        "score_breakdown": {"overall_score": 60, "band": "Needs Revision",
                            "dimensions": {"claim_support": 0.6}, "dimension_weights": {"claim_support": 1}},
    }
    original = deepcopy(full)
    locked = locked_scan_view(full)
    raw = json.dumps(locked)
    assert full == original
    assert locked["manuscript_sections"][0] == original["manuscript_sections"][0]
    for source, projected in zip(original["manuscript_sections"][1:], locked["manuscript_sections"][1:]):
        assert projected["name"] == source["name"] and projected["text"] is None
        assert len(projected["placeholder"]["heading"]) == len(source["name"])
        assert projected["placeholder"]["paragraphs"]
    assert [r["id"] for r in locked["recommendations"]] == ["r-1-1", "r-1-2"]
    assert [r["category"] for r in locked["recommendations"]] == ["objectives", "claims"]
    assert locked["access"]["locked_counts"] == {"sections": 2, "recommendations": 2, "citations": 1}
    assert locked["citations"] == locked["inconsistencies"] == []
    assert locked["manuscript_flow"][0]["scores"]["overall_coherence"] == 0.4
    assert locked["score_breakdown"]["dimension_weights"] == {"claim_support": 1}
    assert locked["inconsistencies_found"] == locked["citations_audited"] == 1
    for secret in ("HIDDEN_SECTION_TEXT", "HIDDEN_REFERENCE_TEXT", "HIDDEN_ORIGINAL", "HIDDEN_REVISION",
                   "HIDDEN_THIRD_REVISION", "HIDDEN_CITATION_ENTRY", "HIDDEN_FINDING_QUOTE",
                   "HIDDEN_FINDING_EXPLANATION", "HIDDEN_CULPRIT", "HIDDEN_PAIR_EXPLANATION", "HIDDEN_SUMMARY"):
        assert secret not in raw
    assert locked_scan_view(dict(original, access={"unlocked": True})) == dict(original, access={"unlocked": True})
    assert locked_scan_view({k: v for k, v in original.items() if k != "access"}) == {
        k: v for k, v in original.items() if k != "access"}


def test_placeholders_are_bounded_deterministic_and_do_not_echo_withheld_words():
    words = ("albatross ambergris anemone antelope aperture armadillo asparagus avalanche "
             "basilisk bellflower blackbird bluegrass breadfruit brontosaurus buttercup "
             "caterpillar chamomile chestnut cinnamon clockwork cloudburst coralreef "
             "crocodile dandelion dragonfly eucalyptus firefly flamingo foxglove "
             "galapagos gardenia goldfinch grapeseed grasshopper hammerhead hawthorn "
             "hibiscus honeycomb hummingbird jacaranda kingfisher lavender lemongrass "
             "magnolia marigold moonflower nightingale oleander peppermint periwinkle "
             "pineapple primrose rhinoceros rosemary sandpiper seahorse snowdrop "
             "starfish sunflower tamarind tangerine watercress woodpecker").split()
    assert len(set(words)) >= 50 and all(len(word) >= 6 for word in words)
    full = {
        "analysis_run_id": "fixed-scan", "access": {"unlocked": False},
        "manuscript_sections": [{"name": "First", "text": "Readable opening."}] + [
            {"name": f"Chapter {index}", "text": " ".join(words) + "\n" + "a" * 9000}
            for index in range(12)],
        "recommendations": [], "citations": [], "inconsistencies": [],
        "ai_synthesis": {"summary": None}, "manuscript_flow": [],
    }
    locked = locked_scan_view(full)
    assert locked == locked_scan_view(full)
    assert locked != locked_scan_view(dict(full, analysis_run_id="another-scan"))
    placeholders = [section["placeholder"] for section in locked["manuscript_sections"][1:]]
    assert all(len(item["heading"]) == len(section["name"]) for item, section in zip(placeholders, full["manuscript_sections"][1:]))
    assert all(len(item["paragraphs"]) == 2 for item in placeholders[:10])
    assert all(sum(map(len, item["paragraphs"])) + len(item["heading"]) <= 4000 for item in placeholders)
    assert sum(len(item["heading"]) + sum(map(len, item["paragraphs"])) for item in placeholders) <= 40000
    assert len(placeholders[0]["paragraphs"][0]) % 40 == 0
    raw = json.dumps(locked).lower()
    assert all(word not in raw for word in words)
    assert all("placeholder" not in section for section in locked_scan_view(dict(full, access={"unlocked": True}))["manuscript_sections"])


def test_scan_provision_keeps_zero_balance_and_rate_limit(monkeypatch):
    calls = []

    class DB:
        def table(self, name):
            self.name = name
            return self

        def select(self, *_):
            return self

        def eq(self, *_):
            return self

        def insert(self, *_):
            return self

        def execute(self):
            return SimpleNamespace(data=[{"user_id": "owner"}])

    async def check(user):
        calls.append(("rate", user))

    async def balance(user):
        calls.append(("balance", user))
        return 0

    async def forbidden(*_):
        raise AssertionError("scan debit must not run")

    monkeypatch.setattr(main, "_check_scan_rate_limit", check)
    monkeypatch.setattr(main.DatabasePersistenceService, "get_client", DB)
    monkeypatch.setattr(main.credits_service, "get_balance", balance)
    monkeypatch.setattr(main.credits_service, "debit_scan_credit", forbidden)
    run_id, _, remaining = asyncio.run(main._provision_analysis_run(main.ScanRequest(user_id="owner")))
    assert run_id and remaining == 0 and calls == [("rate", "owner"), ("balance", "owner")]


def test_get_and_manuscript_routes_use_locked_view(monkeypatch):
    result = {"access": {"unlocked": False, "unlock_cost": 1},
              "manuscript_sections": [{"name": "First", "text": "Visible first."},
                                      {"name": "Second", "text": "SECRET SECOND."}],
              "recommendations": [], "citations": [], "inconsistencies": [],
              "ai_synthesis": {"summary": "SECRET SUMMARY"}, "manuscript_flow": [],
              "doc_url": "https://docs.google.com/document/d/example/edit"}

    class DB:
        def table(self, *_):
            return self

        def select(self, *_):
            return self

        def eq(self, *_):
            return self

        def execute(self):
            return SimpleNamespace(data=[{"analysis_run_id": "run", "user_id": "owner",
                                          "status": "completed", "result_json": result}])

    monkeypatch.setattr(main.DatabasePersistenceService, "get_client", DB)
    response = asyncio.run(main.get_scan_status("run", "owner"))
    preview = asyncio.run(main.get_manuscript_preview("run", authenticated_user_id="owner"))
    assert response.result["manuscript_sections"][1]["text"] is None
    assert "SECRET" not in json.dumps(response.model_dump(mode="json"))
    assert preview == {"available": True, "text": "Visible first."}
    result["access"]["unlocked"] = True
    assert asyncio.run(main.get_manuscript_preview("run", authenticated_user_id="owner"))["text"] == "Visible first.\n\nSECRET SECOND."
    result.pop("access")
    result["doc_url"] = "legacy.docx"
    assert asyncio.run(main.get_manuscript_preview("run", authenticated_user_id="owner"))["text"] == "Visible first.\n\nSECRET SECOND."
