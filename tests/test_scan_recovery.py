import asyncio
from datetime import timedelta
from types import SimpleNamespace

import main


class FakeDB:
    def __init__(self, rows):
        self.rows = rows
        self.updates = []

    def table(self, _):
        self.filters = []
        self.value = None
        return self

    def select(self, *_):
        return self

    def update(self, value):
        self.value = value
        return self

    def eq(self, key, value):
        self.filters.append((key, value))
        return self

    def is_(self, key, value):
        self.filters.append((key, None))
        return self

    def lt(self, key, value):
        self.filters.append((key, ('lt', value)))
        return self

    def neq(self, key, value):
        self.filters.append((key, ('ne', value)))
        return self

    def execute(self):
        def matches(row):
            for key, value in self.filters:
                actual = row.get(key)
                if isinstance(value, tuple):
                    if value[0] == 'lt' and not (actual and actual < value[1]): return False
                    if value[0] == 'ne' and actual == value[1]: return False
                elif actual != value: return False
            return True
        found = [row for row in self.rows if matches(row)]
        if self.value is not None:
            self.updates.append((self.value.copy(), self.filters.copy()))
            for row in found: row.update(self.value)
        return SimpleNamespace(data=found)


def row(name, status='processing'):
    return {'analysis_run_id': name, 'user_id': 'owner', 'status': status, 'result_json': None,
            'created_at': (main.PROCESS_START - timedelta(minutes=10)).isoformat()}


def test_restart_grace_active_and_completed(monkeypatch):
    rows = [row('old'), row('active'), row('done', 'completed')]
    db = FakeDB(rows)
    monkeypatch.setattr(main.DatabasePersistenceService, 'get_client', lambda: db)
    monkeypatch.setattr(main, 'INTERRUPTED_GRACE_SECONDS', 99999)
    main._active_scans.add('active')
    async def run():
        assert (await main.get_scan_status('old', 'owner')).status == 'processing'
        monkeypatch.setattr(main, 'INTERRUPTED_GRACE_SECONDS', 0)
        failed = await main.get_scan_status('old', 'owner')
        assert (failed.status, failed.error, failed.failure_kind) == ('failed', main.INTERRUPTED_MESSAGE, 'interrupted')
        assert failed.elapsed_seconds >= 600
        assert (await main.get_scan_status('active', 'owner')).status == 'processing'
        await main._sweep_interrupted()
        assert rows[1]['status'] == 'processing' and rows[2]['status'] == 'completed'
        assert len(db.updates) == 2
    try:
        asyncio.run(run())
    finally:
        main._active_scans.discard('active')


def test_sweep_unavailable_never_raises(monkeypatch):
    def unavailable(): raise ValueError('offline')
    monkeypatch.setattr(main.DatabasePersistenceService, 'get_client', unavailable)
    asyncio.run(main._sweep_interrupted())


def test_deadline_failure_and_late_completion(monkeypatch):
    db = FakeDB([row('abcdefgh-id')])
    monkeypatch.setattr(main.DatabasePersistenceService, 'get_client', lambda: db)
    monkeypatch.setattr(main, 'SCAN_DEADLINE_SECONDS', 0.01)
    async def pipeline(*args, **kwargs): await asyncio.sleep(10)
    monkeypatch.setattr(main, '_execute_scan_pipeline', pipeline)
    async def run():
        await main._run_scan_job(SimpleNamespace(user_id='owner'), 'abcdefgh-id', None, 0)
        assert db.rows[0]['error_message'] == main.TIMEOUT_MESSAGE
        result = SimpleNamespace(analysis_run_id='abcdefgh-id', model_dump=lambda **kwargs: {'x': 1})
        await main._save_completed_result(result)
        assert db.rows[0]['status'] == 'failed'
        assert (await main.get_scan_status('abcdefgh-id', 'owner')).failure_kind == 'timeout'
    asyncio.run(run())


