import json
from pathlib import Path

import main
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


def test_table_free_response_matches_b5b_fixture_except_additive_fields():
    base = json.loads((Path(__file__).parent / "fixtures" / "b4b_scan_response_base.json").read_text(encoding="utf-8"))
    current = dict(base, manuscript_sections=[dict(base["manuscript_sections"][0], display_name="Introduction", blocks=None)])
    result = main.ScanResponse.model_validate(current).model_dump(mode="json")
    for item in result["manuscript_sections"]:
        item.pop("display_name")
        item.pop("blocks")
    assert result == base


def test_openapi_section_fields_are_optional_and_other_fields_remain_allowed():
    schema = main.app.openapi()["components"]["schemas"]["ScanResponse"]["properties"]["manuscript_sections"]["anyOf"][0]["items"]
    assert schema["additionalProperties"] is True
    assert set(schema["properties"]) == {"display_name", "blocks"}
    assert "required" not in schema
