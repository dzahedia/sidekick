from __future__ import annotations

import os
from langchain_openai import ChatOpenAI


def get_model() -> ChatOpenAI:
    return ChatOpenAI(
        base_url=os.getenv("LLM_BASE_URL", "chat_open_ai compatible, like vllm url"),
        model=os.getenv("LLM_MODEL", "qwen3.8:27b"),
        api_key=os.getenv("LLM_API_KEY", "not-needed"),
        temperature=0,
        extra_body={"enable_thinking": True,"enable_auto_tool_choice": True,
                    "tool_call_parser": "qwen"}
    )