def test_safe_failure_and_cancellation(monkeypatch):
    db = FakeDB([row('abcdefgh-id')])
    monkeypatch.setattr(main.DatabasePersistenceService, 'get_client', lambda: db)
    async def pipeline(*args, **kwargs): raise ValueError('secret technical error')
    monkeypatch.setattr(main, '_execute_scan_pipeline', pipeline)
    async def run():
        await main._run_scan_job(SimpleNamespace(user_id='owner'), 'abcdefgh-id', None, 0)
        result = await main.get_scan_status('abcdefgh-id', 'owner')
        assert result.error == main.GENERIC_FAILURE_MESSAGE.format(scan_number='abcdefgh')
        assert result.failure_kind == 'error' and 'secret' not in result.error
        db.rows[0]['status'] = 'processing'
        async def wait(*args, **kwargs): await asyncio.sleep(10)
        monkeypatch.setattr(main, '_execute_scan_pipeline', wait)
        task = asyncio.create_task(main._run_scan_job(SimpleNamespace(user_id='owner'), 'abcdefgh-id', None, 0))
        await asyncio.sleep(0)
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass
        assert db.rows[0]['status'] == 'failed'
    asyncio.run(run())
    assert main._failure_message('id', main.HTTPException(404, detail='404: Google Doc not found.')) == 'Google Doc not found.'


def test_concurrency_default(monkeypatch):
    monkeypatch.delenv('SCAN_CONCURRENCY', raising=False)
    assert main._scan_concurrency() == 1
    monkeypatch.setenv('SCAN_CONCURRENCY', 'bad')
    assert main._scan_concurrency() == 1


def test_enqueue_failure_marks_row_failed(monkeypatch):
    db = FakeDB([row('abcdefgh-id')])
    monkeypatch.setattr(main.DatabasePersistenceService, 'get_client', lambda: db)
    async def provision(*args, **kwargs): return 'abcdefgh-id', 'manuscript', 0
    def enqueue(*args): raise RuntimeError('queue failed')
    monkeypatch.setattr(main, '_provision_analysis_run', provision)
    monkeypatch.setattr(main, '_enqueue_scan', enqueue)
    async def run():
        try:
            await main.start_scan(main.ScanRequest(user_id='owner'), 'owner')
        except RuntimeError:
            pass
        assert db.rows[0]['status'] == 'failed'
        assert db.rows[0]['error_message'] == main.GENERIC_FAILURE_MESSAGE.format(scan_number='abcdefgh')
    asyncio.run(run())


def test_conditional_filters_and_terminal_races(monkeypatch):
    db = FakeDB([row('old'), row('done', 'completed')])
    monkeypatch.setattr(main, '_active_scans', {'active'})
    db.rows[1]['result_json'] = {'report': 'preserved'}
    monkeypatch.setattr(main.DatabasePersistenceService, 'get_client', lambda: db)
    async def run():
        await main._sweep_interrupted()
        assert db.updates[-1][1] == [('status', 'processing'), ('result_json', None), ('created_at', ('lt', main.PROCESS_START.isoformat())), ('analysis_run_id', ('ne', 'active'))]
        await main._mark_failed('done', main.INTERRUPTED_MESSAGE)
        assert db.updates[-1][1] == [('analysis_run_id', 'done'), ('status', 'processing'), ('result_json', None), ('created_at', ('lt', main.PROCESS_START.isoformat()))]
        await main._mark_failed('done', main.TIMEOUT_MESSAGE)
        assert db.updates[-1][1] == [('analysis_run_id', 'done'), ('status', 'processing')]
        result = SimpleNamespace(analysis_run_id='done', model_dump=lambda **_: {'late': True})
        assert not await main._save_completed_result(result)
        assert db.updates[-1][1] == [('analysis_run_id', 'done'), ('status', 'processing')]
        assert db.rows[1]['status'] == 'completed' and db.rows[1]['result_json'] == {'report': 'preserved'}
    asyncio.run(run())


