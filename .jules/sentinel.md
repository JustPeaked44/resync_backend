
## 2024-03-24 - [SSRF via HTTP Redirects]
**Vulnerability:** The citation service's HTTP reachability check mitigated initial SSRF attempts by checking the target host before calling httpx. However, it was using `follow_redirects=True` without checking subsequent hosts, allowing redirects to bypass the SSRF filter and access internal network addresses.
**Learning:** Checking the initial URL is not sufficient when the HTTP client automatically follows redirects. It's a common oversight where developers accept the risk due to perceived complexity of hooking into redirect handling.
**Prevention:** Always use `event_hooks={'request': [hook_function]}` in `httpx.AsyncClient` to validate the host before *every* request, including redirects.
