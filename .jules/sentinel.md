## 2025-02-28 - [SSRF bypass via httpx redirects]
**Vulnerability:** `services/citation.py` performs SSRF checks before requests but fails to validate redirect destinations.
**Learning:** `httpx` with `follow_redirects=True` will transparently resolve redirects to arbitrary URLs, including internal IPs (`127.0.0.1`), bypassing pre-request manual checks.
**Prevention:** Use `httpx.AsyncClient(event_hooks={'request': [_ssrf_request_hook]})` to guarantee the check is executed before every HTTP request, including transparent redirects.
