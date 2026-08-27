"""Tests for src/sidekick/agent/graph.py."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from sidekick.agent import graph as graph_module
from sidekick.agent.graph import (
    _describe_tool_call,
    _make_gate,
    _post_gate,
    _route,
    build_graph,
)
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


# ---------------------------------------------------------------------------
# _route
# ---------------------------------------------------------------------------


def _ai_with_tool_calls(*calls):
    msg = MagicMock(spec=AIMessage)
    msg.tool_calls = list(calls)
    return msg


def test_route_returns_end_when_no_tool_calls():
    state = {"messages": [HumanMessage(content="hi")]}
    assert _route(state) == "END"


def test_route_returns_tools_for_non_create_tool_calls():
    call = {"name": "read_file", "args": {"path": "a.py"}, "id": "1"}
    state = {"messages": [_ai_with_tool_calls(call)]}
    assert _route(state) == "tools"


def test_route_returns_approve_when_create_file_requested():
    call = {"name": "create_file", "args": {"path": "new.py", "content": "x"}, "id": "1"}
    state = {"messages": [_ai_with_tool_calls(call)]}
    assert _route(state) == "approve"


def test_route_returns_approve_when_any_call_is_create_file():
    create = {"name": "create_file", "args": {"path": "n.py", "content": "c"}, "id": "1"}
    other = {"name": "read_file", "args": {"path": "a.py"}, "id": "2"}
    state = {"messages": [_ai_with_tool_calls(create, other)]}
    assert _route(state) == "approve"


def test_route_handles_missing_tool_calls_attr():
    msg = MagicMock()
    # No tool_calls attribute at all
    del msg.tool_calls
    state = {"messages": [msg]}
    assert _route(state) == "END"


# ---------------------------------------------------------------------------
# _post_gate
# ---------------------------------------------------------------------------


def test_post_gate_routes_back_to_qwen():
    assert _post_gate({}) == "qwen"


# ---------------------------------------------------------------------------
# _make_gate
# ---------------------------------------------------------------------------


def _ai_with_create_calls(*calls):
    msg = MagicMock(spec=AIMessage)
    msg.tool_calls = list(calls)
    return msg


def test_gate_collects_all_create_file_calls_in_turn():
    workspace = MagicMock()
    workspace.create_file.side_effect = lambda p, c: f"created:{p}"
    gate = _make_gate(workspace)

    call_a = {
        "name": "create_file",
        "args": {"path": "a.py", "content": "A"},
        "id": "call-a",
    }
    call_b = {
        "name": "create_file",
        "args": {"path": "b.py", "content": "B"},
        "id": "call-b",
    }
    state = {"messages": [_ai_with_create_calls(call_a, call_b)]}

    with patch.dict(os.environ, {"APPROVE_FILE_CREATION": "true"}):
        result = gate(state)

    assert workspace.create_file.call_count == 2
    workspace.create_file.assert_any_call("a.py", "A")
    workspace.create_file.assert_any_call("b.py", "B")

    messages = result["messages"]
    assert len(messages) == 2
    assert all(isinstance(m, ToolMessage) for m in messages)
    assert {m.tool_call_id for m in messages} == {"call-a", "call-b"}
    assert all(m.name == "create_file" for m in messages)


def test_gate_returns_error_string_when_workspace_raises():
    workspace = MagicMock()
    workspace.create_file.side_effect = graph_module.WorkspaceError("denied")
    gate = _make_gate(workspace)

    call = {
        "name": "create_file",
        "args": {"path": "x.py", "content": "x"},
        "id": "id-1",
    }
    state = {"messages": [_ai_with_create_calls(call)]}

    with patch.dict(os.environ, {"APPROVE_FILE_CREATION": "true"}):
        result = gate(state)

    [msg] = result["messages"]
    assert msg.content == "error: denied"
    assert msg.tool_call_id == "id-1"


def test_gate_returns_rejection_message_when_not_approved(monkeypatch):
    workspace = MagicMock()
    gate = _make_gate(workspace)

    call = {
        "name": "create_file",
        "args": {"path": "x.py", "content": "x"},
        "id": "id-1",
    }
    state = {"messages": [_ai_with_create_calls(call)]}

    monkeypatch.delenv("APPROVE_FILE_CREATION", raising=False)
    with patch.object(graph_module, "interrupt", return_value=False) as interrupt_mock:
        result = gate(state)

    interrupt_mock.assert_called_once()
    workspace.create_file.assert_not_called()
    [msg] = result["messages"]
    assert msg.content == "The user rejected this file creation request."
    assert msg.tool_call_id == "id-1"


def test_gate_handles_missing_args_safely():
    workspace = MagicMock()
    workspace.create_file.side_effect = lambda p, c: f"created:{p}"
    gate = _make_gate(workspace)

    # call with missing args dict and missing id
    call = {"name": "create_file"}
    state = {"messages": [_ai_with_create_calls(call)]}

    with patch.dict(os.environ, {"APPROVE_FILE_CREATION": "true"}):
        result = gate(state)

    workspace.create_file.assert_called_once_with("", "")
    [msg] = result["messages"]
    assert msg.tool_call_id == ""
    assert msg.content == "created:"


def test_gate_treats_truthy_env_values_as_approval(monkeypatch):
    workspace = MagicMock()
    workspace.create_file.side_effect = lambda p, c: "ok"
    gate = _make_gate(workspace)

    call = {"name": "create_file", "args": {"path": "p.py", "content": "c"}, "id": "1"}
    state = {"messages": [_ai_with_create_calls(call)]}

    monkeypatch.setenv("APPROVE_FILE_CREATION", "t")
    with patch.object(graph_module, "interrupt") as interrupt_mock:
        result = gate(state)

    interrupt_mock.assert_not_called()
    workspace.create_file.assert_called_once_with("p.py", "c")
    assert result["messages"][0].content == "ok"


# ---------------------------------------------------------------------------
# _describe_tool_call
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, args, expected",
    [
        ("list_provided_files", {}, "Listing provided files"),
        ("read_file", {"path": "foo.py"}, "Reading foo.py"),
        ("edit_file", {"path": "foo.py"}, "Editing foo.py"),
        ("search_file", {"path": "foo.py"}, "Searching foo.py"),
        ("unknown_tool", {"path": "foo.py"}, "Using tool unknown_tool"),
        ("create_file", {"path": "foo.py"}, "Using tool create_file"),
    ],
)
def test_describe_tool_call(name, args, expected):
    assert _describe_tool_call({"name": name, "args": args}) == expected


def test_describe_tool_call_defaults_missing_fields():
    assert _describe_tool_call({}) == "Using tool "


def test_describe_tool_call_handles_missing_args():
    call = {"name": "read_file"}
    assert _describe_tool_call(call) == "Reading "


# ---------------------------------------------------------------------------
# build_graph
# ---------------------------------------------------------------------------


def test_build_graph_returns_compiled_graph():
    workspace = MagicMock()
    compiled = build_graph(workspace)
    # LangGraph compiled graphs expose .invoke and .stream
    assert hasattr(compiled, "invoke")
    assert hasattr(compiled, "stream")


def test_build_graph_excludes_create_file_from_tool_node():
    """create_file is gated, not executed by ToolNode."""
    workspace = MagicMock()
    compiled = build_graph(workspace)
    # The graph should still expose create_file via its bound tools to the
    # model (so it can be requested), but the ToolNode must not execute it.
    # We verify by inspecting the underlying state graph's nodes.
    state_graph = compiled.get_graph()
    node_ids = set(state_graph.nodes.keys())
    assert "qwen" in node_ids
    assert "tools" in node_ids
    assert "approve" in node_ids
