## 2024-05-18 - SSRF vulnerability due to HTTP redirect handling
**Vulnerability:** A Server-Side Request Forgery (SSRF) vulnerability was present because the initial URL was checked for safety, but `httpx` followed redirects to internal IP addresses automatically.
**Learning:** Checking the host before a request doesn't protect against redirects to local networks if `follow_redirects=True` is enabled, as the initial check only covers the primary URL, missing subsequent ones. To validate each redirect step with `httpx`, a request event hook must be added.
**Prevention:** When using `httpx` with `follow_redirects=True`, use `event_hooks={'request': [hook_function]}` to validate the host before *every* request (including redirects).
