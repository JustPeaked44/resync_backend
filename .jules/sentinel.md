## 2024-05-24 - SSRF via Redirects in Citation Service
**Vulnerability:** The citation service is vulnerable to SSRF via redirects. The `_is_safe_public_host` check is only applied to the initial URL. `httpx` is configured with `follow_redirects=True`, allowing an attacker to submit a seemingly safe URL that redirects to an internal or reserved IP (like AWS metadata server `169.254.169.254`).
**Learning:** Checking the initial URL for SSRF is insufficient when `follow_redirects=True`. The HTTP client needs to be hooked to check the host on every request, including all redirect hops.
**Prevention:** Use `httpx` event hooks (`event_hooks={'request': [hook]}`) to validate the target host on every request before it goes over the wire.
