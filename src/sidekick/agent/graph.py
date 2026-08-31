from __future__ import annotations

import os
from collections.abc import Callable

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage,AIMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import Command, interrupt

from sidekick.agent.prompts import SYSTEM_PROMPT
from sidekick.agent.state import AgentState
from sidekick.agent.tools import build_tools
from sidekick.filesystem.workspace import Workspace, WorkspaceError
from sidekick.models.qwen import get_model


# The checkpointer must outlive a single ``run_agent`` call.  Streamlit reruns
# the app when the user clicks Approve/Reject; recreating MemorySaver inside
# ``build_graph`` would discard the interrupted checkpoint before that rerun.
_CHECKPOINTER = MemorySaver()


def _route(state: AgentState) -> str:
    """Route from qwen to approve (if create_file requested), tools, or END."""
    last = state["messages"][-1]
    tool_calls = getattr(last, "tool_calls", None) or []
    if not tool_calls:
        return END

    # Intercept create_file BEFORE tool execution
    for call in tool_calls:
        if call.get("name") == "create_file":
            return "approve"

    return "tools"


def _make_gate(workspace: Workspace):
    """Build the human-in-the-loop gate node bound to the workspace.

    The interrupt is inside this node, which is LangGraph's durable interrupt
    pattern. On resume the node restarts and ``interrupt`` returns the supplied
    decision; the model node is not run again.
    """

    def _gate(state: AgentState):
        last = state["messages"][-1]
        # Collect every create_file call requested in this turn. A single model
        # turn may request several creations; the gate must handle all of them so
        # none is silently dropped.
        create_calls = [
            call
            for call in (getattr(last, "tool_calls", None) or [])
            if call.get("name") == "create_file"
        ]
        pending = [
            {
                "path": (call.get("args", {}) or {}).get("path", ""),
                "content": (call.get("args", {}) or {}).get("content", ""),
                "tool_call_id": call.get("id", ""),
            }
            for call in create_calls
        ]
        # Interrupt once for the whole turn. The decision applies to every
        # pending create_file call in this turn.
        approve_file_creation = os.getenv("APPROVE_FILE_CREATION")
        if approve_file_creation in ("t", "true"):
            approved = True
        else:
            approved = interrupt({"action": "approve_file_creation", "pending": pending})

        messages: list[ToolMessage] = []
        for item in pending:
            if approved:
                # Perform the gated creation now that the user approved it.
                try:
                    result = workspace.create_file(
                        item.get("path", ""), item.get("content", "")
                    )
                except WorkspaceError as exc:
                    result = f"error: {exc}"
            else:
                result = "The user rejected this file creation request."
            messages.append(
                ToolMessage(
                    content=result,
                    name="create_file",
                    tool_call_id=item.get("tool_call_id", ""),
                )
            )
        return {"messages": messages}

    return _gate


def _post_gate(state: AgentState):
    """After the gate, return to the model so it can continue iterating.

    On approval the file was created by the gate; on rejection a rejection
    message was added. Either way the model should see the outcome and decide
    whether more work is needed, rather than ending the run.
    """
    return "qwen"


def build_graph(workspace: Workspace):
    tools = build_tools(workspace)
    model = get_model().bind_tools(tools)

    # create_file is intercepted by _route and handled by the approve node, so it
    # must NOT be executed by the ToolNode. The model still needs it in its bound
    # tool list so it can *request* a creation, but the ToolNode only runs the
    # non-create tools. This keeps the approval gate in control of file creation
    # and lets the run continue to qwen after an approved creation instead of
    # ending.
    tool_node_tools = [t for t in tools if t.name != "create_file"]

    def call_model(state: AgentState):
        response = model.invoke([SystemMessage(content=SYSTEM_PROMPT), *state["messages"]])
        return {"messages": [response]}

    graph = StateGraph(AgentState)
    graph.add_node("qwen", call_model)
    graph.add_node("tools", ToolNode(tool_node_tools, handle_tool_errors=True))
    graph.add_node("approve", _make_gate(workspace))

    graph.add_edge(START, "qwen")

    graph.add_conditional_edges(
        "qwen",
        _route,
        ["approve", "tools", END],
    )

    # After tools run, return to qwen
    graph.add_edge("tools", "qwen")

    # After the gate (approved or rejected), return to the model so it can
    # continue iterating instead of ending the run.
    graph.add_conditional_edges(
        "approve",
        _post_gate,
        {"qwen": "qwen"},
    )

    return graph.compile(checkpointer=_CHECKPOINTER)


