# CricketApp — CI/CD & Deployment Runbook

Pipeline: **GitHub Actions → GHCR → SSH → EC2 (Docker + ALB)**

Every push to `main` runs: `lint` → `test` → `build & push image to GHCR` → `deploy to EC2`.
Pull requests only run `lint` + `test`.

## Flow

1. `git push origin main`
2. Actions job `lint` (ruff) + `test` (pytest) must pass.
3. `build-and-push` builds `ghcr.io/sanjithhithub/cricketapp` tagged `main` and `sha-<commit>`.
4. `deploy` SCPs `docker-compose.prod.yml` + `deploy.sh` to `/opt/cricketapp`, then runs `./deploy.sh`:
   - logs in to GHCR
   - `docker compose pull` (new `:main` image)
   - `docker compose up -d` (recreates container `cricketapp`; DB volume `cricketapp_pgdata` is untouched)
   - waits for `http://localhost:8000/health` to return OK
   - prunes old images
5. ALB target group health check (`:8000/health`) flips the new container healthy and traffic flows.

Migrations run automatically **inside** the container on start (`docker-entrypoint.sh` runs `alembic upgrade head` before uvicorn).

## GitHub secrets (Settings → Secrets and variables → Actions)

| Secret | Value |
|---|---|
| `GHCR_USERNAME` | your GitHub username (`sanjithhithub`) |
| `GHCR_PAT` | GitHub PAT with `read:packages` (server pulls the private image) |
| `SSH_HOST` | EC2 public IP or DNS |
| `SSH_PORT` | `22` |
| `SSH_USER` | SSH user (e.g. `ubuntu`) |
| `SSH_KEY` | contents of `cricuz.pem`, including `BEGIN/END` lines |

> The build job uses `GITHUB_TOKEN` (needs `packages: write` — already in the workflow). The server-side pull needs `GHCR_PAT` because the image is **private** on GHCR.

## One-time server layout (already matches current set-up)

```
/opt/cricketapp/
├── .env                      # full secrets (from .env.example) + DATABASE_URL=...@cricketdb:5432/moviedb
├── firebase-service-account.json
├── docker-compose.prod.yml   # copied each deploy
└── deploy.sh                 # copied each deploy
```

The prod compose keeps the same project name (`cricketapp`) and container names, so your existing
`cricketapp_pgdata` Postgres volume and data are preserved on switch-over.

## Manual deploy (no git push)

```bash
cd /opt/cricketapp
echo "$GHCR_PAT" | docker login ghcr.io -u "$GHCR_USERNAME" --password-stdin
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
curl -s http://localhost:8000/health
```

## Rollback to a previous version

Every build also pushes a `sha-<commit>` tag. To roll back:

```bash
cd /opt/cricketapp
echo "$GHCR_PAT" | docker login ghcr.io -u "$GHCR_USERNAME" --password-stdin
docker pull ghcr.io/sanjithhithub/cricketapp:sha-<previous-commit>
docker compose -f docker-compose.prod.yml up -d --no-deps \
  ghcr.io/sanjithhithub/cricketapp:sha-<previous-commit>
```

Or use Watchtower/labels if you prefer automated rollbacks — out of scope here.

## Troubleshooting

- **Deploy fails at login**: re-check `GHCR_PAT` (classic token, scope `read:packages`) and `GHCR_USERNAME`.
- **Deploy fails at pull/login**: EC2 must be reachable on port 22 from GitHub runners (`SSH_HOST`/`SSH_USER`/`SSH_KEY`).
- **Container won't start / `/health` not OK**: check `docker compose -f docker-compose.prod.yml logs cricketapp`; most common cause is missing `.env` vars or `firebase-service-account.json`.
- **Migrated but app 500s**: `docker compose -f docker-compose.prod.yml logs cricketdb` and apply rollback tag.
- **ALB stays unhealthy after new deploy**: confirm the target group health path is `/health` on **port 8000** and instance SG allows 8000 from the LB SG.

## Local checks (same as CI)

```bash
ruff check . && ruff format --check .
pytest
```