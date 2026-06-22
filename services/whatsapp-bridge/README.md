# WhatsApp Bridge

FastAPI service (port `3001`, launchd `com.techstartups.whatsapp-bridge`) that turns
your WhatsApp **self-chat** into a remote control for the distributed agent fleet.
It receives messages via the Waha webhook, queues tasks on the orchestrator
(MacBook Pro), and replies in the chat when each task finishes.

> Send commands to **yourself** ("Message yourself" in WhatsApp). The bridge only
> acts on messages in your own self-chat (`fromMe=true`); everything else is ignored.

## Commands

| Command | What it does |
|---------|--------------|
| `agent <llm> <prompt>` | **Launch a CLI agent** with a free-form prompt (BLI-050). |
| `run <agent> <prompt>` | Lower-level alias — runs `agent_run` on mac-mini. |
| `write article: <topic>` | Long-form draft (content agent / Claude). |
| `write post: <topic> [--format=twitter]` | Social post (social agent / Groq). |
| `code review: <path> [--focus=security]` | Repo code review. |
| `assign <description> [--machine=X] [--agent=Y] [--type=Z]` | Full control over machine / agent / task type. |
| `assist <today\|sync\|status\|plan [today\|week]>` | Query the AI Assistant on the MacBook. |
| `status` / `queue` / `review` / `failures` | Fleet + queue visibility. |
| `help` / `help <question>` | Static help / ask Claude how to phrase a command. |

## `agent <llm> <prompt>`

Routes the prompt to a subscription CLI agent on a worker (mac-mini) and replies
with the result. No API keys needed — the agents authenticate via their own logins.

Examples:

```
agent claude help me start a new writing project. ask me what i want to do
agent code produce an image of a car
agent agy review my task list and suggest today's activities
agent codex fix the failing test in cart.py
```

### Agent keywords

| Keyword(s) | Backend (worker `agents/runner.py`) | CLI |
|------------|-------------------------------------|-----|
| `claude`, `code`, `claude-code` | `claude` | `claude -p` (Claude Code) |
| `agy`, `antigravity` | `agy` | `agy -p` (Google Antigravity) |
| `codex`, `gpt` | `codex` | OpenAI Codex CLI |
| `groq` | `groq` | Groq |
| `content` | `content` | long-form content agent |
| `social` | `social` | social-post agent |

An unknown keyword replies with the accepted list. A missing prompt replies with usage.

### Current limitations (next increments)

- **Single-shot only.** Each `agent` message is one task; the agent cannot ask
  follow-up questions yet. Put everything needed in the prompt. Multi-turn
  sessions (the agent's question comes back to WhatsApp and your reply continues
  the same session) are the next step of BLI-050.
- **Output is truncated** to ~1400 chars in the reply. Chunking long output and
  returning non-text artifacts (e.g. a generated image as a file) is a follow-up.

## Tests

```
.venv/bin/python -m pytest tests/test_bridge_agent.py -q
```
