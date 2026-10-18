
## 2024-10-19 - [Fix SSRF vulnerability bypass via httpx redirects]
**Vulnerability:** A known SSRF bypass existed in `services/citation.py` where a user-provided URL could resolve to an external IP initially but redirect to an internal/private IP, bypassing the `_is_safe_public_host` check since httpx transparently handles redirects.
**Learning:** Checking the host only once before initializing the request is insufficient when automatic redirects are enabled (`follow_redirects=True`).
**Prevention:** Use httpx's `event_hooks={'request': [hook_function]}` to run the security check immediately before every HTTP request is sent over the wire, effectively validating all hops in a redirect chain. The hook should raise an `httpx.ConnectError` (with `request=request`) which inherits from `RequestError` and will be caught gracefully by typical error handling.
