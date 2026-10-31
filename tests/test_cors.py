from fastapi.testclient import TestClient

import main


def test_preflight_is_cached_for_ten_minutes():
    response = TestClient(main.app).options("/api/scans/start", headers={
        "Origin": "https://resync.vercel.app",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization",
    })
    assert response.status_code == 200
    assert response.headers["access-control-max-age"] == "600"
