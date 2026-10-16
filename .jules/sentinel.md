## 2024-10-16 - SSRF Protection via HTTPX Event Hooks
**Vulnerability:** HTTPX calls with `follow_redirects=True` did not validate redirect targets, allowing Server-Side Request Forgery (SSRF) into internal networks.
**Learning:** Checking the initial URL is insufficient. `httpx` follows redirects seamlessly, requiring a per-hop hook (`event_hooks={'request': [hook]}`) to validate the target host before each request is issued.
**Prevention:** Centralized SSRF validation into `services/security.py` and enforce `_verify_request_host` event hook on all `httpx.AsyncClient` usages.
