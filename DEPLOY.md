# CricketApp — CI/CD & Deployment Runbook

Pipeline: **GitHub Actions → GHCR → SSH → 1× EC2 (Docker) behind Caddy**

Every push to `main` runs: `lint` → `test` → `build & push image to GHCR` → `deploy to the server`.
Pull requests only run `lint` + `test`.

## Architecture

```
Internet
   │  443 / 80
   ▼
Caddy (host, ports 80+443)   ← auto-issues + renews Let's Encrypt certs
   │  http://127.0.0.1:8000
   ▼
cricketapp container (:8000)  ──►  cricketdb container (Postgres 16, internal only)
```

The app binds **127.0.0.1** only, so TLS cannot be bypassed. Caddy is the single public
entry point. There is no load balancer and no Route 53 — the A record at the domain
registrar points straight at the server's Elastic IP.

## Flow

1. `git push origin main`
2. Actions job `lint` (ruff) + `test` (pytest) must pass.
3. `build-and-push` builds `ghcr.io/sanjithhithub/cricketapp` tagged `main` and `sha-<commit>`.
4. `deploy` SCPs `docker-compose.prod.yml` + `deploy.sh` into the app dir, then runs `./deploy.sh`:
   - logs in to GHCR
   - `docker compose pull` (new `:main` image)
   - `docker compose up -d` (recreates `cricketapp`; volumes `cricketapp_pgdata` and
     `cricketapp_uploads_data` are untouched)
   - waits for `http://127.0.0.1:8000/health` to return OK
   - prunes old images
5. Caddy forwards `yourdomain.com` and `www.yourdomain.com` to the container, no reload needed.

Migrations run automatically **inside** the container on start (`docker-entrypoint.sh` runs
`alembic upgrade head` before uvicorn).

## One-time AWS setup

1. Launch **Ubuntu 24.04** EC2, 20GB gp3, key pair, in a region near your users.
2. **Allocate and associate an Elastic IP.** Without one, stopping the instance changes its
   public IP and the A record stops resolving. Never rely on the auto-assigned IP.
