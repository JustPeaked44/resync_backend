## 2024-10-14 - SSRF vulnerability in httpx redirect handling
**Vulnerability:** Found an SSRF vulnerability where redirects were not being checked for safe public hosts in `httpx` async clients, specifically in ingestion and citation fetching services.
**Learning:** Checking the original requested URL for SSRF is insufficient if `follow_redirects=True`, because the server could redirect the client to an internal IP (like 127.0.0.1 or 169.254.169.254) on subsequent requests.
**Prevention:** Always apply `event_hooks={'request': [_verify_request_host]}` on `httpx.AsyncClient` when fetching URLs provided by users to ensure every redirect hop is validated against safe internal subnets.
