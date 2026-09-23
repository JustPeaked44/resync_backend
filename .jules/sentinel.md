## 2024-05-24 - [httpx SSRF via Redirects]
**Vulnerability:** The application was vulnerable to Server-Side Request Forgery (SSRF) when verifying external citations because `httpx.AsyncClient(follow_redirects=True)` only checked the initial host for safety, allowing malicious redirects to bypass the check.
**Learning:** Checking the initial URL host manually is insufficient when following redirects.
**Prevention:** Use `httpx` Event Hooks (`event_hooks={'request': [hook_fn]}`) to intercept and validate the `request.url.host` for every request, ensuring redirects are also vetted before establishing a connection.
