from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

import main
from services.scan_access import locked_scan_view
from tests.test_scan_recovery import FakeDB, row


@pytest.mark.parametrize('route', ['get', 'run', 'upload'])
def test_scan_routes_return_same_display_contract(monkeypatch, route):
    recommendations = [
        {'recommendation_id': str(i), 'id': str(i), 'section': 'A', 'section_a': 'A',
         'section_b': 'B', 'pair_order': i, 'original_sentence': None,
         'recommended_revision': 'Improve', 'recommendation_text': 'Improve',
         'recommendation_priority': 'high'} for i in (1, 2, 3)
    ]
    stored = {
        'status': 'completed', 'analysis_run_id': 'scan', 'user_id': 'owner',
        'manuscript_id': 'ms', 'doc_url': 'doc', 'overall_coherence_score': 0.8,
        'sections_analyzed': ['A', 'B'], 'manuscript_sections': [
            {'name': 'A', 'text': 'first'}, {'name': 'B', 'text': 'second'}],
        'access': {'unlocked': False},
        'manuscript_flow': [
            {'order': i, 'section_a': 'A', 'section_b': 'B', 'severity': 'None',
             'scores': {'overall_coherence': 0.9}, 'culprit_sentences': {}}
            for i in range(1, 5)],
        'inconsistencies_found': 0, 'inconsistencies': [], 'recommendations': recommendations,
        'ai_synthesis': {'summary': 'summary'}, 'citations_audited': 3,
        'citations': [
            {'citation_raw_reference_text': 'One', 'citation_status': 'verified_url',
             'citation_is_accessible': True, 'citation_primary_link': 'https://example.org'},
            {'citation_raw_reference_text': 'Two', 'citation_status': 'broken_link',
             'citation_is_accessible': False, 'citation_primary_link': 'https://example.org'},
            {'citation_raw_reference_text': 'Three', 'citation_status': 'no_link',
             'citation_is_accessible': False}],
        'db_save_status': 'saved', 'notification_dispatched': False,
    }
    original = deepcopy(stored)
    db_row = row('scan', 'completed')
    db_row['result_json'] = stored
    monkeypatch.setattr(main.DatabasePersistenceService, 'get_client', lambda: FakeDB([db_row]))
    async def provision(*args, **kwargs):
        return 'scan', 'ms', 0
    async def pipeline(*args, **kwargs):
        return main.ScanResponse.model_validate(stored)
    monkeypatch.setattr(main, '_provision_analysis_run', provision)
    monkeypatch.setattr(main, '_execute_scan_pipeline', pipeline)
    main.app.dependency_overrides[main.get_authenticated_user_id] = lambda: 'owner'
    try:
        with TestClient(main.app) as client:
            def request():
                if route == 'get':
                    response = client.get('/api/scans/scan')
                elif route == 'run':
                    response = client.post('/api/scans/run', json={'user_id': 'owner'})
                else:
                    response = client.post('/api/scans/upload/run', data={'user_id': 'owner'},
                                           files={'file': ('test.docx', b'fake')})
                assert response.status_code == 200, response.text
                payload = response.json()
                return payload['result'] if route == 'get' else payload

            locked = request()
            assert stored == original
            assert locked['citations'] == []
            assert locked['access']['locked_counts'] == locked_scan_view(original)['access']['locked_counts']
            stored['access']['unlocked'] = True
            unlocked_original = deepcopy(stored)
            unlocked = request()
            assert stored == unlocked_original
            assert locked['display_counts'] == unlocked['display_counts'] == {
                'recommendations': 3, 'all_clear': 1, 'citations': 3}
            assert [c['display_status'] for c in unlocked['citations']] == [
                'accessible', 'inaccessible', 'no_link']
    finally:
        main.app.dependency_overrides.clear()
