from services.scan_access import display_counts, locked_scan_view


def test_counts_full_result_before_locking():
    result = {"access": {"unlocked": False}, "manuscript_sections": [
        {"name": "A", "text": "A"}, {"name": "B", "text": "B"}],
        "manuscript_flow": [
            {"order": i, "section_a": "A", "section_b": "B", "severity": "None", "scores": {"overall_coherence": 0.9}, "culprit_sentences": {}}
            for i in range(1, 5)],
        "recommendations": [{"pair_order": 1, "recommended_revision": "First"},
                            {"pair_order": 1, "recommended_revision": "Second"},
                            {"pair_order": 2, "recommended_revision": "Third"},
                            {"pair_order": 3, "recommended_revision": " ** ` "}],
        "citations": [{}], "inconsistencies": [], "ai_synthesis": {"summary": None}}
    expected = {"recommendations": 2, "all_clear": 2, "citations": 1}
    assert display_counts(result) == expected
    locked = {**locked_scan_view(result), "display_counts": display_counts(result)}
    assert locked["display_counts"] == expected
    assert display_counts({**result, "access": {"unlocked": True}}) == expected


def test_empty_and_old_results():
    assert display_counts({}) == {"recommendations": 0, "all_clear": 0, "citations": 0}
    assert display_counts({"manuscript_flow": [], "recommendations": []}) == display_counts({})


def test_get_adds_counts_for_old_saved_results_in_both_access_states(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    import main

    result = {"access": {"unlocked": False}, "manuscript_sections": [
        {"name": "A", "text": "first"}, {"name": "B", "text": "second"}],
        "manuscript_flow": [{"order": 1, "section_a": "A", "section_b": "B",
                            "severity": "None", "scores": {"overall_coherence": 0.9}, "culprit_sentences": {}}],
        "recommendations": [{"pair_order": 1, "recommended_revision": "improve"}],
        "citations": [{}], "inconsistencies": [], "ai_synthesis": {"summary": None}}

    class DB:
        def table(self, *_): return self
        def select(self, *_): return self
        def eq(self, *_): return self
        def execute(self):
            return SimpleNamespace(data=[{"analysis_run_id": "run", "user_id": "owner", "status": "completed", "result_json": result}])

    monkeypatch.setattr(main.DatabasePersistenceService, "get_client", DB)
    locked = asyncio.run(main.get_scan_status("run", "owner")).result
    result["access"]["unlocked"] = True
    unlocked = asyncio.run(main.get_scan_status("run", "owner")).result
    assert locked["display_counts"] == unlocked["display_counts"] == {"recommendations": 1, "all_clear": 0, "citations": 1}
    assert locked["citations"] == [] and len(unlocked["citations"]) == 1
