import os
import tempfile

_test_db = os.path.join(tempfile.mkdtemp(prefix="cricket_test_"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_test_db}"
