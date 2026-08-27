import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from sidekick.agent.graph import run_agent
from sidekick.filesystem.workspace import WorkspaceError
from sidekick.models.qwen import get_model
from sidekick.utils.utils import expand_file_patterns

from .schemas import ResumeRequest, RunRequest, RunResponse, StatusResponse, LLMRequest

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "api/templates"
STATIC_DIR = BASE_DIR / "api/static"

app = FastAPI(title="SideKick", description="FastAPI port of the Streamlit SideKick app")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)


# Finished sessions are kept for this long (seconds) so a client can still
# poll the final status, then they are auto-expired to avoid a memory leak.
SESSION_TTL_SECONDS = 300
# Hard cap on the number of stored sessions. When exceeded, the oldest
# finished sessions are evicted first.
MAX_SESSIONS = 100


class Session:
    """Holds per-thread run state, mirroring the Streamlit session_state."""

    def __init__(self, thread_id: str):
        self.thread_id = thread_id
        self.status = "running"  # running | waiting_approval | complete | error
        self.logs: List[str] = []
        self.pending_interrupt: Optional[Dict[str, Any]] = None
        self.summary: Optional[str] = None
        self.changed_files: List[str] = []
        self.error: Optional[str] = None
        self.matched_files: List[str] = []
        self.run_input: Optional[Dict[str, Any]] = None
        self.token_usage: Dict[str, int] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0} # <--- Added
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
            token_usage=dict(self.token_usage), # <--- Pass token usage to status
        )


# In-memory store of active/finished runs keyed by thread_id.
_sessions: Dict[str, Session] = {}
_sessions_lock = threading.Lock()


def _get_session(thread_id: str) -> Optional[Session]:
    with _sessions_lock:
        return _sessions.get(thread_id)


def _store_session(session: Session) -> None:
    with _sessions_lock:
        _sessions[session.thread_id] = session
        _evict_sessions_locked()


def _remove_session(thread_id: str) -> bool:
    with _sessions_lock:
        return _sessions.pop(thread_id, None) is not None


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
                session.token_usage = result.get("token_usage", {}) # <--- Update session token usage
                session.pending_interrupt = None
                session.mark_finished()
                print(f"[Thread {session.thread_id}] Token Usage: {session.token_usage}")

    except WorkspaceError as exc:
        with session._lock:
            session.status = "error"
            session.error = f"Workspace validation failed: {exc}"
            session.mark_finished()
    except Exception as exc:  # noqa: BLE001
        with session._lock:
            session.status = "error"
            session.error = f"Agent/model error: {exc}"
            session.mark_finished()


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request=request, name="index.html")


@app.post("/api/run", response_model=RunResponse)
def start_run(payload: RunRequest) -> RunResponse:
    root = payload.root.strip()
    task = payload.task.strip()
    raw_files = [line.strip() for line in payload.files if line.strip()]

    if not root:
        raise HTTPException(status_code=400, detail="Root directory is required.")
    if not raw_files:
        raise HTTPException(status_code=400, detail="At least one file or file pattern is required.")
    if not task:
        raise HTTPException(status_code=400, detail="Task is required.")

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
    session = Session(thread_id)
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
def resume_run_thread(thread_id: str, payload: ResumeRequest) -> RunResponse:
    session = _get_session(thread_id)
    if session is None:
        raise HTTPException(status_code=404, detail="No active run to resume.")
    if session.run_input is None:
        raise HTTPException(status_code=400, detail="No active run to resume.")

    with session._lock:
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
def get_status(thread_id: str, since: int = 0) -> StatusResponse:
    session = _get_session(thread_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Unknown thread_id.")
    with session._lock:
        return session.to_status(since=since)


@app.post("/api/clear/{thread_id}")
def clear_session(thread_id: str) -> Dict[str, str]:
    if not _remove_session(thread_id):
        raise HTTPException(status_code=404, detail="Unknown thread_id.")
    return {"thread_id": thread_id, "status": "cleared"}


@app.post("/api/llm")
def ask_llm(payload: LLMRequest) -> StreamingResponse:
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
