import json
import logging
import os
import queue
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from sidekick.filesystem.workspace import WorkspaceError

logger = logging.getLogger(__name__)


class UserValidationError(ValueError):
    """Raised when a user field fails validation."""


# A username must be 3-32 chars of letters, digits, dot, dash or underscore.
USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")
# A password must be at least 8 characters.
MIN_PASSWORD_LENGTH = 8


def validate_username(uname: str) -> str:
    """Validate a username and return it stripped.

    Raises :class:`UserValidationError` if the username is missing or does not
    match the allowed pattern.
    """
    if uname is None:
        raise UserValidationError("Username is required.")
    uname = uname.strip()
    if not uname:
        raise UserValidationError("Username is required.")
    if not USERNAME_RE.match(uname):
        raise UserValidationError(
            "Username must be 3-32 characters using letters, digits, '.', '-' or '_'."
        )
    return uname


def validate_password(upass: str) -> str:
    """Validate a password and return it unchanged.

    Raises :class:`UserValidationError` if the password is missing or too short.
    """
    if upass is None:
        raise UserValidationError("Password is required.")
    if len(upass) < MIN_PASSWORD_LENGTH:
        raise UserValidationError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
        )
    return upass


def validate_folder(folder: str) -> str:
    """Validate a user's root folder and return it as a normalized absolute path.

    The folder must be an absolute path that exists and is a directory. This is
    the root the user is allowed to work in; every file they provide must start
    with this folder.

    Raises :class:`UserValidationError` on any problem.
    """
    if folder is None:
        raise UserValidationError("Folder is required.")
    folder = folder.strip()
    if not folder:
        raise UserValidationError("Folder is required.")
    folder_path = Path(folder).expanduser()
    if not folder_path.is_absolute():
        raise UserValidationError("Folder must be an absolute path.")
    folder_path = folder_path.resolve()
    if not folder_path.exists():
        raise UserValidationError(f"Folder does not exist: {folder}")
    if not folder_path.is_dir():
        raise UserValidationError(f"Folder is not a directory: {folder}")
    return str(folder_path)


def validate_user(name: str, uname: str, upass: str, folder: str) -> Dict[str, str]:
    """Validate all user fields and return a clean dict of the values.

    Raises :class:`UserValidationError` if any field is invalid.
    """
    return {
        "name": (name or "").strip(),
        "uname": validate_username(uname),
        "upass": validate_password(upass),
        "folder": validate_folder(folder),
    }


# Bump when the payload shape changes so downstream consumers can adapt.
METRIC_SCHEMA_VERSION = 1

# Bounded in-memory buffer of the most recent metric payloads, exposed to the
# UI via the "ui" sink. Kept separate from the sink list so the buffer is
# always populated regardless of which sinks are configured.
MAX_BUFFERED_METRICS = 50


