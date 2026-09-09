import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from sidekick.agent.graph import run_agent
from sidekick.filesystem.workspace import WorkspaceError
from sidekick.models.qwen import get_model
from sidekick.utils.utils import (
    MAX_BUFFERED_METRICS,
    UserValidationError,
    expand_file_patterns,
    metric_collector,
    validate_user,
)

from .schemas import (
    LoginRequest,
    LoginResponse,
    LogoutResponse,
    RegisterRequest,
    ResumeRequest,
    RunRequest,
    RunResponse,
    StatusResponse,
    LLMRequest,
)

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "api/templates"
STATIC_DIR = BASE_DIR / "api/static"

app = FastAPI(title="SideKick", description="SideKick agent API")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)


# --- Authentication -------------------------------------------------------
# Users are persisted in a local SQLite database. Passwords are stored hashed
# (SHA-256 with a per-user salt) so the raw password is never kept on disk.

# Override with SIDEKICK_DB_PATH (e.g. in tests or deployments).
DB_PATH = Path(os.getenv("SIDEKICK_DB_PATH", str(BASE_DIR / "api" / "users.db")))
_db_lock = threading.Lock()


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db() -> None:
    with _db_lock, _get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                                                 id TEXT PRIMARY KEY,
                                                 name TEXT NOT NULL,
                                                 uname TEXT NOT NULL UNIQUE,
                                                 salt TEXT NOT NULL,
                                                 upass_hash TEXT NOT NULL,
                                                 folder TEXT NOT NULL
            )
            """
        )
        # Active users are whitelisted by an admin. A user can only log in
        # once their username has been added to this table.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS active_users (
                                                 uname TEXT PRIMARY KEY,
                                                 created_at TEXT NOT NULL
            )
            """
        )


def _hash_password(upass: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{upass}".encode("utf-8")).hexdigest()


def _register_user(name: str, uname: str, upass: str, folder: str) -> Dict[str, Any]:
    """Validate and register a user in SQLite, returning the stored record (no password)."""
    clean = validate_user(name, uname, upass, folder)
    salt = secrets.token_hex(16)
    record = {
        "id": uuid4().hex,
        "name": clean["name"],
        "uname": clean["uname"],
        "salt": salt,
        "upass_hash": _hash_password(clean["upass"], salt),
        "folder": clean["folder"],
    }
    with _db_lock, _get_db() as conn:
        conn.execute(
            "INSERT INTO users (id, name, uname, salt, upass_hash, folder) VALUES (?, ?, ?, ?, ?, ?)",
            (
                record["id"],
                record["name"],
                record["uname"],
                record["salt"],
                record["upass_hash"],
                record["folder"],
            ),
        )
    return record


def _get_user(uname: str) -> Optional[Dict[str, Any]]:
    with _db_lock, _get_db() as conn:
        row = conn.execute(
            "SELECT id, name, uname, salt, upass_hash, folder FROM users WHERE uname = ?",
            (uname,),
        ).fetchone()
    return dict(row) if row is not None else None


def _is_active_user(uname: str) -> bool:
    """Return True if the username has been whitelisted by an admin."""
    with _db_lock, _get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM active_users WHERE uname = ?",
            (uname,),
        ).fetchone()
    return row is not None


def _public_user(record: Dict[str, Any]) -> Dict[str, str]:
    """Return a user record without the password hash or salt."""
    return {
        "id": record["id"],
        "name": record["name"],
        "uname": record["uname"],
        "folder": record["folder"],
    }


# In-memory map of valid Bearer tokens to the public user record they belong
# to. Tokens are bound to a user so every API call can enforce that user's
# folder restriction and thread ownership.
_active_sessions: Dict[str, Dict[str, Any]] = {}
_active_sessions_lock = threading.Lock()


def _bearer_token(request: Request) -> Optional[str]:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[len("Bearer "):].strip()
        return token or None
    return None


def _current_user(request: Request) -> Dict[str, Any]:
    """FastAPI dependency: resolve the Bearer token to a logged-in user or 401."""
    token = _bearer_token(request)
    if token is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    with _active_sessions_lock:
        user = _active_sessions.get(token)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid or expired session token.")
    return user


