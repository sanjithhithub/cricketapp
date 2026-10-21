import os
import tempfile

_test_db = os.path.join(tempfile.mkdtemp(prefix="cricket_test_"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_test_db}"

# app/__init__.py runs load_dotenv(), which does not override variables already
# set here. Pinning the S3 backend keeps the suite deterministic no matter what
# the developer's .env says (local .env sets STORAGE_BACKEND=local). storage.py
# reads this per call, so a test can still monkeypatch.setenv its own value.
os.environ["STORAGE_BACKEND"] = "s3"
