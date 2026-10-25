## 2023-10-27 - [SSRF in HTTPX redirects]
**Vulnerability:** HTTPX client follows redirects, allowing SSRF by redirecting to a local loopback address.
**Learning:** `httpx` redirects are not verified by `_is_safe_public_host` prior to making a request.
**Prevention:** Use an event hook in `httpx` client to verify every request.
