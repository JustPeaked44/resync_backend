## 2025-02-14 - Prevent SSRF via redirect chaining in httpx
**Vulnerability:** Server-Side Request Forgery (SSRF) bypass through HTTP redirects
**Learning:** The application correctly validated the host on initial URL submission but failed to validate the host on subsequent requests when following HTTP redirects with `follow_redirects=True`.
**Prevention:** Always validate all network hops. When using httpx to follow redirects to user-provided URLs, apply an asynchronous per-request event hook (like `event_hooks={'request': [verify_hook]}`) to execute SSRF safeguards before every single request in the chain.
