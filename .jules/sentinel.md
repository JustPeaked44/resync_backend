## 2024-05-24 - SSRF via HTTP Redirects
**Vulnerability:** Server-Side Request Forgery (SSRF) was possible because the `_is_safe_public_host` check was only applied to the initial URL, allowing malicious servers to redirect the client to internal network addresses.
**Learning:** `httpx` with `follow_redirects=True` automatically follows HTTP 3xx responses without re-validating the target URL against application-level security constraints, bypassing initial URL checks.
**Prevention:** Use `httpx` event hooks (`event_hooks={'request': [hook_function]}`) to intercept and validate the host of *every* request, including subsequent ones triggered by redirects, against internal network restrictions. Wrap blocking socket resolution calls in `asyncio.to_thread`.
