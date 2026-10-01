## 2025-01-01 - Fix SSRF in HTTPX Redirects
**Vulnerability:** The SSRF guard (`_is_safe_public_host`) was only applied to the initial URL. Redirects were automatically followed by `httpx` to any internal/private address.
**Learning:** `httpx`'s `follow_redirects=True` does not re-validate the target URL automatically, making it susceptible to SSRF via redirects.
**Prevention:** Use `httpx`'s `event_hooks={'request': [hook_function]}` to validate the host before every request, including those triggered by redirects. Remember to include the `request=request` parameter when raising `httpx.ConnectError`.
