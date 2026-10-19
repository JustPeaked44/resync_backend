## 2024-05-24 - [SSRF via httpx Redirects]
**Vulnerability:** httpx AsyncClient with follow_redirects=True allows SSRF via redirects even if initial URL is checked.
**Learning:** Checking the initial URL before fetching is insufficient because malicious servers can redirect the request to an internal network address (e.g. cloud metadata endpoint).
**Prevention:** Use httpx event_hooks={'request': [hook_function]} to validate the host before every request, including all redirect hops.
