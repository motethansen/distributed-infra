# Agent roster, LLM switching and chat access

Status: build in progress (2026-10-30). Built so far: roles.yaml + resolver, Opus allow-list with a
monthly cap, DeepSeek project limit, `role` tag and column, `role`/`project`/`sprint`/`story` on
`fleet_ask`. Not yet built: agent definition files, client model selector, auto-queued review.
Inputs: the 5-role Scrum roster proposed by Michael, the live Scrum board
(`/scrum.html` on Vantage), and the fleet as it runs today.

## 1. What exists today

| Piece | State |
|---|---|
| Routing by task class (`config/routing.yaml`) | exists: coding/review = Claude Sonnet, verify = Codex, research = agy, bulk/reasoning = DeepSeek |
| Pick a specific agent per call | exists: `payload.agent`, `payload.model`, `payload.failover=false` (also `fleet_ask(agent=...)`) |
| Failover chain + hand-over of the conversation | exists |
| Executor and token tracking per task | exists (tasks table, `GET /scrum/usage`, Scrum board) |
| Opus | blocked in `agents/claude_agent.py` unless the orchestrator grants it to a role in `roles.yaml` (exists) |
| Scrum roles | `~/.claude/agents/{scrum-master,project-orchestrator,sprint-developer}.md`; scrum-master is pinned to `opus` |
| `scrum.py` (cheersfriends_cc) | never ran; its own header says so. The "5-agent limit" does not exist in any running code |
| Story/sprint tag on fleet tasks | exists as a column, but **nothing sets it**: every sprint on the board shows "agents involved: none recorded" |

The last row is the main gap for "proof of agents picking up tasks". Fix it first.

## 2. Roster (adopted, with four changes)

| Role | Model | Notes |
|---|---|---|
| Product Owner | Claude Opus 5.5 | gated: see 3.2 |
| Scrum Master | Claude Sonnet 5.5 | change `scrum-master.md` from `opus` to `sonnet` |
| Project Optimization Analyst | agy | reads `/scrum/usage` and the sprint files; read-only |
| Developer 1 (front-end/UI) | Claude Sonnet (5 or 5.5) | see 3.1 |
| Developer 2 (back-end/API) | DeepSeek | see 3.3 |
| Inline assistant | Codex in the IDE | plus the reviewer duty in 3.4 |

Changes against the proposal:

1. **Reviewer role added.** The proposal has no one who checks the work. The
   analyst measures; it does not review. Reviewer = Codex (read-only, different
   model family from both developers) for every PR, and Claude Sonnet for
   anything Codex wrote or when Codex is cooling down. Rule: the author never
   reviews its own change. This is why Codex stays in the fleet queue as well as
   in the IDE.
