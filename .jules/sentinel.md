
## 2023-10-23 - SSRF Vulnerability via Redirects in HTTPX
**Vulnerability:** A residual SSRF gap existed in the citation checking logic where `httpx.AsyncClient` was configured with `follow_redirects=True`. The initial URL was validated against internal IP resolution, but malicious external sites could redirect to internal infrastructure (e.g. cloud metadata endpoints at 169.254.169.254) which httpx would follow automatically.
**Learning:** Checking only the initial host is insufficient when following redirects. Modern HTTP clients require per-hop validation when automatically following redirects to prevent attackers from bypassing domain fronting or initial SSRF checks.
**Prevention:** When using `httpx` with `follow_redirects=True`, apply `event_hooks={'request': [hook_function]}` to validate the host before every request, including redirects. For `httpx.AsyncClient`, the hook function can be asynchronous and use `asyncio.to_thread` for synchronous DNS resolution.