def test_failure_write_retries_once_even_when_both_attempts_fail(monkeypatch, caplog):
    for failures in (1, 2):
        calls = []
        db = FakeDB([row('id')])
        def client():
            calls.append(1)
            if len(calls) <= failures: raise RuntimeError('offline')
            return db
        monkeypatch.setattr(main.DatabasePersistenceService, 'get_client', client)
        asyncio.run(main._mark_failed('id', main.TIMEOUT_MESSAGE))
        assert len(calls) == 2
        assert db.rows[0]['status'] == ('failed' if failures == 1 else 'processing')
    assert 'attempt 2' in caplog.text


def test_each_safe_refusal_is_returned_as_refused(monkeypatch):
    messages = [main.NOT_A_PAPER_MESSAGE, 'Google Doc not found.', 'Unable to open the document.',
        'Unable to access document.', 'Document access restricted.', 'Invalid Google Docs URL format.',
        'The submitted Google Doc appears to be empty.', 'The uploaded document appears to be empty',
        'Unsupported file format.', 'No valid manuscript provided.', 'Invalid base64 encoding',
        'This Google Doc is too large', 'This Google Doc is larger', 'Document is too large to process']
    async def run():
        for message in messages:
            db = FakeDB([dict(row('id', 'failed'), error_message=message)])
            monkeypatch.setattr(main.DatabasePersistenceService, 'get_client', lambda: db)
            response = await main.get_scan_status('id', 'owner')
            assert (response.failure_kind, response.error) == ('refused', message)
    asyncio.run(run())


def test_sync_endpoints_record_error_and_deadline(monkeypatch):
    import io
    import pytest
    from starlette.datastructures import UploadFile
    async def provision(*args, **kwargs): return 'abcdefgh-id', 'm', 0
    monkeypatch.setattr(main, '_provision_analysis_run', provision)
    monkeypatch.setattr(main, 'SCAN_DEADLINE_SECONDS', 0.001)
    async def run():
        for upload in (False, True):
            for timeout in (False, True):
                db = FakeDB([row('abcdefgh-id')])
                monkeypatch.setattr(main.DatabasePersistenceService, 'get_client', lambda: db)
                async def pipeline(*args, **kwargs):
                    if timeout: await asyncio.sleep(1)
                    raise ValueError('secret')
                monkeypatch.setattr(main, '_execute_scan_pipeline', pipeline)
                with pytest.raises((ValueError, asyncio.TimeoutError)):
                    if upload:
                        await main.upload_and_run_scan(file=UploadFile(filename='x.docx', file=io.BytesIO(b'doc')), user_id='owner', manuscript_id=None, manuscript_title=None, template_toc=None, style_reference_url=None, authenticated_user_id='owner')
                    else:
                        await main.run_scan(main.ScanRequest(user_id='owner'), 'owner')
                assert db.rows[0]['status'] == 'failed'
                assert db.rows[0]['error_message'] == (main.TIMEOUT_MESSAGE if timeout else main.GENERIC_FAILURE_MESSAGE.format(scan_number='abcdefgh'))
    asyncio.run(run())


def test_cancelled_insert_finishes_before_failure_write(monkeypatch):
    import threading
    entered, release = threading.Event(), threading.Event()
    events = []
    class DB:
        def table(self, name): self.name = name; return self
        def select(self, *_): return self
        def eq(self, *_): return self
        def insert(self, *_): self.inserting = True; return self
        def execute(self):
            if getattr(self, 'inserting', False):
                entered.set(); release.wait(5); events.append('insert')
            return SimpleNamespace(data=[{}])
    async def noop(*args, **kwargs): return 'm'
    async def failed(*args): events.append('failed')
    monkeypatch.setattr(main, '_check_scan_rate_limit', noop)
    monkeypatch.setattr(main, '_resolve_manuscript_id', noop)
    monkeypatch.setattr(main, '_mark_failed', failed)
    monkeypatch.setattr(main.DatabasePersistenceService, 'get_client', lambda: DB())
    async def run():
        task = asyncio.create_task(main._provision_analysis_run(main.ScanRequest(user_id='owner')))
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.01)
        assert events == []
        release.set()
        try: await task
        except asyncio.CancelledError: pass
        assert events == ['insert', 'failed']
    try: asyncio.run(run())
    finally: release.set()
