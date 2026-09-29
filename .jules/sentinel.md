## 2024-05-18 - Prevent SSRF in httpx Redirects
**Vulnerability:** The application was vulnerable to SSRF through HTTP redirects because the host validation was only performed on the initial URL, and not on subsequent redirects when using `httpx.AsyncClient(follow_redirects=True)`.
**Learning:** `httpx` automatically follows redirects, but does not re-validate the host on each hop unless explicitly configured.
**Prevention:** Always use `event_hooks={"request": [ssrf_hook]}` on `httpx` clients that follow redirects to validate the host before every request, including redirects.