3. Security group inbound rules:

   | Port | Source | Why |
   |---|---|---|
   | 22 | `0.0.0.0/0` | **required for CI/CD** — see below |
   | 80 | `0.0.0.0/0` | HTTP, and the Let's Encrypt HTTP-01 challenge |
   | 443 | `0.0.0.0/0` | HTTPS |

   Outbound: allow all (image pulls, cert issuance, Let's Encrypt).

   **Why 22 is open to the world:** the GitHub Actions runner that deploys this repo
   connects from a random, unannounced IP, so the SSH port cannot be limited to your
   own address without also dropping automated deploys. Access is instead controlled
   by key-only auth. Harden SSH before opening the port:

   ```bash
   sudo sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
   sudo sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
   sudo systemctl restart ssh
   ```

   Confirm the key is the only way in before moving on:
   `sudo grep -E 'PasswordAuthentication|PermitRootLogin' /etc/ssh/sshd_config`.
   If you would rather not expose 22 at all, run a **self-hosted runner** on the
   instance instead and lock the port down to your own IP.

4. DNS at the domain registrar (GoDaddy/Namecheap/etc.) — a plain **A record** is all this
   setup needs, since there is no load balancer in front:

   | Type | Host | Value | TTL |
   |---|---|---|---|
   | A | `@` | the Elastic IP | 300 |
   | A | `www` | the Elastic IP | 300 |

5. Confirm it resolved before continuing: `nslookup yourdomain.com` must return the Elastic IP.

## One-time server setup

```bash
ssh -i cricuz.pem ubuntu@<ELASTIC-IP>

sudo apt update
sudo apt install -y docker.io docker-compose-v2
sudo usermod -aG docker ubuntu && newgrp docker

# Caddy (TLS terminator)
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/caddy-stable-archive-keyring.gpg] https://dl.cloudsmith.io/public/caddy/stable/debian any main" \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy
```

Then copy the two untracked files and the Caddy config up from your machine:

```bash
# from the local repo
scp -i cricuz.pem .env                          ubuntu@<IP>:~/cricketapp/
scp -i cricuz.pem firebase-service-account.json  ubuntu@<IP>:~/cricketapp/
scp -i cricuz.pem Caddyfile                      ubuntu@<IP>:~/cricketapp/
```

Edit `~/cricketapp/Caddyfile`, replace `example.com` with your real domain, then enable it:

```bash
sudo mkdir -p /var/log/caddy && sudo chown caddy:caddy /var/log/caddy
sudo cp ~/cricketapp/Caddyfile /etc/caddy/Caddyfile
sudo systemctl reload caddy
sudo systemctl status caddy
```

The first `reload` is when the certificate is issued, so the A record must already be live.

## First deploy

```bash
cd ~/cricketapp
chmod +x deploy.sh
# The DB must exist with these credentials; compose creates it on first `up`.
./deploy.sh
curl -s http://127.0.0.1:8000/health     # {"status":"ok"}
curl -sI https://yourdomain.com/health   # HTTP/2 200 over TLS
```

## GitHub secrets (Settings → Secrets and variables → Actions)

| Secret | Value |
|---|---|
| `DEPLOY_HOST` | the **Elastic IP** (not the auto-assigned one) |
| `DEPLOY_USER` | `ubuntu` |
| `DEPLOY_DIR` | `/home/ubuntu/cricketapp` |
| `SSH_KEY` | contents of the new `.pem`, **including** the `BEGIN`/`END` lines |
| `SSH_PORT` | `22` |
| `GHCR_USERNAME` | your GitHub username (`sanjithhithub`) |
| `GHCR_PAT` | classic PAT with `read:packages` (the instance pulls a private image) |

The `build-and-push` job uses the automatic `GITHUB_TOKEN` (the workflow grants it
`packages: write`), so no AWS credentials are stored in GitHub at all.

## `.env` notes for a fresh server

- `DATABASE_URL=postgresql+asyncpg://postgres:postgres@cricketdb:5432/moviedb` — `cricketdb` is
  the compose service name, resolved on the internal Docker network. Correct as-is.
- `CORS_ORIGINS` — **must** be set to your web client's real origin. Left unset it falls back to
  `localhost:5173` and every browser request will be blocked.
- `JWT_SECRET_KEY` — keep it stable and out of git. Changing it invalidates all issued tokens.
- If you later move Postgres to RDS, change only `DATABASE_URL` to the RDS endpoint and add its
  security group to the DB's allowlist. The app code does not care where the DB lives.

## Manual deploy (no git push)

```bash
cd ~/cricketapp
echo "$GHCR_PAT" | docker login ghcr.io -u "$GHCR_USERNAME" --password-stdin
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
curl -s http://127.0.0.1:8000/health
```

## Rollback to a previous version

Every build also pushes a `sha-<commit>` tag:

```bash
cd ~/cricketapp
docker pull ghcr.io/sanjithhithub/cricketapp:sha-<previous-commit>
docker compose -f docker-compose.prod.yml up -d
```

If the bad release included a migration, roll the schema back too:

```bash
docker compose -f docker-compose.prod.yml run --rm cricketapp alembic downgrade -1
```

## Troubleshooting

- **Caddy won't issue a cert**: the A record isn't resolving to this server yet, or port 80 is
  blocked in the security group. Check `sudo journalctl -u caddy -n 50` for the exact reason.
- **`/health` returns 502 through Caddy but works on `127.0.0.1:8000`**: the container is down.
  `docker compose -f docker-compose.prod.yml logs cricketapp`.
- **App exits at startup**: almost always `.env` or `firebase-service-account.json` missing.
- **Migrations fail**: `docker compose -f docker-compose.prod.yml logs cricketapp`. The entrypoint
  refuses to adopt a pre-existing untracked schema rather than guess — that error is intentional.
- **502 on uploads**: raise `request_body.max_size` in the Caddyfile, then `sudo systemctl reload caddy`.
- **Out of disk**: `docker system prune -a` reclaims old images and stopped containers.

## Backups

Postgres data lives in the `cricketapp_pgdata` Docker volume on the instance itself. If the box
is lost, the data is lost. Schedule a nightly dump off-box:

```bash
docker exec cricketdb pg_dump -U postgres moviedb | gzip > /var/backups/moviedb-$(date +%F).sql.gz
```

## Local checks (same as CI)

```bash
ruff check . && ruff format --check .
pytest
```
