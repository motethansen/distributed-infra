# fleet-mcp — the distributed agents inside any harness

A small MCP server (stdio) that gives Claude Code, Codex, OpenCode, Antigravity, pi —
and T3 Code threads, which run on those — access to the agent fleet:

| Tool | What it does |
|---|---|
| `fleet_ask` | Run a prompt on a fleet agent and return the answer (or a `task_id` if it takes longer than `wait_seconds`, max 110). |
| `fleet_result` | Get / wait for a task's result. |
| `fleet_tasks` | Recent tasks in the queue. |
| `fleet_machines` | Workers, whether they are active, and whether they can run agents. |
| `fleet_end_conversation` | Forget a named conversation. |

The agents run **on the workers under their own logins** — `claude`, `codex` and `agy` use
their subscription CLI logins, `deepseek` uses its API key. This server holds no LLM
credential. If the chosen agent is out of usage the worker switches to the next one
(claude → codex → agy for most work, see `config/routing.yaml` `fallbacks`) and the
result's `failover` field says who was skipped and when it is back.

## Conversations, across harnesses

Pass `conversation: "<name>"` to `fleet_ask` and the recent turns are kept on this machine
(`data/fleet_mcp_state.json`, gitignored) and sent with every turn. Because the state is
local and shared, a conversation started from Claude Code can be continued from Codex or
OpenCode with the same name — useful when Claude itself hits its limit.

## What it does and does not solve

- **Your harness's own model is not covered.** If Claude is out of usage, Claude Code cannot
  run at all — so it cannot call this tool either. Switch harness (Codex / OpenCode thread in
  T3 Code, or the `codex` CLI): the fleet tools are registered there too.
- **Delegating from Claude Code to `agent: "claude"` spends the same Claude quota.** To save
  Claude usage, delegate to `codex`, `agy` or `deepseek`, or use a `task_kind`.
- `cwd` is a directory **on the worker**, not on this machine.
- `sensitivity: "private"` keeps the work off non-private providers (never DeepSeek).

## Setup (done on the MacBook 2026-10-28)

```bash
cd distributed-infra && .venv/bin/pip install -r clients/fleet-mcp/requirements.txt
PY=$PWD/.venv/bin/python; SRV=$PWD/clients/fleet-mcp/server.py
claude mcp add --scope user fleet -- $PY $SRV      # Claude Code (and T3 Code's Claude threads)
codex mcp add fleet -- $PY $SRV                    # then add tool_timeout_sec = 150 (see below)
agy mcp add fleet $PY $SRV                         # Antigravity
# OpenCode: ~/.config/opencode/opencode.jsonc → "mcp": {"fleet": {"type": "local", "command": [PY, SRV]}}
```

Codex asks before every MCP tool call. In `~/.codex/config.toml` the read-only tools are
pre-approved; `fleet_ask` (which starts work on the fleet) still asks:

```toml
[mcp_servers.fleet]
tool_timeout_sec = 150
[mcp_servers.fleet.tools.fleet_machines]
approval_mode = "approve"   # same for fleet_result, fleet_tasks, fleet_end_conversation
```

Config comes from the environment, falling back to the repo's `.env` — so `SECRET_KEY`
never has to be copied into a harness config: `SECRET_KEY`, `ORCHESTRATOR_URLS` (tried in
order; both orchestrators are active), `FLEET_MCP_STATE`, `VANTAGE_URL`, `VANTAGE_INGEST_KEY`.

## Health

Reports to Vantage as component `fleet-mcp` (`up`, or `degraded` with the error when no
orchestrator answers). It only runs while a harness uses it, so between sessions Vantage
shows it as unknown, which is expected.

Tests: `tests/test_fleet_mcp.py` (fake orchestrator, no network).