def _assert_root_allowed(root: str, user: Dict[str, Any]) -> None:
    """Reject a workspace root that lies outside the user's registered folder.

    Every user registers a folder; all roots they run the agent against must be
    that folder or a directory inside it. The check is done on resolved paths so
    ``..`` segments and symlinks cannot be used to escape.
    """
    folder = Path(user["folder"]).resolve()
    try:
        root_path = Path(root).expanduser().resolve(strict=True)
    except (FileNotFoundError, RuntimeError):
        raise HTTPException(status_code=400, detail=f"Root directory does not exist: {root}")
    try:
        root_path.relative_to(folder)
    except ValueError:
        raise HTTPException(
            status_code=403,
            detail=f"Root directory must be inside your registered folder: {folder}",
        )


# Initialize the database and seed a default user so the app is usable out of the box.
_init_db()
try:
    _register_user("Admin", "admin", "admin1234", str(BASE_DIR))
except (UserValidationError, sqlite3.IntegrityError):
    pass
# Ensure the default admin is whitelisted so the app is usable out of the box.
with _db_lock, _get_db() as conn:
    conn.execute(
        "INSERT OR IGNORE INTO active_users (uname, created_at) VALUES (?, ?)",
        ("admin", datetime.now(timezone.utc).isoformat()),
    )


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request=request, name="login.html")


@app.get("/", response_class=HTMLResponse)
def root(request: Request) -> HTMLResponse:
    """Render the login page at the public entry point."""
    return templates.TemplateResponse(request=request, name="login.html")


@app.post("/api/register", response_model=LoginResponse)
def register(payload: RegisterRequest) -> LoginResponse:
    try:
        record = _register_user(payload.name, payload.uname, payload.upass, payload.folder)
    except UserValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="Username already exists.")
    return LoginResponse(**_public_user(record))


@app.post("/api/login", response_model=LoginResponse)
def login(payload: LoginRequest) -> LoginResponse:
    record = _get_user(payload.uname.strip())
    if record is None:
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    if not secrets.compare_digest(
            _hash_password(payload.upass, record["salt"]), record["upass_hash"]
    ):
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    if not _is_active_user(record["uname"]):
        raise HTTPException(
            status_code=403,
            detail="Account is not yet activated. Please wait for an admin to approve your username.",
        )

    token = secrets.token_urlsafe(32)
    public = _public_user(record)
    with _active_sessions_lock:
        _active_sessions[token] = public
    response = dict(public)
    response["token"] = token
    return LoginResponse(**response)


@app.post("/api/logout", response_model=LogoutResponse)
def logout(request: Request) -> LogoutResponse:
    # The session token is carried in the Authorization header (Bearer token).
    token = _bearer_token(request)
    if token is not None:
        with _active_sessions_lock:
            _active_sessions.pop(token, None)
    return LogoutResponse(message="Logged out.")


# Finished sessions are kept for this long (seconds) so a client can still
# poll the final status, then they are auto-expired to avoid a memory leak.
SESSION_TTL_SECONDS = 300
# Hard cap on the number of stored sessions. When exceeded, the oldest
# finished sessions are evicted first.
MAX_SESSIONS = 100


class Session:
    """Holds per-thread run state for a single agent run."""

    def __init__(self, thread_id: str, owner: str = ""):
        self.thread_id = thread_id
        # Username of the user who started the run. Only the owner may poll,
        # resume or clear it.
        self.owner = owner
        self.status = "running"  # running | waiting_approval | complete | error
        self.logs: List[str] = []
        self.pending_interrupt: Optional[Dict[str, Any]] = None
        self.summary: Optional[str] = None
        self.changed_files: List[str] = []
        self.error: Optional[str] = None
        self.matched_files: List[str] = []
        self.run_input: Optional[Dict[str, Any]] = None
        self.token_usage: Dict[str, int] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        self._lock = threading.Lock()
        self.created_at = time.monotonic()
        self.finished_at: Optional[float] = None

    def mark_finished(self) -> None:
        if self.finished_at is None:
            self.finished_at = time.monotonic()

    def is_expired(self, now: Optional[float] = None) -> bool:
        if self.finished_at is None:
            return False
        if now is None:
            now = time.monotonic()
        return (now - self.finished_at) > SESSION_TTL_SECONDS

    def to_status(self, since: int = 0) -> StatusResponse:
        # Only return log lines the client hasn't seen yet (cursor-based).
        # Clamp to 0 so a bad cursor degrades to "send everything" rather
        # than silently dropping lines.
        offset = max(0, min(since, len(self.logs)))
        return StatusResponse(
            status=self.status,
            logs=list(self.logs[offset:]),
            log_offset=offset,
            pending_interrupt=self.pending_interrupt,
            summary=self.summary,
            changed_files=list(self.changed_files),
            error=self.error,
            token_usage=dict(self.token_usage),
        )


