# CricketApp

FastAPI backend for cricket scoring, teams, players, tournaments and
leaderboards. Every route is served under both `/v1` and `/api`.

## Local development

Requires **Python 3.12**.

### 1. Create the virtualenv and install dependencies

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. Configure the environment

```powershell
Copy-Item .env.example .env
```

For local work keep `DATABASE_URL=sqlite+aiosqlite:///./cricket.db` and
`STORAGE_BACKEND=local`; those need no external services. Fill in the provider
keys only if you are testing SMS/email/Google sign-in.

### 3. Run the server

```powershell
.\run.ps1
```

or on Linux/macOS:

```bash
./run.sh
```

The standard uvicorn command behind those scripts is:

```powershell
uvicorn app.main:app --reload
```

`uvicorn main:app --reload` also works: `main.py` at the repository root is a
thin re-export of `app.main.app` for convenience. Prefer `app.main:app` — it is
the entrypoint the Docker image uses.

| URL | Purpose |
| --- | --- |
| <http://127.0.0.1:8000> | API root |
| <http://127.0.0.1:8000/docs> | Swagger UI |
| <http://127.0.0.1:8000/redoc> | ReDoc |
| <http://127.0.0.1:8000/health> | Health check |

Useful variants:

```powershell
.\run.ps1 -Port 8001       # different port
.\run.ps1 -NoReload        # disable the file watcher
```

## Important: run from the repository root

The canonical import target is `app.main:app` — the application module is
`app/main.py`. The root `main.py` only re-exports that object so the shorter
`uvicorn main:app` command is valid; it contains no application code.

Starting uvicorn from inside `app/` breaks `from app.…` imports and creates a
second `cricket.db` and `uploads/` folder there.

The database path (`./cricket.db`) and uploads directory (`./uploads`) are
resolved relative to the current working directory, so always launch from the
project root. `run.ps1` / `run.sh` do this for you.

## Tests

```powershell
pytest
```

## Deployment

Production runs in Docker behind Caddy. See [DEPLOY.md](DEPLOY.md).
