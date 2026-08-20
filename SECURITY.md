# Security Policy

## Supported branch

Security fixes are applied to the current default branch. Do not include passwords, Icecast source credentials, JWTs, private media, database files, or personally identifying logs in public issues.

## Reporting

Report a suspected vulnerability privately to the repository owner through GitHub's private vulnerability reporting feature. Include affected version, reproduction steps, impact, and a minimal redacted log when useful.

## Deployment baseline

- Keep local desktop installations bound to loopback (`127.0.0.1`).
- For remote access, terminate TLS at a trusted reverse proxy, enable HTTPS/WSS, set `PUBLIC_BASE_URL`, set explicit `CORS_ORIGINS`, and enable `TRUST_PROXY_HEADERS` only when the proxy overwrites forwarded headers.
- Set a unique random `JWT_SECRET_KEY` for managed deployments. Without one, the app generates and persists a private random `.jwt-secret` beside the database.
- Change the bootstrap administrator password immediately in the deterministic wall.
- Give operators only the permissions and station assignments they need.
- Back up the runtime database separately from this source repository and protect the backup as a credential-bearing asset.
