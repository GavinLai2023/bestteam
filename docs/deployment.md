# Deploying a per-customer instance

bestteam is deployed as **one independent instance per customer** (not a
multi-tenant SaaS) — see `docs/team_builder_methodology.md`. Each instance
runs the FastAPI backend (with its own SQLite database) and the React
frontend behind Docker Compose.

## 1. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and fill in:

- LLM provider keys (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `TAVILY_API_KEY`
  if using web search).
- `BESTTEAM_SECRET_KEY` — the backend refuses to start (in any environment)
  if this is left at the default value; generate a real one with:
  ```bash
  python -c "import secrets; print(secrets.token_hex(32))"
  ```
- `BESTTEAM_CORS_ORIGINS` — the customer's frontend URL(s), comma-separated,
  no trailing slash.
- `VITE_API_BASE` / `VITE_WS_BASE` — the backend's public URL, as reachable
  from the customer's browser (`https://...` / `wss://...`). These are baked
  into the frontend at build time.

TLS termination (HTTPS/WSS) is assumed to be handled by a reverse proxy or
the hosting platform's load balancer in front of these containers.

## 2. Build and start

```bash
docker compose build
docker compose up -d
```

`docker compose` automatically loads `.env` from the project root to
substitute `${VITE_API_BASE}`/`${VITE_WS_BASE}` in `docker-compose.yml` (this
is separate from the backend's `env_file: .env`), so the values you set in
step 1 are baked into the frontend image at build time.

## 3. Create the first user

There is no public registration UI — create the first login via the API:

```bash
curl -X POST http://localhost:8000/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "<choose-a-strong-password>"}'
```

This returns an `access_token`. Subsequent users (if needed) can be created
the same way.

## 4. Verify

- `curl http://localhost:8000/api/health` → `200 {"status": "ok"}` (public,
  no auth required).
- `curl http://localhost:8000/api/workflows` → `401` (auth required).
- `curl http://localhost:8000/api/workflows -H "Authorization: Bearer <access_token>"`
  → `200`.
- Open the frontend in a browser — you'll be redirected to `/login`. Log in
  with the user created above; you should land on the monitoring page
  (`/`) with a "Log out" link in the nav.

## Known limitation: unauthenticated WebSocket

`/api/runs/{run_id}/stream` is not auth-protected. This is an accepted
limitation for the first pilot: run IDs are unguessable UUIDs, only
obtainable via an authenticated `POST /api/runs` or
`POST /api/builder/sessions/{id}/test-runs`.

## Data persistence

The SQLite database (agents, teams, workflows, users — config persistence,
not run history) lives in the `bestteam_data` named volume, mounted at
`/app/ui/backend/data` in the backend container. It survives
`docker compose restart` / `docker compose down` (without `-v`).
