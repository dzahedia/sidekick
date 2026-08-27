
from __future__ import annotations

from typing import Annotated, TypedDict
from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    task: str
    root: str
    allowed_files: list[str]
    messages: Annotated[list, add_messages]
