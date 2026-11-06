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
        await main._run_scan_job(None, 'abcdefgh-id', None, 0)
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
        await main._run_scan_job(None, 'abcdefgh-id', None, 0)
        result = await main.get_scan_status('abcdefgh-id', 'owner')
        assert result.error == main.GENERIC_FAILURE_MESSAGE.format(scan_number='abcdefgh')
        assert result.failure_kind == 'error' and 'secret' not in result.error
        db.rows[0]['status'] = 'processing'
        async def wait(*args, **kwargs): await asyncio.sleep(10)
        monkeypatch.setattr(main, '_execute_scan_pipeline', wait)
        task = asyncio.create_task(main._run_scan_job(None, 'abcdefgh-id', None, 0))
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
