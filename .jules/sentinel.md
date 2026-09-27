## 2024-05-15 - SSRF via `httpx` redirects
**Vulnerability:** `httpx` with `follow_redirects=True` will happily redirect to private/internal IP addresses, even if the initial URL is validated, unless `event_hooks` are used to check every request.
**Learning:** Checking only the initial host is insufficient for SSRF protection when redirects are enabled.
**Prevention:** Use `event_hooks={'request': [hook_function]}` when instantiating the `httpx.AsyncClient` to validate the host before every request, including redirects.
