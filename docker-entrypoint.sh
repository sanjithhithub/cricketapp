#!/bin/sh
set -e

echo "Waiting for database to be ready..."
python - <<'PY'
import os
import time

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

url = os.environ["DATABASE_URL"].replace("+asyncpg", "+psycopg2")
engine = create_engine(url)

for _ in range(60):
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        break
    except OperationalError:
        time.sleep(2)
else:
    raise SystemExit("ERROR: database not reachable after 120s")
PY

echo "Checking database migration state..."
set +e
python - <<'PY'
import os
import sys

from sqlalchemy import create_engine, inspect

import app.auth.models  # noqa: F401
import app.levels.models  # noqa: F401
import app.matches.models  # noqa: F401
import app.models  # noqa: F401
import app.players.models  # noqa: F401
import app.scoring.models  # noqa: F401
import app.teams.models  # noqa: F401
from app.database import Base

url = os.environ["DATABASE_URL"].replace("+asyncpg", "+psycopg2")
engine = create_engine(url)
insp = inspect(engine)
tables = set(insp.get_table_names())

# Fresh DB or a DB already tracked by Alembic -> normal upgrade path.
if not tables or "alembic_version" in tables:
    sys.exit(0)

# DB has tables but was never tracked by Alembic (old deployments created the
# schema via Base.metadata.create_all). Adopt it only if it fully matches the
# current models; otherwise tell the operator to rebuild it.
wide_columns = {
    "batting_hand": 10,
    "bowling_hand": 10,
    "batting_position": 20,
    "bowling_type": 20,
}
required_tables = {t.name: {c.name for c in t.columns} for t in Base.metadata.sorted_tables}
missing_tables = sorted(set(required_tables) - tables)
adopt = True
problems = []

for table, columns in required_tables.items():
    if table not in tables:
        continue
    actual = {c["name"] for c in insp.get_columns(table)}
    missing_cols = sorted(columns - actual)
    if missing_cols:
        problems.append(f"{table} missing columns: {missing_cols}")
    if table == "players":
        for col in insp.get_columns("players"):
            if col["name"] in wide_columns:
                limit = None
                type_str = str(col["type"])
                if "VARCHAR" in type_str:
                    limit = int(
                        type_str.split("(")[-1].rstrip(")") if type_str.endswith(")") else 0
                    )
                if limit is not None and limit < wide_columns[col["name"]]:
                    problems.append(
                        f"players.{col['name']} is {type_str}, expected VARCHAR({wide_columns[col['name']]})"
                    )

if missing_tables or problems:
    print(
        "ERROR: pre-existing schema is not tracked by Alembic and does not match\n"
        "the current models. The container cannot safely adopt it:\n"
        + "  - " + "\n  - ".join(missing_tables + problems)
        + "\n\nReset the database volume, then redeploy:\n"
        "  docker compose -f docker-compose.prod.yml down\n"
        "  docker volume rm cricketapp_pgdata\n"
        "  docker compose -f docker-compose.prod.yml up -d\n",
        file=sys.stderr,
    )
    sys.exit(3)

print("INFO: adopting existing schema (stamp at head), then applying any pending migrations.")
sys.exit(2)
PY
DB_STATE=$?
set -e

if [ "$DB_STATE" -eq 3 ]; then
    echo "ERROR: schema adoption aborted." >&2
    exit 1
fi

if [ "$DB_STATE" -eq 2 ]; then
    echo "Stamping existing schema at head..."
    alembic stamp head
fi

echo "Running database migrations..."
alembic upgrade head

echo "Starting CricketApp..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000