# In-memory store of active/finished runs keyed by thread_id.
_sessions: Dict[str, Session] = {}
_sessions_lock = threading.Lock()


def _get_session(thread_id: str) -> Optional[Session]:
    with _sessions_lock:
        return _sessions.get(thread_id)


def _get_owned_session(thread_id: str, user: Dict[str, Any]) -> Session:
    """Return the session if it exists and belongs to ``user``; otherwise 404.

    A 404 (rather than 403) is returned for foreign threads so callers cannot
    probe which thread ids exist.
    """
    session = _get_session(thread_id)
    if session is None or session.owner != user["uname"]:
        raise HTTPException(status_code=404, detail="Unknown thread_id.")
    return session


def _store_session(session: Session) -> None:
    with _sessions_lock:
        _sessions[session.thread_id] = session
        _evict_sessions_locked()


def _remove_session(thread_id: str, owner: Optional[str] = None) -> bool:
    with _sessions_lock:
        session = _sessions.get(thread_id)
        if session is None or (owner is not None and session.owner != owner):
            return False
        del _sessions[thread_id]
        return True


def _evict_sessions_locked() -> None:
    """Drop expired finished sessions, then enforce the size cap.

    Must be called with _sessions_lock held.
    """
    now = time.monotonic()

    # 1) TTL: expire finished sessions that have been idle too long.
    expired = [
        tid for tid, s in _sessions.items()
        if s.finished_at is not None and (now - s.finished_at) > SESSION_TTL_SECONDS
    ]
    for tid in expired:
        del _sessions[tid]

    # 2) Cap: if still over the limit, evict the oldest finished sessions
    #    first; only as a last resort evict the oldest running ones.
    while len(_sessions) > MAX_SESSIONS:
        finished = [
            (s.finished_at, s.created_at, tid)
            for tid, s in _sessions.items()
            if s.finished_at is not None
        ]
        if finished:
            finished.sort()
            del _sessions[finished[0][2]]
            continue
        # No finished sessions left; evict the oldest running session.
        oldest = min(_sessions.items(), key=lambda kv: kv[1].created_at)
        del _sessions[oldest[0]]


def _run_agent_in_background(
        session: Session,
        root: str,
        files: List[str],
        task: str,
        resume_decision: Optional[bool] = None,
) -> None:
    """Run the agent in a worker thread, updating the session as it goes."""

    def show_event(message: str) -> None:
        with session._lock:
            session.logs.append(message)

    def handle_approval(pending: Dict[str, Any]) -> None:
        with session._lock:
            session.pending_interrupt = pending
            session.status = "waiting_approval"

    try:
        result = run_agent(
            root,
            files,
            task,
            thread_id=session.thread_id,
            on_event=show_event,
            on_approval=handle_approval,
            resume_decision=resume_decision,
        )

        with session._lock:
            if result.get("pending_interrupt"):
                session.status = "waiting_approval"
                session.pending_interrupt = result["pending_interrupt"]
            else:
                session.status = "complete"
                session.summary = result.get("summary")
                session.changed_files = list(result.get("changed_files") or [])
                session.token_usage = result.get("token_usage", {})
                session.pending_interrupt = None
                session.mark_finished()
                metric_collector.record(
                    root=root,
                    files=files,
                    task=task,
                    token_usage=session.token_usage,
                    duration_seconds=session.finished_at - session.created_at,
                    status="success",
                    thread_id=session.thread_id,
                    uname=session.owner,
                )

    except WorkspaceError as exc:
        with session._lock:
            session.status = "error"
            session.error = f"Workspace validation failed: {exc}"
            session.mark_finished()
            metric_collector.record(
                root=root,
                files=files,
                task=task,
                token_usage=session.token_usage,
                duration_seconds=session.finished_at - session.created_at,
                status="error",
                error=session.error,
                thread_id=session.thread_id,
            )
    except Exception as exc:  # noqa: BLE001
        with session._lock:
            session.status = "error"
            session.error = f"Agent/model error: {exc}"
            session.mark_finished()
            metric_collector.record(
                root=root,
                files=files,
                task=task,
                token_usage=session.token_usage,
                duration_seconds=session.finished_at - session.created_at,
                status="error",
                error=session.error,
                thread_id=session.thread_id,
            )


@app.get("/app", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request=request, name="index.html")