2. **Opus is allowed per role, not globally.** Replace the blanket block with an
   allow-list (`roles.yaml: allow_opus: true` only for Product Owner) and a
   monthly cost cap (the Claude CLI's own estimate, summed from the tasks table). Anything else asking for Opus is
   still refused.
3. **DeepSeek never sees private repos or secrets.** The routing privacy guard
   already forbids it. The payment example in the proposal (webhook
   verification) is exactly the code that must not be handed over with real
   keys or private data. Dev 2 receives a spec plus interfaces and writes
   against fixtures; secrets and live payloads never go in the prompt. Security
   code gets a Sonnet or Codex review plus a human look before merge. DeepSeek runs only for projects listed in
   `deepseek_projects` (empty today, so every project falls back to Claude until you list one).
4. **Model versions are config, not roles.** Whether Dev 1 is Sonnet 5 or 5.5 is
   a one-line change in `roles.yaml`; decide it by the numbers in section 5, not
   in advance. (Check that `claude --model` accepts the exact id before pinning;
   today the code passes the unversioned alias `sonnet`.)

## 3. Design

### 3.1 `config/roles.yaml` (new, tracked, no secrets)

```yaml
roles:
  product_owner:  { agent: claude,   model: opus,            allow_opus: true, monthly_cost_cap_usd: 50 }
  scrum_master:   { agent: claude,   model: sonnet }
  analyst:        { agent: agy,      read_only: true }
  dev_frontend:   { agent: claude,   model: sonnet }
  dev_backend:    { agent: deepseek, model: deepseek-chat, private_fallback: { agent: claude, model: sonnet } }
  reviewer:       { agent: codex,    read_only: true,      fallback: { agent: claude, model: sonnet } }
```

Resolution order for any task: explicit `payload.agent` > project override in
(not built) > role default > task-class routing. The privacy
guard still wins over all of them.

### 3.2 Launching a sprint (the loop)

1. PO (Opus) turns the request into stories with acceptance criteria and writes
   them to the product backlog. One Opus call per planning session.
2. Scrum Master (Sonnet) splits each story into tasks and **queues them with
   `project`, `sprint`, `story` and `role` set**. This is the missing tag.
3. Contract first: the first back-end task of a story emits the API contract
   (OpenAPI or typed schema). Front-end and back-end tasks then run in
   parallel against it; waiting for the finished back-end serialises the sprint.
4. Developers work on a branch and open a PR; they never merge or deploy.
5. Reviewer checks the PR (different family from the author) and posts findings.
6. A story is done only with evidence (merged PR, passing tests or an observed
   response). The Scrum Master records the evidence in the backlog; the board
   reads the backlog.
7. Analyst reads tokens, cost per story point, rework and review findings and
   writes recommendations at sprint end.

### 3.3 Switching the LLM

Two levels, built in this order:

- **Per request (small):** a `/model` style selector in every client:
  `auto` (routing), `claude`, `claude-opus`, `codex`, `agy`, `deepseek`, or a
  role name. Maps straight onto `payload.agent`/`payload.model`/`failover`.
  Add it to the WhatsApp bridge (`agent <llm> <prompt>` already does this), the
  Flutter terminal (dropdown) and fleet-mcp (already has `agent`).
- **Per conversation:** the existing conversation hand-over carries the
  transcript, so switching mid-chat keeps context.

### 3.4 Chat interface

Recommendation: **keep the queue as the single back end and put thin clients in
front of it; do not adopt a second agent runtime as the hub.**

| Option | Fit |
|---|---|
| **T3 Code** (what this session runs in) | GUI over provider CLIs (Codex, Claude Code and others). Good chat UI on the MacBook. It runs the harnesses locally, so fleet work reaches it through fleet-mcp (already registered). It cannot show fleet tasks in its own thread view. Keep as the MacBook front end. |
| **pi** | Minimal terminal agent with its own provider list and extensions; MCP only through an extension; has RPC/SDK modes. Already installed on the MacBook with fleet registered. Good as a scriptable second harness; its default model is pay-per-token OpenRouter, so set it deliberately. |
| **Flutter terminal (ours)** | Already talks to the queue and carries `x-client`, so iPad tasks are provable. Add the model dropdown and the sprint/story tag here. This is the iPad path. |
| **OpenAI-compatible gateway** (`/v1/chat/completions` on the orchestrator) | Would let any chat UI (Open WebUI, pi, LibreChat) use the fleet as one "model". Cost: it can only expose chat, not tool use, and the Claude/Codex/agy access is through subscription CLIs, not API keys. Defer until a second client actually needs it. |

Why not make pi or T3 Code the hub: both execute agents on the machine they run
on. The value of this system is that the queue assigns work to the right machine
and records who ran it. A client-side runtime would bypass that record.

## 4. Build order

1. Tag every queued task with `project/sprint/story/role` (Scrum Master and the
   bridge/MCP paths). Board shows agents per sprint within one sprint.
2. `config/roles.yaml` + resolver in `agents/router.py`; Opus allow-list with
   monthly cap; tests including mutation checks (Opus refused outside the
   allow-list, DeepSeek refused for private).
3. Update the three agent files in `~/.claude/agents` and add `product-owner`,
   `reviewer`, `analyst` definitions; `scrum-master` to `sonnet`.
4. Model selector in Flutter terminal and WhatsApp bridge.
5. Reviewer step wired: PR opened -> review task queued automatically.
6. MacBook <-> thinkpad consistency check and documented takeover (open item).

## 5. How to judge the roster after two sprints

Per role, from the tasks table: tokens out, estimated cost, accepted stories,
review findings per PR, rework tasks per story. Keep a model on a role only if
its cost per accepted story point is competitive. Subscription logins (Claude,
Codex, agy) report tokens but no dollars, so for those compare tokens and
wall-clock time; only DeepSeek has real spend.