def _describe_tool_call(call: dict) -> str:
    name = call.get("name", "")
    args = call.get("args", {}) or {}
    path = args.get("path", "")
    if name == "list_provided_files":
        return "Listing provided files"
    if name == "read_file":
        return f"Reading {path}"
    if name == "edit_file":
        return f"Editing {path}"
    if name == "search_file":
        return f"Searching {path}"
    return f"Using tool {name}"


def run_agent(
        root: str,
        files: list[str],
        task: str,
        thread_id: str = "default_thread",
        resume_decision: bool | None = None,
        on_event: Callable[[str], None] | None = None,
        on_approval: Callable[[dict], None] | None = None,
):
    """Run or resume the agent graph using checkpointer state."""
    workspace = Workspace(root, files)
    graph = build_graph(workspace)
    allowed = workspace.list_files()

    config = {
        "recursion_limit": 80,
        "configurable": {"thread_id": thread_id},
    }

    if resume_decision is not None:
        input_state = Command(resume=resume_decision)
    else:
        input_state: AgentState = {
            "task": task,
            "root": str(workspace.root),
            "allowed_files": allowed,
            "messages": [
                HumanMessage(
                    content="Provided files:\n" + "\n".join(allowed) + f"\n\nTask:\n{task}"
                )
            ],
        }

    final_state = None
    pending_interrupt = None

    if on_event:
        on_event("Analyzing task" if resume_decision is None else "Resuming task")

    for state in graph.stream(input_state, config=config, stream_mode="values"):
        final_state = state

        # Check for active pause/interrupt
        interrupts = state.get("__interrupt__")
        if interrupts:
            pending_interrupt = (
                interrupts[0].value
                if hasattr(interrupts[0], "value")
                else interrupts[0]
            )
            # Surface the pending approval to the caller (e.g. the UI) so it can
            # ask the user and feed the decision back via resume_decision. The
            # callback's return value is intentionally ignored: the decision is
            # supplied on a separate run via resume_decision, not here.
            if on_approval:
                on_approval(pending_interrupt)
            break

        messages = state.get("messages", [])
        if messages:
            last_msg = messages[-1]
            calls = getattr(last_msg, "tool_calls", None) or []
            for call in calls:
                if on_event:
                    on_event(_describe_tool_call(call))

    if on_event and not pending_interrupt:
        on_event("Finished")

    # A tool call in a message only shows intent; the tool may have failed
    # (e.g. edit_file when `old` is not found exactly once). Only count a file
    # as changed when its tool result indicates success. The tools return a
    # structured signal: a successful edit/create returns a plain message, while
    # a failure returns an "error:"-prefixed message. We key off that signal
    # rather than a fragile prefix heuristic on the message text.
    changed: list[str] = []
    if final_state and "messages" in final_state:
        for msg in final_state["messages"]:
            if getattr(msg, "tool_call_id", None) is None:
                continue
            name = getattr(msg, "name", "")
            if name not in ("edit_file", "create_file"):
                continue
            content = str(getattr(msg, "content", "") or "")
            if content.startswith("error:"):
                continue
            # edit_file results may carry the path in args. For create_file,
            # recover it from the matching assistant tool call.
            path = getattr(msg, "args", {}).get("path")
            if not path and name == "create_file":
                for prior in final_state["messages"]:
                    for call in (getattr(prior, "tool_calls", None) or []):
                        if call.get("id") == msg.tool_call_id:
                            path = (call.get("args", {}) or {}).get("path")
                            break
                    if path:
                        break
            if path and path not in changed:
                changed.append(path)

    total_input = 0
    total_output = 0

    if final_state and "messages" in final_state:
        for msg in final_state["messages"]:
            if isinstance(msg, AIMessage):
                # Standard LangChain token metadata format
                usage = getattr(msg, "usage_metadata", None) or {}
                # Fallback to provider-specific metadata dictionary if usage_metadata is empty
                if not usage:
                    usage = getattr(msg, "response_metadata", {}).get("token_usage", {})

                total_input += usage.get("input_tokens") or usage.get("prompt_tokens", 0)
                total_output += usage.get("output_tokens") or usage.get("completion_tokens", 0)

    token_usage = {
        "input_tokens": total_input,
        "output_tokens": total_output,
        "total_tokens": total_input + total_output,
    }

    return {
        "pending_interrupt": pending_interrupt,
        "summary": (
            final_state["messages"][-1].content
            if (final_state and final_state.get("messages") and not pending_interrupt)
            else None
        ),
        "changed_files": changed,
        "token_usage": token_usage,  # <--- Added token metrics output
        "state": final_state,
    }
    if False:
        return {
        "pending_interrupt": pending_interrupt,
        "summary": (
            final_state["messages"][-1].content
            if (final_state and final_state.get("messages") and not pending_interrupt)
            else None
        ),
        "changed_files": changed,
        "state": final_state,
    }