@app.post("/api/run", response_model=RunResponse)
def start_run(payload: RunRequest, user: Dict[str, Any] = Depends(_current_user)) -> RunResponse:
    root = payload.root.strip()
    task = payload.task.strip()
    raw_files = [line.strip() for line in payload.files if line.strip()]

    if not root:
        raise HTTPException(status_code=400, detail="Root directory is required.")
    if not raw_files:
        raise HTTPException(status_code=400, detail="At least one file or file pattern is required.")
    if not task:
        raise HTTPException(status_code=400, detail="Task is required.")

    _assert_root_allowed(root, user)

    try:
        files = expand_file_patterns(root, raw_files)
    except WorkspaceError as exc:
        raise HTTPException(status_code=400, detail=f"Workspace validation failed: {exc}")

    if not files:
        raise HTTPException(
            status_code=400,
            detail="The provided file paths/patterns did not match any files.",
        )

    thread_id = uuid4().hex
    session = Session(thread_id, owner=user["uname"])
    session.matched_files = list(files)
    session.run_input = {"root": root, "files": files, "task": task, "thread_id": thread_id}
    _store_session(session)

    thread = threading.Thread(
        target=_run_agent_in_background,
        args=(session, root, files, task),
        daemon=True,
    )
    thread.start()

    return RunResponse(
        thread_id=thread_id,
        status=session.status,
        matched_files=list(files),
    )


@app.post("/api/resume/{thread_id}", response_model=RunResponse)
def resume_run_thread(
    thread_id: str,
    payload: ResumeRequest,
    user: Dict[str, Any] = Depends(_current_user),
) -> RunResponse:
    session = _get_owned_session(thread_id, user)

    # Atomically claim the resume so two concurrent /api/resume calls on the
    # same thread cannot both spawn an agent thread. The claim is released
    # once the run finishes (see _run_agent_in_background).
    with session._lock:
        if session.run_input is None:
            raise HTTPException(status_code=400, detail="No active run to resume.")
        if session.status == "running":
            raise HTTPException(status_code=409, detail="Run is already in progress.")
        session.status = "running"
        session.pending_interrupt = None
        run_input = session.run_input

    thread = threading.Thread(
        target=_run_agent_in_background,
        args=(session, run_input["root"], run_input["files"], run_input["task"], payload.decision),
        daemon=True,
    )
    thread.start()

    return RunResponse(
        thread_id=thread_id,
        status=session.status,
        matched_files=list(session.matched_files),
    )


@app.get("/api/status/{thread_id}", response_model=StatusResponse)
def get_status(
    thread_id: str,
    since: int = 0,
    user: Dict[str, Any] = Depends(_current_user),
) -> StatusResponse:
    session = _get_owned_session(thread_id, user)
    with session._lock:
        return session.to_status(since=since)


@app.get("/api/metrics")
def get_metrics(limit: int = 50, user: Dict[str, Any] = Depends(_current_user)) -> Dict[str, Any]:
    """Return the caller's most recent collected metrics, newest first."""
    mine = [
        m for m in metric_collector.get_metrics(limit=MAX_BUFFERED_METRICS)
        if m.get("uname") == user["uname"]
    ]
    return {"metrics": mine[:limit]}


@app.post("/api/clear/{thread_id}")
def clear_session(thread_id: str, user: Dict[str, Any] = Depends(_current_user)) -> Dict[str, str]:
    if not _remove_session(thread_id, owner=user["uname"]):
        raise HTTPException(status_code=404, detail="Unknown thread_id.")
    return {"thread_id": thread_id, "status": "cleared"}


@app.post("/api/llm")
def ask_llm(payload: LLMRequest, user: Dict[str, Any] = Depends(_current_user)) -> StreamingResponse:
    prompt = payload.prompt.strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt is required.")

    messages = []
    if payload.system:
        messages.append(("system", payload.system))
    messages.append(("human", prompt))

    def event_stream():
        usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        try:
            for chunk in get_model().stream(messages):
                content = chunk.content
                if isinstance(content, list):
                    content = "".join(
                        part.get("text", "") for part in content if isinstance(part, dict)
                    )
                if content:
                    yield f"data: {json.dumps({'type': 'token', 'text': content})}\n\n"
                chunk_usage = chunk.usage_metadata or {}
                if chunk_usage:
                    usage = {
                        "input_tokens": chunk_usage.get("input_tokens", usage["input_tokens"]),
                        "output_tokens": chunk_usage.get("output_tokens", usage["output_tokens"]),
                        "total_tokens": chunk_usage.get("total_tokens", usage["total_tokens"]),
                    }
            yield f"data: {json.dumps({'type': 'done', 'token_usage': usage})}\n\n"
        except Exception as exc:  # noqa: BLE001
            yield f"data: {json.dumps({'type': 'error', 'detail': str(exc)})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")