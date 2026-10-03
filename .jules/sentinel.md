## 2024-05-18 - SSRF Vulnerability in httpx redirects
**Vulnerability:** The application was vulnerable to SSRF (Server-Side Request Forgery) because `httpx.AsyncClient` was configured with `follow_redirects=True`, allowing an attacker to submit a valid public URL that redirects to an internal/private address.
**Learning:** `httpx` does not automatically validate redirects against custom security constraints unless explicitly configured via event hooks.
**Prevention:** Always use `event_hooks={'request': [hook_function]}` when using `follow_redirects=True` with `httpx` to validate the host before every request, including redirects.
