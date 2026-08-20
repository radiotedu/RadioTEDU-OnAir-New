# Radio Mode Phase 4A Hardening Design

Phase 4A is intentionally implemented as a deploy-hardening slice, not a transport rewrite.

- `PUBLIC_BASE_URL`, `CORS_ORIGINS`, `TRUST_PROXY_HEADERS`, and `SECURITY_HEADERS_ENABLED` are the deployment-facing knobs used by the app.
- The browser still uses the existing authenticated WebSocket path for live updates and mic control.
- The current remote mic path remains WebSocket plus `MediaRecorder`; WebRTC and TURN/STUN are still deferred.
- The PWA shell is intentionally conservative: shell assets can be cached, but authenticated API responses are not cached.
- The mobile polish is intentionally bounded to ergonomics on the existing operator shell, not a layout redesign.

### 3a. Deferred Future-Hardening Items

- `SESSION_COOKIE_SECURE` or an equivalent secure-cookie flag
- `Permissions-Policy` for a limited browser surface
