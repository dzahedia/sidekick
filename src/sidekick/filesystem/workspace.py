from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class WorkspaceError(ValueError):
    """Raised when a requested filesystem operation violates the workspace boundary."""


@dataclass(frozen=True)
class AllowedFile:
    relative: str
    resolved: Path


class Workspace:
    """Request-scoped, allowlisted view of user-selected files."""

    def __init__(self, root: str | Path, relative_paths: list[str]):
        raw_root = Path(root).expanduser()
        if not raw_root.exists() or not raw_root.is_dir():
            raise WorkspaceError(f"Invalid root directory: {root}")
        self.root = raw_root.resolve(strict=True)

        if not relative_paths:
            raise WorkspaceError("Provide at least one file.")
        if len(relative_paths) != len(set(relative_paths)):
            raise WorkspaceError("Duplicate file paths are not allowed.")

        allowed: dict[str, Path] = {}
        for raw in relative_paths:
            rel = self._normalize_relative(raw)
            candidate = self.root / rel
            try:
                resolved = candidate.resolve(strict=True)
            except FileNotFoundError as exc:
                raise WorkspaceError(f"Provided file does not exist: {rel}") from exc
            self._assert_within_root(resolved, rel)
            if not resolved.is_file():
                raise WorkspaceError(f"Provided path is not a file: {rel}")
            # Prevent aliases (including symlinks) from giving two names to one file.
            if resolved in allowed.values():
                raise WorkspaceError(f"Duplicate resolved file: {rel}")
            allowed[rel] = resolved

        self._allowed = allowed

    @staticmethod
    def _normalize_relative(raw: str) -> str:
        if not isinstance(raw, str) or not raw.strip():
            raise WorkspaceError("File paths must be non-empty relative paths.")
        p = Path(raw.strip())
        if p.is_absolute():
            raise WorkspaceError(f"Absolute paths are not allowed: {raw}")
        if ".." in p.parts:
            raise WorkspaceError(f"Parent traversal is not allowed: {raw}")
        normalized = p.as_posix()
        if normalized in ("", "."):
            raise WorkspaceError(f"Invalid file path: {raw}")
        return normalized

    def _assert_within_root(self, resolved: Path, display: str) -> None:
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise WorkspaceError(f"Path escapes root: {display}") from exc

    def list_files(self) -> list[str]:
        return list(self._allowed.keys())

    def _get(self, path: str) -> Path:
        rel = self._normalize_relative(path)
        resolved = self._allowed.get(rel)
        if resolved is None:
            raise WorkspaceError(f"File was not provided for this run: {rel}")
        # Re-resolve on every access so a post-validation symlink swap cannot escape.
        try:
            current = (self.root / rel).resolve(strict=True)
        except FileNotFoundError as exc:
            raise WorkspaceError(f"Provided file no longer exists: {rel}") from exc
        self._assert_within_root(current, rel)
        if current != resolved or not current.is_file():
            raise WorkspaceError(f"Provided file changed identity since validation: {rel}")
        return current

    def read_file(self, path: str) -> str:
        target = self._get(path)
        try:
            return target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise WorkspaceError(f"File is not valid UTF-8: {path}") from exc

    def edit_file(self, path: str, old: str, new: str) -> str:
        if old == "":
            raise WorkspaceError("old must not be empty.")
        target = self._get(path)
        text = self.read_file(path)
        count = text.count(old)
        if count == 0:
            raise WorkspaceError("Edit rejected: old text was not found.")
        if count != 1:
            raise WorkspaceError(f"Edit rejected: old text occurs {count} times; it must occur exactly once.")
        updated = text.replace(old, new, 1)
        target.write_text(updated, encoding="utf-8")
        return f"Edited {path} successfully."

    def search_file(self, path: str, query: str) -> str:
        if not query:
            raise WorkspaceError("query must not be empty.")
        lines = self.read_file(path).splitlines()
        hits = [f"{i}: {line}" for i, line in enumerate(lines, 1) if query in line]
        return "\n".join(hits) if hits else "No matches."

    def create_file(self, path: str, content: str) -> str:
        rel = self._normalize_relative(path)
        target = self.root / rel
        self._assert_within_root(target, rel)

        if target.exists():
            raise WorkspaceError(f"File already exists: {rel}")

        target.write_text(content, encoding="utf-8")
        self._allowed[rel] = target.resolve(strict=True)
        return f"Created file {rel} successfully."