class MetricCollector:
    """Collects run metrics and writes them to one or more configured sinks.

    Sinks are selected via the ``METRIC_SINKS`` environment variable, a
    comma-separated list of sink names (e.g. ``"print"`` or ``"print,kafka"``).
    Each sink is a pluggable callable that receives the metric payload, so new
    destinations (db, kafka, ...) can be added without touching callers.

    Metrics are enqueued and flushed by a single background worker thread, so
    ``record()`` never blocks the caller on slow sinks (kafka, db, ...).
    """

    def __init__(
        self,
        sinks: Optional[List[str]] = None,
        queue_size: int = 1000,
        start_worker: bool = True,
    ):
        if sinks is None:
            # Default to printing to the terminal AND surfacing in the UI
            # sidebar. The "ui" sink is always appended below, so the sidebar
            # keeps working even if METRIC_SINKS overrides the default.
            raw = os.environ.get("METRIC_SINKS", "print")
            sinks = [s.strip() for s in raw.split(",") if s.strip()]
        # The UI buffer is always populated so the sidebar can show metrics
        # regardless of which sinks are configured via METRIC_SINKS.
        if "ui" not in sinks:
            sinks.append("ui")
        self._sinks = [self._build_sink(name) for name in sinks]
        self._queue: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=queue_size)
        # Most recent metric payloads, newest first, for the UI.
        self._buffer: List[Dict[str, Any]] = []
        self._buffer_lock = threading.Lock()
        # thread_ids that have been recorded but not yet flushed. A thread_id is
        # only ever recorded once (the resume path reuses the same id and is
        # deduped), so it is safe to drop from this set as soon as its payload
        # has been flushed. This keeps the set bounded by the queue depth rather
        # than growing unboundedly for the lifetime of the process.
        self._seen: set = set()
        self._seen_lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        if start_worker:
            self._worker = threading.Thread(
                target=self._flush_loop, name="metric-collector", daemon=True
            )
            self._worker.start()

    def _build_sink(self, name: str):
        if name == "print":
            return MetricCollector._print_sink
        if name == "kafka":
            return MetricCollector._kafka_sink
        if name == "db":
            return MetricCollector._db_sink
        if name == "ui":
            return self._ui_sink
        raise ValueError(f"Unknown metric sink: {name}")

    @staticmethod
    def _print_sink(payload: Dict[str, Any]) -> None:
        print(f"[metric] {json.dumps(payload, default=str)}")

    def _ui_sink(self, payload: Dict[str, Any]) -> None:
        """Store the payload in the bounded in-memory buffer for the UI."""
        with self._buffer_lock:
            self._buffer.insert(0, payload)
            del self._buffer[MAX_BUFFERED_METRICS:]

    def get_metrics(self, limit: int = MAX_BUFFERED_METRICS) -> List[Dict[str, Any]]:
        """Return a copy of the most recent metric payloads, newest first."""
        with self._buffer_lock:
            return list(self._buffer[:limit])

    @staticmethod
    def _kafka_sink(payload: Dict[str, Any]) -> None:
        # Placeholder: wire up a real Kafka producer here.
        # NOTE: this is NOT a real sink — it only logs. If you configure
        # METRIC_SINKS=kafka expecting real Kafka delivery, you will not get it.
        logger.warning(
            "[metric:kafka] stub sink invoked (no real Kafka producer configured); "
            "payload: %s",
            json.dumps(payload, default=str),
        )

    @staticmethod
    def _db_sink(payload: Dict[str, Any]) -> None:
        # Placeholder: wire up a real DB insert here.
        # NOTE: this is NOT a real sink — it only logs. If you configure
        # METRIC_SINKS=db expecting a real DB write, you will not get it.
        logger.warning(
            "[metric:db] stub sink invoked (no real DB connection configured); "
            "payload: %s",
            json.dumps(payload, default=str),
        )

    def record(
        self,
        root: str,
        files: List[str],
        task: str,
        token_usage: Optional[Dict[str, int]] = None,
        duration_seconds: Optional[float] = None,
        status: str = "success",
        **extra: Any,
    ) -> None:
        """Record a metric. Non-blocking: the payload is queued for the
        background flusher. Duplicate ``thread_id``s are dropped so a resume
        path cannot emit the same metric twice.
        """
        thread_id = extra.get("thread_id")
        if thread_id is not None:
            with self._seen_lock:
                if thread_id in self._seen:
                    logger.debug("[metric] duplicate record for thread_id=%s dropped", thread_id)
                    return
                self._seen.add(thread_id)

        # The flusher removes the thread_id from _seen once this payload has
        # been written to all sinks, so the set does not grow unboundedly.
        payload: Dict[str, Any] = {
            "schema_version": METRIC_SCHEMA_VERSION,
            "root": root,
            "files": list(files),
            "task": task,
            "token_usage": token_usage or {},
            "duration_seconds": duration_seconds,
            "status": status,
            "recorded_at": time.time(),
        }
        payload.update(extra)
        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            logger.error("[metric] queue full; dropping metric for thread_id=%s", thread_id)

    def _flush_loop(self) -> None:
        while True:
            payload = self._queue.get()
            try:
                for sink in self._sinks:
                    try:
                        sink(payload)
                    except Exception:  # noqa: BLE001
                        # A failing sink must never break the run.
                        logger.exception("[metric] sink failed for thread_id=%s", payload.get("thread_id"))
            finally:
                # Release the dedup marker now that the payload has been
                # flushed, so _seen stays bounded by the queue depth.
                thread_id = payload.get("thread_id")
                if thread_id is not None:
                    with self._seen_lock:
                        self._seen.discard(thread_id)
                self._queue.task_done()

    def flush(self, timeout: Optional[float] = None) -> None:
        """Block until all queued metrics have been processed.

        If ``timeout`` is given, give up after that many seconds.
        """
        if timeout is None:
            self._queue.join()
            return
        deadline = time.monotonic() + timeout
        while not self._queue.empty():
            if time.monotonic() > deadline:
                return
            time.sleep(0.01)


# Module-level default collector, configured from the environment.
metric_collector = MetricCollector()


def _is_within_root(path: Path, root_path: Path) -> bool:
    """Return True if ``path`` is ``root_path`` itself or located inside it.

    Uses ``Path.relative_to`` in a try/except so it works on Python < 3.9,
    where ``Path.is_relative_to`` does not exist.
    """
    try:
        path.relative_to(root_path)
    except ValueError:
        return False
    return True


def expand_file_patterns(root: str, entries: list[str]) -> list[str]:
    root_path = Path(root).resolve()
    files = []

    for entry in entries:
        entry = entry.strip()
        if not entry:
            continue

        if "*" in entry:
            # Important: glob relative to the root, not arbitrary absolute paths
            for match in root_path.glob(entry):
                resolved = match.resolve()

                # Prevent symlink / traversal escapes
                if not _is_within_root(resolved, root_path):
                    raise WorkspaceError(f"Path escapes root: {entry}")

                if resolved.is_file():
                    files.append(resolved.relative_to(root_path).as_posix())
        else:
            # Non-glob entries are validated the same way as glob matches:
            # resolve against the root and reject anything that escapes it,
            # so a plain path (possibly absolute or containing "..") cannot
            # bypass the root check.
            resolved = (root_path / entry).resolve()
            if not _is_within_root(resolved, root_path):
                raise WorkspaceError(f"Path escapes root: {entry}")
            if not resolved.is_file():
                raise WorkspaceError(f"File not found: {entry}")
            files.append(resolved.relative_to(root_path).as_posix())

    # Remove duplicates while preserving order
    return list(dict.fromkeys(files))