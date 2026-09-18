## 2024-05-24 - [CRITICAL] Unauthenticated Test Endpoints Enabled by Default
**Vulnerability:** Test/debug endpoints (`/api/test-*`) were enabled by default (`os.getenv("ENABLE_TEST_ENDPOINTS", "true")`). These endpoints accept unauthenticated requests and consume real third-party API quotas (Gemini).
**Learning:** Default-allow configuration combined with missing authentication on test endpoints creates a massive financial DoS risk. The environment variable default prioritized developer convenience over production security.
**Prevention:** Always use secure defaults (`false` or missing). Never expose unauthenticated endpoints that consume paid resources, even in development, without explicit opt-in configuration.
