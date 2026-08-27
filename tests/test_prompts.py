"""Tests for src/sidekick/agent/prompts.py."""

from sidekick.agent.prompts import SYSTEM_PROMPT


def test_system_prompt_is_nonempty_string():
    assert isinstance(SYSTEM_PROMPT, str)
    assert SYSTEM_PROMPT.strip()


def test_system_prompt_defines_role():
    assert "constrained coding assistant" in SYSTEM_PROMPT


def test_system_prompt_enforces_file_constraints():
    assert "list_provided_files" in SYSTEM_PROMPT
    assert "Never invent filenames" in SYSTEM_PROMPT
    assert "Never delete files" in SYSTEM_PROMPT
    assert "Never use shell commands or Git" in SYSTEM_PROMPT


def test_system_prompt_requires_approval_for_creation():
    assert "create_file" in SYSTEM_PROMPT
    assert "explicit user approval" in SYSTEM_PROMPT


def test_system_prompt_requires_summary():
    assert "summarize" in SYSTEM_PROMPT
    assert "what you changed" in SYSTEM_PROMPT
    assert "which files changed" in SYSTEM_PROMPT
    assert "remaining concerns" in SYSTEM_PROMPT
