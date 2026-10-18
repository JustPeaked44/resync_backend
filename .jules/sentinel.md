## 2023-10-18 - [CRITICAL] Prevent SSRF via HTTP Redirects
**Vulnerability:** The application was vulnerable to Server-Side Request Forgery (SSRF) when making HTTP requests with `follow_redirects=True`. While the initial URL was checked against internal IPs, redirects could still point to internal resources.
**Learning:** `httpx`'s automatic redirect handling circumvents initial validation. When fetching URLs using an `httpx.AsyncClient` with `follow_redirects=True`, we must validate every hop.
**Prevention:** Apply an event hook (`event_hooks={'request': [_verify_request_host]}`) to the `httpx.AsyncClient` instance, where the hook validates the request host. Centralize this in `services/security.py` and reuse it for all external HTTP requests.
