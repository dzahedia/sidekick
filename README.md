# Local Coding Agent

> **Version 0.75** — see [Changelog](#changelog) for release history.

This is a **highly focused** coding agent that operates exclusively on a small, explicitly selected set of files provided by the user. By limiting its scope, it keeps token usage **low and predictable** while enabling precise, targeted edits.

This design is critical because **real software development happens in increments**. While LLM agents can accelerate greenfield projects or assist beginners, **production-grade software demands precision**. This agent is built for **incremental development** and continuous code improvement, ensuring that changes are safe, controlled, and context-aware.

In addition to the full agent run **for convenience**, you can also **ask the LLM directly**. This is a single, stateless call (no agent loop, no file access) that streams a direct LLM response—useful for quick questions or one-off completions.

## Prerequisites

- **Python 3.12+** — the project targets modern Python syntax and dependencies.
- **uv** — a fast Python package manager and runner. Install it from [https://docs.astral.sh/uv/](https://docs.astral.sh/uv/) (e.g. `curl -LsSf https://astral.sh/uv/install.sh | sh` on macOS/Linux).
- **An LLM API key** — the agent needs access to a large language model to reason about the task and generate edits. Set the required environment variable `LLM_API_KEY` before running. See [Configuration](#configuration) for details.

## Setup

```bash
uv sync
```

## Run

The app is a FastAPI service (a port of the original Streamlit SideKick app). Start it with:

```bash
uv run uvicorn sidekick.api.main:app  --port 8005
```

Then open `http://localhost:8005` in a browser for the web UI, or use the REST API directly.



### Web UI

The web UI (served at `/`) is a single-page interface for driving the agent without touching the REST API directly. It is split into two areas:

- **Workspace (sidebar)** — where you define the context the agent is allowed to work with:
  - **Root directory** — the base path the provided files are resolved against.
  - **Files to provide** — a list of files (one per line) to make available to the agent. `*` glob patterns are supported (e.g. `src/models/*`).
- **Task (main area)** — where you describe what you want done and start a run:
  - **Task** — a free-text description of the change you want.
  - **Run Agent** — starts a new agent run with the workspace and task above.
  - **Ask LLM** — sends the task text as a single, direct LLM question (no agent loop, no file access) and shows the streamed answer in a separate "LLM answer" section.

While a run is in progress, the UI shows a **status** section with the current state and a live log of the agent's activity. If the agent requests to create a new file, an **approval dialog** appears showing the requested file and its content, with **Approve Creation** / **Reject Creation** buttons.

When the run finishes, a **results** section appears with:

- **Final summary** — the agent's summary of what it did.
- **Changed files** — the list of files that were modified.

## Configuration

The agent is built on top of a large language model (LLM) and needs credentials to call it. Configure the following environment variables (or a `.env` file at the project root):

| Variable | Description                                                                    |
| --- |--------------------------------------------------------------------------------|
| `LLM_API_KEY` | API key for the LLM provider used to generate edits.                           |
| `LLM_BASE_URL` | Base URL of the LLM endpoint, useful for proxies or self-hosted models.        |
| `LLM_MODEL` | Model name to use (e.g. `qwen3.8:30b`). Defaults to a sensible built-in value. |
| `APPROVE_FILE_CREATION` | When set to `t` or `true`, file creation is auto-approved (no manual approval prompt). |

## How it works

The agent is intentionally constrained to keep token usage low and edits precise:

1. **File selection** — You provide a root directory plus an explicit list of files (or globs). Only those files are made available to the agent; it cannot read or modify anything outside that set.
2. **Task** — You describe what you want done (for example, "add a docstring to the main function").
3. **Reasoning** — The LLM inspects the provided files and the task, then plans the smallest correct change needed.
4. **Edits** — Edits are applied as targeted, exact text replacements against the provided files. Each replacement must match the original text exactly, so changes stay surgical and reviewable.
5. **Iteration** — After an edit, the agent re-reads the affected file to verify the change and can continue iterating until the task is complete.

This design means the agent never touches files you didn't select, and every change is a small, explicit diff rather than a full-file rewrite.

Token usage is tracked per run and reported in the `token_usage` field of the status response (and in the final `done` event of the `/api/llm` stream), so you can see exactly how many tokens each run consumed.


## Changelog

- **0.5** — Initial release.
- **0.75** — Added direct LLM call (no agent loop).
