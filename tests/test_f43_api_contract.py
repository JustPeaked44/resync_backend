"""Compare the current routes to the base commit using the same synthetic records."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]

RUNNER = r'''
import json, sys
from types import SimpleNamespace
from fastapi.testclient import TestClient
import main
fixture = json.loads(sys.stdin.read())
new = fixture["new"]
report = fixture["report"]
run = {"analysis_run_id":"run", "user_id":"owner", "analysis_run_created_at":"2026-01-01T00:00:00+00:00", "analysis_run_status":"completed", "analysis_run_result_json":report, "analysis_run_is_unlocked":fixture["unlocked"]} if new else {"analysis_run_id":"run", "user_id":"owner", "created_at":"2026-01-01T00:00:00+00:00", "status":"completed", "result_json":report}
ledger = {"ledger_id":"entry", "kind":"purchase", "delta":3, "balance_after":3, "analysis_run_id":None, "note":"synthetic", "created_at":"2026-01-01T00:00:00Z"}
if new:
    ledger = {(('credit_ledger_' + k) if k not in ('analysis_run_id',) else k):v for k,v in ledger.items()}
    ledger['credit_ledger_id'] = ledger.pop('credit_ledger_ledger_id')
class Query:
    def __init__(self, name): self.name=name
    def select(self,*a,**k): return self
    def update(self,*a,**k): return self
    def delete(self,*a,**k): return self
    def eq(self,*a,**k): return self
    def order(self,*a,**k): return self
    def limit(self,*a,**k): return self
    def execute(self):
        return SimpleNamespace(data={'analysis_run':[{**run, 'manuscript_id':'m-1'}], 'manuscript':[], 'inconsistency':[], 'recommendation':[], 'citation':[], 'citation_crossmatch_issue':[], 'manuscript_section_cache':[], 'credit_ledger':[ledger], 'credit_wallet':[{'balance':3}], 'get_credit_balance':3}[self.name])
class DB:
    def table(self,name): return Query(name)
    def rpc(self,name,args): return Query(name)
main.DatabasePersistenceService.get_client = lambda: DB()
main.app.dependency_overrides[main.get_authenticated_user_id] = lambda:'owner'
with TestClient(main.app) as client:
    paths = ['/api/scans/run', '/api/scans/run/manuscript', '/api/credits/balance', '/api/credits/history']
    output = []
    for path in paths:
        r = client.get(path,headers={'X-User-Id':'owner'})
        assert r.status_code == 200, (path,r.status_code,r.text)
        output.append(r.content.decode())
    r = client.post('/api/scans/run/discard', headers={'X-User-Id':'owner'})
    assert r.status_code == 200, (r.status_code, r.text)
    output.append(r.content.decode())
print('F43_RESULT=' + json.dumps(output))
'''


def test_saved_scan_contract_matches_base(tmp_path):
    archive = subprocess.run(['git', 'archive', 'fa4d32e', 'main.py', 'services'], cwd=ROOT, check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(tmp_path, filter='data')
    env = os.environ.copy()
    env['PYTHON_DOTENV_DISABLED'] = '1'
    for key in list(env):
        if any(word in key.upper() for word in ('GEMINI', 'SUPABASE', 'MODAL', 'GOOGLE_API', 'AWS')):
            env.pop(key)
    report = {'status':'completed', 'access':{'unlocked':False, 'unlock_cost':1},
              'manuscript_sections':[{'name':'Introduction','text':'First.'}, {'name':'Results','text':'Second.'}],
              'citations':[{'citation_status':'verified_url'}], 'inconsistencies':[], 'recommendations':[],
              'manuscript_flow':[], 'ai_synthesis':{'summary':'A summary.'}}
    for unlocked, partial in ((False, False), (True, False), (False, True)):
        report['access']['unlocked'] = unlocked
        if partial:
            report['ai_synthesis']['review_status'] = {'status':'partial', 'unreviewed':3, 'whole_paper_check':'partial', 'whole_paper_note':'Synthetic note.'}
        outputs = []
        for root, new in ((tmp_path, False), (ROOT, True)):
            fixture = {'report':report, 'unlocked':unlocked, 'new':new}
            result = subprocess.run([sys.executable, '-c', RUNNER], cwd=root, env=env, input=json.dumps(fixture), capture_output=True, text=True, check=True)
            outputs.append(next(line.removeprefix('F43_RESULT=') for line in result.stdout.splitlines() if line.startswith('F43_RESULT=')))
        assert outputs[0] == outputs[1]
