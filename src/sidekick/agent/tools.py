from __future__ import annotations

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from sidekick.filesystem.workspace import Workspace, WorkspaceError


class PathInput(BaseModel):
    path: str = Field(description="Relative path from list_provided_files().")


class EditInput(PathInput):
    old: str = Field(description="Exact text that must occur exactly once.")
    new: str = Field(description="Replacement text.")


class SearchInput(PathInput):
    query: str = Field(description="Literal text to search for in this file.")


class CreateInput(PathInput):
    content: str = Field(description="Full text content to write to the new file.")


def build_tools(workspace: Workspace):
    """Build request-scoped tools bound directly to one Workspace.

    Important: these functions deliberately do not call Streamlit/UI callbacks.
    LangGraph's ToolNode may execute tools on worker threads, where Streamlit has
    no ScriptRunContext. UI activity is derived from graph events in graph.py.
    """

    def list_provided_files() -> str:
        return "\n".join(workspace.list_files())

    def read_file(path: str) -> str:
        return workspace.read_file(path)

    def edit_file(path: str, old: str, new: str) -> str:
        # Return a structured success/failure signal so callers can tell a
        # successful edit from a failed one without parsing the message text.
        # On failure we return a string (never raise) so the ToolNode can
        # surface the error to the model and the run can continue.
        try:
            return workspace.edit_file(path, old, new)
        except WorkspaceError as exc:
            return f"error: {exc}"

    def search_file(path: str, query: str) -> str:
        return workspace.search_file(path, query)

    def create_file(path: str, content: str) -> str:
        # Same structured signal as edit_file: success returns a plain message,
        # failure returns an "error:"-prefixed message instead of raising.
        try:
            return workspace.create_file(path, content)
        except WorkspaceError as exc:
            return f"error: {exc}"

    return [
        StructuredTool.from_function(
            list_provided_files,
            name="list_provided_files",
            description="List only the relative files explicitly provided for this run.",
        ),
        StructuredTool.from_function(
            read_file,
            name="read_file",
            description="Read one explicitly provided UTF-8 text file.",
            args_schema=PathInput,
        ),
        StructuredTool.from_function(
            edit_file,
            name="edit_file",
            description="Replace exactly one occurrence of old with new in an explicitly provided file.",
            args_schema=EditInput,
        ),
        StructuredTool.from_function(
            search_file,
            name="search_file",
            description="Search literal text inside one explicitly provided file.",
            args_schema=SearchInput,
        ),
        StructuredTool.from_function(
            create_file,
            name="create_file",
            description="Create a new file with the given content. Requires explicit user approval before it runs.",
            args_schema=CreateInput,
        ),
    ]
