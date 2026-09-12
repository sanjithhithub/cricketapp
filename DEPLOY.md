# CricketApp — CI/CD & Deployment Runbook

Pipeline: **GitHub Actions → GHCR → SSH → 2× EC2 (Docker) behind Application Load Balancer**

Every push to `main` runs: `lint` → `test` → `build & push image to GHCR` → `deploy to both EC2 targets`.
Pull requests only run `lint` + `test`.

## Flow

1. `git push origin main`
2. Actions job `lint` (ruff) + `test` (pytest) must pass.
3. `build-and-push` builds `ghcr.io/sanjithhithub/cricketapp` tagged `main` and `sha-<commit>`.
4. `deploy` runs **twice** (a matrix over both ALB targets), each: SCPs `docker-compose.prod.yml` + `deploy.sh`
   into `~/cricketapp` on the instance, then runs `./deploy.sh`:
   - logs in to GHCR
   - `docker compose pull` (new `:main` image)
   - `docker compose up -d` (recreates container `cricketapp`; DB volume `cricketapp_pgdata` is untouched)
   - waits for `http://localhost:80/health` to return OK
   - prunes old images
5. ALB `cricketapp-alb` target group `cricketapp-target` (HTTP port **80**, health path **`/health`**)
   keeps both instances healthy and serves traffic automatically.

Migrations run automatically **inside** the container on start (`docker-entrypoint.sh` runs `alembic upgrade head` before uvicorn).

## GitHub secrets (Settings → Secrets and variables → Actions)

Only **4** are needed (hosts/usernames are hard-coded in the workflow matrix):

| Secret | Value |
|---|---|
| `GHCR_USERNAME` | your GitHub username (`sanjithhithub`) |
| `GHCR_PAT` | GitHub PAT with `read:packages` (instances pull the private image) |
| `SSH_KEY` | contents of `cricuz.pem`, including `BEGIN/END` lines (one key works for both instances) |
| `SSH_PORT` | `22` |

Deploy targets (in `.github/workflows/ci-cd.yml` → `deploy` matrix):

| Instance | Host | User | App dir |
|---|---|---|---|
| ubuntu | `44.200.235.240` | `ubuntu` | `/home/ubuntu/cricketapp` |
| amazon-linux | `32.195.44.211` | `ec2-user` | `/home/ec2-user/cricketapp` |

> The build job uses `GITHUB_TOKEN` (needs `packages: write` — already in the workflow). The instance-side pulls need `GHCR_PAT` because the image is **private** on GHCR. If an instance IP ever changes, update the matrix values in the workflow.

## One-time server layout (already matches the running set-up)

The app lives in `~/cricketapp` on **both** instances (repo clone):

```
~/cricketapp/
├── .env                      # full secrets + DATABASE_URL=...@cricketdb:5432/moviedb
├── firebase-service-account.json
├── docker-compose.prod.yml   # copied each deploy
└── deploy.sh                 # copied each deploy
```

The prod compose keeps the same project name (`cricketapp`) and container names, maps **`80:8000`**
(same as the current running setup; the ALB targets port 80), so your existing `cricketapp_pgdata`
Postgres volume and data are preserved on switch-over.

## Manual deploy (no git push, run on each instance)

```bash
cd ~/cricketapp
echo "$GHCR_PAT" | docker login ghcr.io -u "$GHCR_USERNAME" --password-stdin
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
curl -s http://localhost:80/health
```

## Rollback to a previous version

Every build also pushes a `sha-<commit>` tag. To roll back:

```bash
cd ~/cricketapp
echo "$GHCR_PAT" | docker login ghcr.io -u "$GHCR_USERNAME" --password-stdin
docker pull ghcr.io/sanjithhithub/cricketapp:sha-<previous-commit>
docker compose -f docker-compose.prod.yml up -d --no-deps \
  ghcr.io/sanjithhithub/cricketapp:sha-<previous-commit>
```

Or use Watchtower/labels if you prefer automated rollbacks — out of scope here.

## Troubleshooting

- **Deploy fails at login**: re-check `GHCR_PAT` (classic token, scope `read:packages`) and `GHCR_USERNAME`.
- **Deploy fails at pull/login**: instances must be reachable on port 22 from GitHub runners (matrix uses `SSH_HOST`/`SSH_USER` values in the workflow + `SSH_KEY` secret).
- **Container won't start / `/health` not OK**: check `docker compose -f docker-compose.prod.yml logs cricketapp`; most common cause is missing `.env` vars or `firebase-service-account.json`.
- **Migrated but app 500s**: `docker compose -f docker-compose.prod.yml logs cricketdb` and apply rollback tag.
- **ALB stays unhealthy after new deploy**: confirm the target group health path is `/health` on **port 80** (`cricketapp-target`) and instance SG allows 80 from the LB SG.

## Local checks (same as CI)

```bash
ruff check . && ruff format --check .
pytest
```