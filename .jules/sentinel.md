## 2025-02-28 - SSRF Vulnerability in httpx Request Redirects
**Vulnerability:** Found Server-Side Request Forgery (SSRF) vulnerabilities in `services/citation.py` and `services/ingestion.py` where `httpx.AsyncClient` was configured with `follow_redirects=True`.
**Learning:** Checking the URL host manually before issuing the request does not prevent SSRF if redirects are followed, because an attacker can return a redirect pointing to an internal IP (like `localhost` or metadata services).
**Prevention:** Using `event_hooks={'request': [_validate_request_host]}` inside `httpx` ensures every redirect is also validated before being fetched.
