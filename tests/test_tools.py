"""Tests for src/sidekick/agent/tools.py."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from sidekick.agent.tools import build_tools
from sidekick.filesystem.workspace import WorkspaceError


@pytest.fixture
def workspace():
    return MagicMock()


@pytest.fixture
def tools(workspace):
    return build_tools(workspace)


def test_build_tools_returns_five_tools(tools):
    assert len(tools) == 5


def test_tool_names(tools):
    assert [t.name for t in tools] == [
        "list_provided_files",
        "read_file",
        "edit_file",
        "search_file",
        "create_file",
    ]


def test_list_provided_files_joins_workspace_listing(tools, workspace):
    workspace.list_files.return_value = ["a.py", "b.py"]
    tool = tools[0]
    assert tool.invoke({}) == "a.py\nb.py"
    workspace.list_files.assert_called_once_with()


def test_read_file_delegates_to_workspace(tools, workspace):
    workspace.read_file.return_value = "content"
    tool = tools[1]
    assert tool.invoke({"path": "a.py"}) == "content"
    workspace.read_file.assert_called_once_with("a.py")


def test_edit_file_success_delegates_to_workspace(tools, workspace):
    workspace.edit_file.return_value = "ok"
    tool = tools[2]
    assert tool.invoke({"path": "a.py", "old": "x", "new": "y"}) == "ok"
    workspace.edit_file.assert_called_once_with("a.py", "x", "y")


def test_edit_file_returns_error_string_on_workspace_error(tools, workspace):
    workspace.edit_file.side_effect = WorkspaceError("boom")
    tool = tools[2]
    result = tool.invoke({"path": "a.py", "old": "x", "new": "y"})
    assert result == "error: boom"


def test_search_file_delegates_to_workspace(tools, workspace):
    workspace.search_file.return_value = "1: match"
    tool = tools[3]
    assert tool.invoke({"path": "a.py", "query": "match"}) == "1: match"
    workspace.search_file.assert_called_once_with("a.py", "match")


def test_create_file_success_delegates_to_workspace(tools, workspace):
    workspace.create_file.return_value = "created"
    tool = tools[4]
    assert tool.invoke({"path": "new.py", "content": "hi"}) == "created"
    workspace.create_file.assert_called_once_with("new.py", "hi")


def test_create_file_returns_error_string_on_workspace_error(tools, workspace):
    workspace.create_file.side_effect = WorkspaceError("denied")
    tool = tools[4]
    result = tool.invoke({"path": "new.py", "content": "hi"})
    assert result == "error: denied"
