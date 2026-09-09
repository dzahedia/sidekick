"""Shared pytest configuration.

Point the API at a throwaway SQLite database *before* ``sidekick.api.main`` is
imported, so tests never touch the real ``users.db`` shipped next to the app.
"""

import os
import tempfile

_TMP_DB_DIR = tempfile.mkdtemp(prefix="sidekick-test-db-")
os.environ.setdefault("SIDEKICK_DB_PATH", os.path.join(_TMP_DB_DIR, "users.db"))
