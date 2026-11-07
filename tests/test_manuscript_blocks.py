import json

from services.v7_scan import manuscript_preview_sections


def test_preview_limits_table_shape_and_total_size():
    section = {"name": "introduction", "text": "Original flattened text"}
    table = {"type": "table", "rows": [["x" * 501] + ["cell"] * 21] * 201}
    parsed = {"parsed_sections": {"introduction": section["text"]}, "section_order": ["introduction"],
              "display_names": {"introduction": "Introduction"}, "section_blocks": {"introduction": [table, table]}}
    preview = manuscript_preview_sections([section], parsed)[0]
    assert preview["text"] == section["text"] and preview["display_name"] == "Introduction"
    assert len(preview["blocks"]) == 1
    assert len(preview["blocks"][0]["rows"]) == 200
    assert len(preview["blocks"][0]["rows"][0]) == 20
    assert preview["blocks"][0]["rows"][0][0] == "x" * 499 + "…"
    assert len(json.dumps([preview["blocks"]]).encode()) <= 400 * 1024


def test_preview_limits_number_of_tables_and_falls_back_when_too_large():
    section = {"name": "a", "text": "text"}
    table = {"type": "table", "rows": [["x"]]}
    parsed = {"parsed_sections": {"a": "text"}, "section_order": ["a"], "section_blocks": {"a": [table] * 41}}
    assert len(manuscript_preview_sections([section], parsed)[0]["blocks"]) == 40
    parsed["section_blocks"]["a"] = [{"type": "paragraph", "text": "x" * (400 * 1024)}, table]
    assert manuscript_preview_sections([section], parsed)[0]["blocks"] is None
    assert manuscript_preview_sections([section], {"parsed_sections": {"a": "text"}})[0]["blocks"] is None
