# Building a Distributed AI Agent Stack: Claude, Gemini, Codex, and Cursor Agent Across Three Machines

*A practical walkthrough of running multiple AI coding agents as distributed workers on your existing hardware — and why mixing online and local agents changes how you think about AI-assisted development.*

---

Every major AI coding tool now ships a CLI. Claude Code, Gemini CLI, OpenAI Codex, Cursor Agent — all of them can be called headlessly with a single flag and a prompt. Most developers use them one at a time, interactively, on one machine.

This post is about what happens when you treat them as a fleet.

The setup: a task queue on your primary machine, worker processes on every machine in your network, and four AI agents available as callable workers across all of them. Two modes of operation — run locally for instant results, push to the queue for parallel or hardware-specific work. Everything runs as CLI processes over Tailscale with no additional API overhead.

---

## Architecture Overview

The system has three layers:

**Orchestrator (MacBook Pro)**
- FastAPI server on port 8000
- SQLite task queue (`aiosqlite`, async)
- Interactive `da` CLI — the control plane for the whole fleet
- Tailscale static IP: assigned by Tailscale (run `tailscale ip -4`)

**Workers (ThinkPad Ubuntu + Mac Mini Intel)**
- FastAPI server on port 8001
- Background poller — claims tasks every 10 seconds
- Dispatches to the right handler based on task type
- Reports `completed` / `failed` / `needs_human` back to orchestrator

**AI Agents (on every machine)**
- Claude Code CLI: `claude -p "prompt"`
- Gemini CLI: `gemini -p "prompt"`
- OpenAI Codex CLI: `codex exec "prompt"`
- Cursor Agent CLI: `agent -p "prompt" --trust`

All connected via Tailscale mesh VPN. No port forwarding. Each agent authenticates via its own login session — no API keys in config files.

---

## Two Modes: Local and Queued

The most useful design decision was exposing both modes from a single interactive CLI:

### Local — run on this machine, right now

```
da › run claude explain this Riverpod provider pattern
da › run gemini summarise the last 10 commits
da › test                   # smoke-test all four agents
da › test codex             # test one agent
```

Local runs are synchronous — the output streams directly to your terminal. Best for quick questions, code explanations, or anything where you want an immediate response without context-switching.

### Queued — send to a worker, pick up the result later

```
da › assign review the auth module for security issues
     → Asking Claude for routing recommendation…
     → Suggested: ThinkPad / claude  (reasoning: code review, no build required)
     → Confirm? Y
     → ✓ Task queued  a3f29c1d  →  thinkpad / claude

da › queue
da › review       # tasks waiting for human action
```

Queued tasks are routed by Claude based on the description — it reads what you're asking for and matches it to the capabilities declared for each worker machine. You can override with explicit flags:

```
da › assign build the iOS app --machine=mac-mini --llm=gemini --type=ios_build
```

This separation is natural in practice: keep short interactive tasks local, push anything hardware-specific or long-running to a worker.

---

## Task Types

The queue supports these task types out of the box:

```python
class TaskType(str, Enum):
    android_build = "android_build"
    ios_build     = "ios_build"
    npm_build     = "npm_build"
    git_pull      = "git_pull"
    test_run      = "test_run"
    lint          = "lint"
    run_script    = "run_script"
    agent_run     = "agent_run"
    human_action  = "human_action"
    custom        = "custom"
```

Each worker declares its capabilities in `config/machines.yaml`. The ThinkPad claims `android_build`, `git_pull`, `run_script`, `agent_run`. The Mac Mini claims `ios_build`, `xcode`, `flutter`, `agent_run`. Tasks are matched to workers that can handle them.

---

## Running It: Step by Step

### 1. Start the orchestrator (MacBook)

```bash
cd ~/Projects/distributed-infra
uvicorn orchestrator.main:app --host 0.0.0.0 --port 8000
```

Or let launchd handle it at boot — a plist in `~/Library/LaunchAgents/` keeps it running automatically.

### 2. Start workers (ThinkPad / Mac Mini)

```bash
uvicorn worker.main:app --host 0.0.0.0 --port 8001
```

On Ubuntu, a systemd user service handles auto-start:

```ini
[Unit]
Description=Infra Worker

[Service]
WorkingDirectory=/home/your-username/Projects/distributed-infra
ExecStart=/home/your-username/Projects/distributed-infra/.venv/bin/uvicorn worker.main:app --host 0.0.0.0 --port 8001
Environment=PATH=/home/your-username/.local/bin:/home/your-username/.npm-global/bin:/usr/local/bin:/usr/bin:/bin
Restart=always

[Install]
WantedBy=default.target
```

### 3. Launch the CLI

```bash
./da
```

```
╭──────────────────────────────────────────────────────────────────╮
│  Distributed Agents                                              │
│  macbook-pro  ·  mac-mini  ·  thinkpad-x1              │
│  2/2 workers online                                              │
╰──────────────────────────────────────────────────────────────────╯

  Type help for commands, exit to quit.

da ›
```

### 4. Check the fleet

```
da › status

  Machine               Role         Online   Active   Done   Failed   Top LLM
 ────────────────────────────────────────────────────────────────────────────────
  macbook-pro  orchestrator ✓        -        -      -        -
  mac-mini              worker       ✓        0        14     0        gemini (8)
  thinkpad-x1           worker       ✓        0        22     2        claude (15)
```

