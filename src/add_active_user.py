import sqlite3
from datetime import datetime, timezone

from sidekick.api.main import DB_PATH

def _get_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def activate_user(uname: str, db_path: str) :
    """Add a user to the active (whitelisted) users table.

    This is a plain Python function (no HTTP endpoint) so an admin can
    activate a user from the backend, e.g. in a script or REPL:

        from utils.utils import activate_user
        activate_user("jane_doe", "path/to/users.db")

    The username must already exist in the ``users`` table; activating a
    non-existent username raises :class:`ValueError`.

    Returns a small dict describing the result.
    """
    uname = (uname or "").strip()
    if not uname:
        raise ValueError("Username is required.")
    with _get_db(db_path) as conn:
        exists = conn.execute(
            "SELECT 1 FROM users WHERE uname = ?",
            (uname,),
        ).fetchone()
        if exists is None:
            raise ValueError(f"User does not exist: {uname}")
        conn.execute(
            "INSERT OR IGNORE INTO active_users (uname, created_at) VALUES (?, ?)",
            (uname, datetime.now(timezone.utc).isoformat()),
        )
    return {"uname": uname, "status": "activated"}

if __name__ == "__main__":
    activate_user("kamijz", DB_PATH)