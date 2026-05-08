# distributed-infra

A distributed AI agent stack — task queue, worker fleet, and interactive CLI for running Claude, Gemini, Codex, and Cursor Agent across multiple machines.

**Three machines. Four AI agents. Two ways to work.**

- **Local** — run an agent directly on your machine, get an instant answer, stay in flow
- **Queued** — describe a task, Claude recommends which machine and agent should handle it, runs in the background while you work on something else

---

## Architecture

```
MacBook Pro (Orchestrator)
  ├── FastAPI queue server  (port 8000)
  ├── SQLite task queue
  └── da CLI — the control plane

ThinkPad / Mac Mini (Workers)
  └── FastAPI worker server  (port 8001)
        ├── polls queue every 10 seconds
        ├── claims tasks matching its capabilities
        └── reports: completed / failed / needs_human
```

All machines connect via [Tailscale](https://tailscale.com) mesh VPN — no port forwarding, no config headaches.

---

## Machines (example setup)

| Machine | Role | Capabilities |
|---|---|---|
| MacBook Pro | Orchestrator | Queue server, `da` CLI |
| ThinkPad (Ubuntu) | Worker | Android builds, backend dev, general coding |
| Mac Mini (Intel) | Worker | iOS/Xcode builds, Flutter, Swift |

Declare your own machines in `config/machines.yaml`.

---

## Quick Start

### 1. Clone on each machine

```bash
git clone https://github.com/motethansen/distributed-infra.git
cd distributed-infra
```

### 2. Create `.env` (not committed — copy to each machine)

```bash
cp .env.example .env
# Edit .env: set MACHINE_NAME, MACHINE_ROLE, SECRET_KEY (same on all machines)
# Generate a secret: openssl rand -hex 32
```

### 3. Run the setup script for your machine

```bash
bash scripts/setup-macbook.sh   # orchestrator (MacBook)
bash scripts/setup-ubuntu.sh    # worker (Ubuntu / ThinkPad)
bash scripts/setup-macmini.sh   # worker (Mac Mini)
```

### 4. Start services

```bash
# MacBook — orchestrator + queue
source .venv/bin/activate
uvicorn orchestrator.main:app --host 0.0.0.0 --port 8000

# ThinkPad / Mac Mini — worker
source .venv/bin/activate
uvicorn worker.main:app --host 0.0.0.0 --port 8001
```

### 5. Launch the CLI

```bash
./da
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

### 6. Auto-start at boot (optional)

**macOS** — copy and customise the launchd plist:
```bash
cp scripts/com.techstartups.orchestrator.plist.example \
   ~/Library/LaunchAgents/com.techstartups.orchestrator.plist
# Fill in YOUR_USERNAME and YOUR_SECRET_KEY, then:
launchctl load ~/Library/LaunchAgents/com.techstartups.orchestrator.plist
```

**Ubuntu** — systemd user service:
```bash
cp scripts/worker.service ~/.config/systemd/user/infra-worker.service
# Edit: fill in your username and project path
systemctl --user enable --now infra-worker
```

---

## CLI Commands

### Local agents (run on this machine, no queue)

```
da › run claude explain this Riverpod provider pattern
da › run gemini summarise the last 10 commits
da › test                   # smoke-test all four agents
da › test codex             # test one agent
```

### Queue (send tasks to workers)

```
da › assign review the auth module for security issues
     → Asking Claude for routing recommendation…
     → Suggested: thinkpad-x1 / claude  (code review, no build required)
     → Confirm? Y
     → ✓ Task queued  a3f29c1d  →  thinkpad-x1 / claude

da › assign build the iOS app --machine=mac-mini --llm=gemini --type=ios_build

da › queue                           # view all tasks
da › queue --status=needs_human      # filter by status
da › review                          # tasks waiting for your action
da › failures                        # failed tasks with error details
da › resolve a3f29c1d done           # mark a task resolved
da › resolve all                     # bulk-close all needs_human tasks
```

### Machines

```
da › status                          # per-machine health + LLM usage stats
da › ssh mac-mini                    # open SSH session to a worker
```

### Skills

```
da › skills                          # declared capabilities per machine
da › skills available                # full registry with install status per machine
da › skills available --category=mobile
da › skills list mac-mini            # SSH-check what's actually installed
da › skills install thinkpad-x1 flutter
da › skills add mac-mini swiftlint   # register capability in machines.yaml
da › skills create deploy            # scaffold a new custom skill
```

---

## Task Types

| Type | Handler | Typical machine |
|---|---|---|
| `agent_run` | Claude / Gemini / Codex / Cursor Agent | Any worker |
| `android_build` | Gradle | Ubuntu worker |
| `ios_build` | xcodebuild / Flutter | macOS worker |
| `npm_build` | npm run | Any worker |
| `git_pull` | git | Any worker |
| `run_script` | shell | Any worker |
| `test_run` | pytest / jest / gradle test | Any worker |
| `lint` | ruff / eslint / ktlint / swiftlint | Any worker |
| `human_action` | — | You (via `da review`) |

---

## Skills Registry

`config/skills.yaml` is the central catalog of installable tools — install recipes, check commands, and task type mappings for 20 skills across four categories (`ai-agent`, `mobile`, `backend`, `infrastructure`).

Create a custom skill interactively:

```
da › skills create deploy
  Description: Deploy a build artifact to a remote server
  Category [custom]: infrastructure
  Check command: rsync --version
  Install command (macos): brew install rsync
  Install command (linux): sudo apt-get install -y rsync
  Task type: deploy

  ✓ Created handler: worker/handlers/deploy.py
  ✓ Registered in skills.yaml
```

The scaffolded handler is auto-discovered by the worker dispatch immediately — no manual wiring required.

---

## Configuration

**`config/machines.yaml`** — declare your machines, Tailscale IPs, and capabilities:

```yaml
machines:
  macbook-pro:
    tailscale_ip: "YOUR_MACBOOK_TAILSCALE_IP"   # tailscale ip -4
    role: orchestrator
    os: macos
    queue_port: 8000

  thinkpad-x1:
    tailscale_ip: "YOUR_THINKPAD_TAILSCALE_IP"
    role: worker
    os: linux
    capabilities: [android_build, git_pull, run_script, agent_run]
    agents: [claude, gemini, codex, cursor-agent]
    worker_port: 8001
```

**`.env`** (not committed — generate and copy to each machine):

```bash
MACHINE_NAME=macbook-pro
MACHINE_ROLE=orchestrator
SECRET_KEY=your-secret-key   # openssl rand -hex 32
ORCHESTRATOR_URL=http://YOUR_MACBOOK_TAILSCALE_IP:8000
```

---

## AI Agents

All four agents run headlessly via their CLI tools — no API keys in config files. Each authenticates via its own login session:

```bash
claude     → claude login    (Claude Code CLI)
gemini     → gemini login    (Gemini CLI)
codex      → codex login     (OpenAI Codex CLI)
cursor     → agent login     (Cursor Agent CLI)
```

---

## License

MIT — see [LICENSE](LICENSE).
