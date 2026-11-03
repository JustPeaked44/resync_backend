from tempfile import TemporaryDirectory
from pathlib import Path

from scripts.check_schema_names import ROOT, check


def test_schema_checker_reports_unknown_table_query_and_payload_names():
    with TemporaryDirectory(dir=ROOT) as folder:
        path = Path(folder) / "bad.py"
        path.write_text('client.table("missing").execute()\n'
                        'client.table("analysis_run").select("old_column").execute()\n'
                        'client.table("analysis_run").update({"doc_url": "x"}).execute()\n'
                        'payload = {"score_breakdown_json": {}}\n'
                        'client.table("analysis_run").update(payload).execute()\n', encoding="utf-8")
        errors = check(path)
    assert len(errors) == 4
    assert all("bad.py:" in error for error in errors)
