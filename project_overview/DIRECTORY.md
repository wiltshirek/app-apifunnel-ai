# project_overview/

Reference docs for AI agents working in this repo.

| File | Contents |
|------|----------|
| [AUTH_IDENTITY_CONTRACT.md](AUTH_IDENTITY_CONTRACT.md) | **Key design principle:** dual-mode auth (admin key + explicit params vs JWT). Read this first. |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Services, routing table, database layout, bridge integration, S3, gotchas |
| [OPERATIONS.md](OPERATIONS.md) | Deploy, Caddy, PM2, GitHub secrets, PR Bot gotchas, test token minting |
| [PRBOT_SERVICE.md](PRBOT_SERVICE.md) | PR Bot service: architecture, routes, auth, deploy, env vars |
| [ADDING_NEW_SERVICES.md](ADDING_NEW_SERVICES.md) | Step-by-step guide: how to add a new API server to the platform |
| [TESTING.md](TESTING.md) | How to run the endpoint test harnesses for each service |
