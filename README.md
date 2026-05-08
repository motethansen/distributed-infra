# distributed-infra

A distributed AI agent stack — task queue, worker fleet, and interactive CLI for running Claude, Gemini, Codex, and Groq across multiple machines over a private Tailscale network.

**Three machines. Four AI agents. Two ways to work.**

- **Local** — run an agent directly on your MacBook, get an instant answer, stay in flow
- **Queued** — describe a task, pick a machine and agent, it runs in the background while you keep working

---

## Architecture

```
MacBook Pro (Orchestrator)
  ├── FastAPI queue server  (port 8000, SQLite-backed)
  ├── da CLI               — the control plane
  └── Tailscale IP         — reachable by all workers

Worker machines (Mac Mini, ThinkPad, …)
  └── FastAPI worker server  (port 8001)
        ├── Poller: claims tasks every 10 s
        ├── Dispatches to the right handler (agent_run / build / script / …)
        └── Reports: done / failed / needs_human
```

All machines connect over [Tailscale](https://tailscale.com) mesh VPN — no port-forwarding, no firewall rules.

---

## Machines (example)

| Machine | Role | Highlights |
|---|---|---|
| MacBook Pro | Orchestrator | Queue server, `da` CLI |
| Mac Mini (Intel, macOS) | Worker | iOS/Xcode, Flutter, Swift, Cloudflare deploys |
| ThinkPad (Ubuntu) | Worker | Android, Gradle, Python/Node backend, Docker |

Declare your own fleet in `config/machines.yaml` (see [Configuration](#configuration)).

---

## Quick Start

### 1. Clone on every machine

```bash
git clone https://github.com/your-username/distributed-infra.git
cd distributed-infra
```

### 2. Create `.env` on each machine

```bash
cp .env.example .env
# Edit: set MACHINE_NAME, MACHINE_ROLE, SECRET_KEY (same value on all machines)
openssl rand -hex 32   # generate a SECRET_KEY
```

### 3. Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 4. Set up Tailscale IPs

```bash
cp config/machines.yaml.example config/machines.yaml
# Fill in each machine's Tailscale IP: tailscale ip -4
```

### 5. Start services

```bash
# MacBook — orchestrator
uvicorn orchestrator.main:app --host 0.0.0.0 --port 8000

# Mac Mini / ThinkPad — worker
uvicorn worker.main:app --host 0.0.0.0 --port 8001
```

### 6. Launch the CLI

```bash
./da          # from the repo root
# or if symlinked:
ln -sf "$(pwd)/da" /usr/local/bin/da
da
```

```
╭──────────────────────────────────────────────────────────────────╮
│  Distributed Agents                                              │
│  macbook-pro  ·  mac-mini  ·  thinkpad-x1                       │
│  2/2 workers online                                              │
╰──────────────────────────────────────────────────────────────────╯

  Type help for commands, exit to quit.
da ›
```

### 7. Auto-start at boot

**macOS (launchd)**
```bash
cp scripts/com.techstartups.orchestrator.plist.example \
   ~/Library/LaunchAgents/com.techstartups.orchestrator.plist
# Edit: fill in YOUR_USERNAME and YOUR_SECRET_KEY
launchctl load ~/Library/LaunchAgents/com.techstartups.orchestrator.plist
```

**Ubuntu (systemd)**
```bash
# Worker service is declared in scripts/worker.service
# Copy it, fill in paths, enable:
sudo cp scripts/worker.service /etc/systemd/system/infra-worker.service
sudo systemctl daemon-reload
sudo systemctl enable --now infra-worker
```

---

## CLI Reference

### Local agents (no queue, runs on this MacBook)

```
da › run claude explain the Riverpod provider pattern
da › run gemini summarise the last 10 commits
da › run codex refactor the auth module

da › test              # smoke-test all four agents
da › test gemini       # test one agent
```

---

### Assigning tasks to workers

```
da › assign <description> [--machine=X] [--agent=Y] [--type=Z]
```

**Fully explicit — recommended when you know exactly where it should run:**
```
da › assign build the iOS release --machine=mac-mini --agent=gemini --type=ios_build
da › assign run the test suite --machine=thinkpad-x1 --agent=claude --type=test_run
da › assign deploy the site --machine=mac-mini --agent=- --type=run_script
```

**Auto-routed — Claude picks the best machine and agent:**
```
da › assign refactor the payment service for better error handling
     → Asking Claude for routing recommendation…
     → Suggested: thinkpad-x1 / claude  (Python backend, no build tools needed)

  Machine   thinkpad-x1
  Agent     claude
  Task type agent_run
  Reason    Python backend work, no mobile build tooling required

  Confirm? [Y/n]
```

**Partial flags — override just what you care about:**
```
da › assign write unit tests for the cart module --machine=thinkpad-x1
     # Claude picks the agent; you lock the machine
```

**Validation:** `assign` checks before queuing:
- Blocks if the machine doesn't have the capability for the task type
- Warns if the requested agent isn't listed for that machine

---

### Monitoring the queue

```
da › queue                           # all tasks, newest first
da › queue --status=pending          # filter: pending / claimed / in_progress
da › queue --status=needs_human      #         done / failed / needs_human
```

Queue columns:
- **Machine** — shows `→mac-mini` for pending tasks (targeted but not yet claimed), actual machine name once claimed
- **Agent** — which AI agent handled/will handle it
- **Task / Notes** — prompt preview or error summary

---

### Handling results

```
da › review                          # all tasks waiting for your attention
da › failures                        # failed tasks with error details + re-queue hints

da › resolve a3f29c1d done           # mark a specific task done
da › resolve a3f29c1d pending        # re-queue (retry from scratch)
da › resolve a3f29c1d failed         # record as failure
da › resolve a3f29c1d done --notes="handled manually"

da › resolve all                     # bulk-close every needs_human task as done
da › resolve all pending             # bulk re-queue all needs_human tasks
```

---

### Machine management

```
da › status                          # health, active tasks, done/failed counts, top LLM per machine
da › ssh mac-mini                    # open SSH session to a worker
```

---

### Skills

```
da › skills                                        # declared capabilities per machine
da › skills available                              # full registry + install status (SSH check)
da › skills available --category=mobile            # filter by category
da › skills list mac-mini                          # SSH-verify what's actually installed
da › skills install thinkpad-x1 docker             # install a skill via SSH
da › skills add mac-mini swiftlint                 # register new capability in machines.yaml
da › skills create deploy                          # scaffold a new custom skill handler
```

---

## Task Types

| Type | What it does | Typical machine |
|---|---|---|
| `agent_run` | Runs Claude / Gemini / Codex / Groq on a prompt | Any worker |
| `run_script` | Executes a shell script | Any worker |
| `git_pull` | Pulls latest on a repo | Any worker |
| `ios_build` | `xcodebuild` / Flutter / CocoaPods | macOS worker |
| `android_build` | Gradle | Ubuntu worker |
| `npm_build` | `npm run <script>` | Any worker |
| `test_run` | pytest / jest / gradle test / xcode test | Any worker |
| `lint` | ruff / eslint / ktlint / swiftlint | Any worker |

Custom types are auto-discovered from `worker/handlers/<type>.py` — no wiring needed.

---

## Payload Reference

Every task payload is a JSON object sent with the task. Common fields:

### `agent_run`

```json
{
  "agent":  "claude",
  "prompt": "Refactor the auth module to use dependency injection",
  "cwd":    "~/Projects/my-app",
  "model":  "claude-opus-4-5"
}
```

- **`cwd`** — working directory for the agent. Claude will be able to read and write files relative to this path. Always set this for tasks that touch files.
- **`model`** — optional model override (defaults to each agent's default)

### `run_script`

```json
{
  "script":  "cd ~/Projects/my-app && npm run build 2>&1",
  "cwd":     "~/Projects/my-app",
  "timeout": 300
}
```

- **`timeout`** — seconds before the script is killed (default: 120)
- **`_target_machine`** — set automatically by `assign --machine=X`; can also be set directly

### `git_pull`

```json
{
  "repo_path": "~/Projects/my-app",
  "branch":    "main"
}
```

---

## Best Practices

### Use `run_script` for file operations, `agent_run` for thinking

`agent_run` is best for tasks that need creativity or reasoning — writing code, designing architecture, summarising. For deterministic file operations (deploy, build, scaffold), use `run_script` — it's faster, more predictable, and the output is always captured.

```
# Good — agent thinks, script acts
da › assign write the authentication handler --machine=thinkpad-x1 --agent=claude --type=agent_run
da › assign deploy to production --machine=mac-mini --type=run_script
```

### Always set `cwd` for agent tasks that touch files

Without `cwd`, Claude runs from the worker's project directory (`distributed-infra`) and cannot write to your actual project. Always include it:

```
da › assign add error handling to all API routes --machine=thinkpad-x1 --agent=claude --type=agent_run
```
Then manually add `"cwd": "~/Projects/my-backend"` to the payload — or submit via the API directly:

```bash
curl -X POST http://localhost:8000/tasks \
  -H "x-secret-key: $SECRET_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "agent_run",
    "payload": {
      "agent": "claude",
      "prompt": "Add error handling to all API routes",
      "cwd": "~/Projects/my-backend",
      "_target_machine": "thinkpad-x1"
    }
  }'
```

### Keep `agent_run` prompts self-contained

The agent runs in a single non-interactive `claude -p` call — it cannot ask follow-up questions. Give it everything it needs upfront: file paths, constraints, output format, examples.

```
# Too vague — agent will ask for clarification it can't receive
"update the website"

# Self-contained — agent can act immediately
"In ~/Projects/motethansen-site, update index.html to add a Projects section
 with cards for winedragons.asia and urbanlife.works. Use the existing CSS
 variables. Do not change the navigation or footer."
```

### Machine targeting is enforced at the database level

`_target_machine` in the task payload is checked by the orchestrator before returning a task to a claiming worker. A machine that doesn't match the target will never see the task — no race conditions.

### Use `resolve pending` to retry, not `assign` again

If a task fails or times out, `resolve <id> pending` puts it back in the queue with the same payload. Only create a new task if you want to change the prompt or routing.

```
da › resolve 3cd295f8 pending    # retry with same payload
```

### Check `review` before `resolve all`

`resolve all done` closes everything in one shot. Run `review` first to make sure nothing important is hiding behind a generic `needs_human` status.

---

## Task Routing

```
assign "..." --machine=mac-mini --agent=claude --type=agent_run
         │                │               │             │
         │                │               │             └── task type (capability)
         │                │               └── which AI agent runs the prompt
         │                └── which machine claims the task (enforced in DB)
         └── natural language description → stored as notes + prompt
```

If any flag is omitted, Claude analyses the description and recommends the missing values. You confirm before anything is queued.

**Enforcement layers:**

1. **`da assign` validates** — blocks if machine lacks the capability; warns if agent isn't listed for that machine
2. **Orchestrator DB filters** — `json_extract(payload, '$._target_machine')` in the SQL claim query — only the named machine can claim the task
3. **Agent handler dispatches** — `payload.agent` is passed directly to the agent's CLI subprocess

---

## Skills Registry

`config/skills.yaml` is the source of truth for installable tools — install recipes, check commands, task type mappings, and handler paths for 20+ skills across four categories.

### Scaffold a new skill

```
da › skills create summarise-pr

  Description: Summarise a pull request using git log and diff
  Category [custom]: backend
  Check command: git --version
  Install (macos): brew install git
  Install (linux): sudo apt-get install -y git
  Task type: summarise_pr

  ✓ Created handler: worker/handlers/summarise_pr.py
  ✓ Registered in skills.yaml

  Next steps:
  1. Implement worker/handlers/summarise_pr.py
  2. Add to machines.yaml:  skills add <machine> summarise_pr
```

The handler is auto-discovered by the worker — no changes to `__init__.py` needed.

---

## Configuration

### `config/machines.yaml`

```yaml
machines:
  macbook-pro:
    tailscale_ip: "YOUR_MACBOOK_TAILSCALE_IP"
    role: orchestrator
    os: macos
    queue_port: 8000

  mac-mini:
    tailscale_ip: "YOUR_MACMINI_TAILSCALE_IP"
    role: worker
    os: macos
    capabilities:
      - ios_build
      - xcode
      - swift
      - agent_run
      - run_script
      - git_pull
    agents:
      - claude
      - gemini
      - codex
    worker_port: 8001

  thinkpad-x1:
    tailscale_ip: "YOUR_THINKPAD_TAILSCALE_IP"
    role: worker
    os: linux
    aliases: ["old-hostname"]    # historical names kept for stats continuity
    capabilities:
      - android_build
      - python_backend
      - docker
      - agent_run
      - run_script
    agents:
      - claude
      - gemini
    worker_port: 8001
```

`config/machines.yaml` is gitignored (it contains your real Tailscale IPs). Track `config/machines.yaml.example` in git.

### `.env` (not committed)

```bash
MACHINE_NAME=macbook-pro           # must match a key in machines.yaml
MACHINE_ROLE=orchestrator          # orchestrator | worker
SECRET_KEY=<openssl rand -hex 32>  # same on all machines
TAILSCALE_IP=100.x.x.x
ORCHESTRATOR_URL=http://100.x.x.x:8000   # worker only
WORKER_PORT=8001                          # worker only
POLL_INTERVAL_SECONDS=10
```

---

## AI Agents

All four agents use their CLI tools — no API keys in config files. Each authenticates via its own login session:

| Agent | CLI install | Auth |
|---|---|---|
| Claude | `npm install -g @anthropic-ai/claude-code` | `claude login` |
| Gemini | `npm install -g @google/gemini-cli` | `gemini login` |
| Codex | `npm install -g @openai/codex` | `codex login` |
| Groq | install via pip / npm | set `GROQ_API_KEY` in `.env` |

Agents run non-interactively in the queue:
- Claude: `claude -p "<prompt>" --dangerously-skip-permissions`
- Gemini: `gemini --yolo -p "<prompt>"`
- Codex: `codex --approval-mode full-auto -q "<prompt>"`

---

## Troubleshooting

**Worker keeps showing "Poller error: ConnectError"**
The worker can't reach the orchestrator. Check: (1) Tailscale is running on both machines, (2) `ORCHESTRATOR_URL` in worker `.env` matches the orchestrator's Tailscale IP, (3) orchestrator is running on port 8000.

**Task goes to wrong machine**
Make sure the orchestrator was restarted after any changes to `db.py`. The `_target_machine` filter runs in the orchestrator's SQLite query — stale code means the old query runs without the filter.

**Agent task has empty output / `needs_human` with no details**
Check `da review` — since recent fixes, stdout/stderr are preserved in the result even when a task fails. If still empty, the worker may be running old code: `git pull && restart worker`.

**Claude writes to wrong directory**
Add `"cwd": "~/Projects/your-project"` to the task payload. Without it, Claude runs from the worker's `distributed-infra` directory and cannot reach other projects.

**`da assign` blocks with "machine doesn't have capability"**
Add the capability to `config/machines.yaml` (`skills add <machine> <type>`) or use a different machine.

**Task stuck `in_progress` after orchestrator restart**
The worker's HTTP connection broke mid-task. Mark it manually:
```
da › resolve <task-id> failed --notes="orphaned after restart"
```
Then `resolve <id> pending` to retry.

---

## License

MIT — see [LICENSE](LICENSE).