---

## How Each Machine Works Out Tasks

### MacBook Pro — Orchestrator Only

The MacBook runs the queue server and the `da` CLI. It doesn't execute tasks — it coordinates. This keeps your primary machine responsive for interactive work while workers carry the load.

When you push a task:
1. It lands in SQLite with status `pending`
2. A worker polls and sends `POST /tasks/claim`
3. The orchestrator marks it `in_progress` and returns the task
4. The worker executes, then calls `POST /tasks/{id}/complete` (or `/fail` or `/needs-human`)

You can also run agents directly on the MacBook using the `run` command — useful when you want a quick local answer without involving the queue.

### ThinkPad Ubuntu — Android & Backend Worker

Capabilities: `android_build`, `npm_build`, `git_pull`, `test_run`, `lint`, `run_script`, `agent_run`

When the ThinkPad claims an `agent_run` task, the handler routes to the right CLI:

```python
async def run(agent: str, prompt: str) -> dict:
    if agent == "claude":
        return await claude_agent.run(prompt)
    elif agent == "gemini":
        return await gemini_agent.run(prompt)
    elif agent == "codex":
        return await codex_agent.run(prompt)
    elif agent == "groq":        # Cursor Agent
        return await groq_agent.run(prompt)
```

Each agent module searches common install paths (Homebrew, npm-global, .local/bin), runs the CLI as an async subprocess, and returns structured JSON. Android builds run Gradle directly — the ThinkPad has the Android SDK, JDK 17, and the release keystore.

### Mac Mini Intel — iOS & Xcode Worker

Capabilities: `ios_build`, `xcode`, `flutter`, `cocoapods`, `swift`, `agent_run`

The Mac Mini handles everything Xcode-related. Flutter iOS builds, CocoaPods, IPA exports — all go here. The worker's PATH includes `/usr/local/share/flutter/bin` and `/usr/local/Cellar/cocoapods/1.16.2_2/bin` so the right tools are always found.

For a Flutter iOS build:
```bash
flutter build ios --no-codesign   # compile check
make build-ipa                    # signed, ready for TestFlight
```

---

## Mixing Agents

One of the more interesting possibilities is using different agents for different parts of the same workflow. A practical example:

```
da › run claude what's the best approach to caching Riverpod providers?
     # instant local answer while you keep working

da › assign add keepAlive to the stable providers in membership_provider.dart
     → mac-mini / gemini
     # Gemini implements it on the Mac Mini in the background

da › assign write tests for the new keepAlive behaviour
     → thinkpad / codex
     # Codex writes tests on the ThinkPad in parallel
```

Three agents, three machines, all working at the same time on related tasks. The results come back to the queue and you review them when you're ready.

The agents aren't interchangeable. Claude tends to reason through tradeoffs before writing code. Gemini moves fast and handles large context windows well. Codex is focused and literal — good for targeted edits. Cursor Agent is strongest when the task requires deep project context. Routing the right task to the right agent makes a noticeable difference in output quality.

---

## The `needs_human` State

This is the detail that makes the system practical rather than fragile.

If an agent hits something it can't resolve — a merge conflict, an ambiguous requirement, a test that only fails on device — it returns `needs_human` instead of failing or hallucinating through. The orchestrator marks the task accordingly.

```
da › review

  5 task(s) need your review

  a3f29c1d  thinkpad  claude
    notes:  merge conflict in auth_repository.dart
    prompt: refactor token refresh to use new endpoint
    resolve a3f29c1d done   # or: failed / pending
```

You look at it, resolve it, re-queue with more context if needed, and the workflow continues. The agents are delegates, not autonomous actors.

---

## Lessons from Building It

**Keep the queue simple.** SQLite is enough for a personal fleet. The async `aiosqlite` pattern works well — open a new connection per function call rather than reusing one across async contexts.

**PATH matters more than you think.** Launchd on macOS doesn't inherit your shell PATH. You have to specify every directory explicitly in the plist. Missing `/usr/sbin` breaks `sysctl`, which breaks the Gemini CLI. Missing the Flutter bin dir breaks iOS builds. Enumerate everything.

**Xcode is the hardest part.** Headless Xcode setup on a new Mac requires accepting the license (`sudo xcodebuild -license accept`), running first-launch setup (`sudo xcodebuild -runFirstLaunch`), and downloading platform SDKs through the Xcode GUI. None of it is fully automatable over SSH.

**Agents that know their limits are more useful.** The `needs_human` task state is the most important design decision. It's what separates "distributed agents" from "distributed hallucinations."

---

## What's Next

The natural extension is smarter routing — learning from history which agent produces the best results for which task types, and routing accordingly without needing explicit instructions.

After that: streaming task output, so you can watch a long agent run in real time. And eventually, agents that can push new tasks — letting one agent delegate sub-tasks to another across the fleet.

The stack is small enough to understand end-to-end and practical enough to use daily. That's the sweet spot.

---

*The full implementation covers ~1,200 lines of Python across the orchestrator, workers, and agent modules. Happy to share specifics on any component in the comments.*
