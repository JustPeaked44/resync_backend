import asyncio
import io
import os
import subprocess
import sys
from types import SimpleNamespace

import main
import pytest
from starlette.datastructures import UploadFile
from services import gemini, verdict_cache
from tests.test_scan_recovery import FakeDB, row
from tests.test_v7_pipeline import build_scan
from tests.test_verdict_cache import FakeClient


def test_queued_users_keep_separate_verdicts(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(verdict_cache.DatabasePersistenceService, 'get_client', lambda: client)
    monkeypatch.setenv('VERDICT_CACHE', 'on')
    monkeypatch.setattr(main, 'SCAN_CONCURRENCY', 1)
    monkeypatch.setattr(main, 'SCAN_SLOT', asyncio.Semaphore(1))
    seen = []
    verdict_cache.VerdictCache.forget_everything_in_memory()

    async def pipeline(req, scan_id, *args, **kwargs):
        assert gemini.scan_context.get() == scan_id
        assert gemini.scan_user.get() == req.user_id
        key = verdict_cache.VerdictCache.fingerprint('same paper')
        found = await verdict_cache.VerdictCache.get_many([key])
        seen.append((req.user_id, found.get(key)))
        await verdict_cache.VerdictCache.save_many({key: {'severity': req.user_id}})

    monkeypatch.setattr(main, '_execute_scan_pipeline', pipeline)
    async def run():
        for i, user in enumerate(('A', 'B', 'A')):
            main._enqueue_scan(SimpleNamespace(user_id=user), str(i), None, 0)
        await asyncio.gather(*main._scan_workers)
    try:
        asyncio.run(run())
        assert seen == [('A', None), ('B', None), ('A', {'severity': 'A'})]
        assert len(client.rows) == 2
    finally:
        verdict_cache.VerdictCache.forget_everything_in_memory()


def test_restart_and_deadline_keep_terminal_rows(monkeypatch):
    rows = [row('old'), row('done', 'completed'), row('timeout'), row('finished', 'completed')]
    rows[1]['result_json'] = rows[3]['result_json'] = {'report': 'kept'}
    db = FakeDB(rows)
    monkeypatch.setattr(main.DatabasePersistenceService, 'get_client', lambda: db)
    monkeypatch.setattr(main, 'INTERRUPTED_GRACE_SECONDS', 0)
    monkeypatch.setattr(main, 'SCAN_DEADLINE_SECONDS', 0.001)
    async def pipeline(*args, **kwargs):
        await asyncio.sleep(1)
    monkeypatch.setattr(main, '_execute_scan_pipeline', pipeline)
    async def run():
        response = await main.get_scan_status('old', 'owner')
        assert (response.status, response.error) == ('failed', main.INTERRUPTED_MESSAGE)
        await main._run_scan_job(SimpleNamespace(user_id='owner'), 'timeout', None, 0)
        await main._mark_failed('finished', main.TIMEOUT_MESSAGE)
    asyncio.run(run())
    assert rows[2]['error_message'] == main.TIMEOUT_MESSAGE
    assert rows[1]['status'] == rows[3]['status'] == 'completed'
    assert rows[3]['result_json'] == {'report': 'kept'}


def test_normal_parser_path_does_not_load_spacy():
    code = "import sys; import main; from services.parser import ManuscriptParserService as P; P.parse_manuscript_sections('Introduction\\nSome words.'); assert 'spacy' not in sys.modules"
    subprocess.run([sys.executable, '-c', code], check=True,
                   env={**os.environ, 'ML_BACKEND': 'modal', 'PYTHON_DOTENV_DISABLED': '1'})


def test_fake_pipeline_produces_valid_response(monkeypatch):
    monkeypatch.setattr(main, 'check_research_paper', lambda *a, **k: {'ok': True, 'reason': ''})
    payload, captured, _ = build_scan(monkeypatch)
    assert main.ScanResponse.model_validate(payload).analysis_run_id == 'run'
    assert captured['events'] == ['save', 'notify', 'publish']


@pytest.mark.parametrize('upload', [False, True])
def test_sync_scan_context_and_busy_slot(monkeypatch, upload):
    db = FakeDB([row('sync')])
    monkeypatch.setattr(main.DatabasePersistenceService, 'get_client', lambda: db)
    async def provision(*args, **kwargs): return 'sync', 'ms', 0
    monkeypatch.setattr(main, '_provision_analysis_run', provision)
    async def pipeline(req, scan_id, *args, **kwargs):
        assert gemini.scan_user.get() == req.user_id == 'owner'
        assert gemini.scan_context.get() == scan_id == 'sync'
        raise ValueError('fake failure')
    monkeypatch.setattr(main, '_execute_scan_pipeline', pipeline)
    async def invoke():
        if upload:
            return await main.upload_and_run_scan(file=UploadFile(filename='x.docx', file=io.BytesIO(b'x')), user_id='owner', manuscript_id=None, manuscript_title=None, template_toc=None, style_reference_url=None, authenticated_user_id='owner')
        return await main.run_scan(main.ScanRequest(user_id='owner'), 'owner')
    async def run():
        monkeypatch.setattr(main, 'SCAN_SLOT', asyncio.Semaphore(1))
        with pytest.raises(ValueError): await invoke()
        db.rows[0]['status'] = 'processing'
        class Busy:
            async def acquire(self): raise asyncio.TimeoutError()
        monkeypatch.setattr(main, 'SCAN_SLOT', Busy())
        with pytest.raises(main.HTTPException) as exc: await invoke()
        assert exc.value.status_code == 503
        assert db.rows[0]['error_message'] == main.GENERIC_FAILURE_MESSAGE.format(scan_number='sync')
    asyncio.run(run())
