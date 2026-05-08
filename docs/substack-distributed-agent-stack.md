# I Built a Distributed AI Agent Stack Across Three Machines — And Then Taught It New Skills

*What happens when you stop treating AI agents as a single-machine tool, start treating them as a network — and give that network the ability to learn new capabilities on demand.*

---

Most developers run one AI agent at a time. One machine, one model, one conversation. It works — but it misses something fundamental: you probably already have more compute than you're using, and the agents you rely on most are available as CLI tools that can run anywhere.

That realisation turned into a weekend project that became something I now use every day. A distributed agent stack where my MacBook Pro orchestrates tasks, my ThinkPad Ubuntu handles Android and backend work, and my Mac Mini Intel handles iOS and Xcode builds — all connected over Tailscale, all running Claude, Gemini, Codex, and Cursor Agent as worker processes.

This post covers two things:

1. How the stack works and the two modes of operation that make it practical
2. How to add new capabilities to the system — the skills registry and why it matters

---

## Part One: The Stack

### The Setup

Three machines, one mesh VPN:

| Machine | Role | Specialisation |
|---|---|---|
| MacBook Pro (M-series) | Orchestrator | Task queue, `da` CLI, coordination |
| ThinkPad X1 (Ubuntu) | Worker | Android builds, backend dev, general coding |
| Mac Mini Intel | Worker | iOS/Xcode builds, Swift, Flutter iOS |

The network layer is Tailscale — each machine gets a static private IP, and they talk to each other as if they're on the same LAN. No port forwarding, no VPN config headaches.

The queue is a FastAPI server running on the MacBook (port 8000) backed by SQLite. Workers run their own FastAPI servers (port 8001) and poll the orchestrator every 10 seconds. When a task arrives that matches their capabilities, they claim it, execute it, and report back: `completed`, `failed`, or `needs_human`.

That last state — `needs_human` — is the one I'm most proud of. An agent that knows what it doesn't know is worth ten that hallucinate through the finish line.

---

### Two Modes of Working

The most useful design decision was exposing two distinct modes from a single interactive CLI: run a task locally on your MacBook right now, or push it to the queue and let a worker handle it in the background.

```
╭──────────────────────────────────────────────────────────────────╮
│  Distributed Agents                                              │
│  macbook-pro  ·  mac-mini  ·  thinkpad-x1              │
│  2/2 workers online                                              │
╰──────────────────────────────────────────────────────────────────╯

  Type help for commands, exit to quit.

da ›
```

**Local** — run on this machine, right now:

```
da › run claude explain this Riverpod provider pattern
da › run gemini summarise the last 10 commits
da › test                   # smoke-test all four agents
da › test codex             # test one agent
```

Local runs are synchronous — output streams directly to your terminal. Best for quick questions, code explanations, or anything where you want an immediate answer without context-switching.

**Queued** — send to a worker, pick it up when it's done:

```
da › assign review the auth module for security issues
     → Asking Claude for routing recommendation…
     → Suggested: ThinkPad / claude  (reasoning: code review, no build required)
     → Confirm? Y
     → ✓ Task queued  a3f29c1d  →  thinkpad / claude

da › queue
da › review       # tasks waiting for human action
```

Queued tasks are routed by Claude — it reads the description and matches it to the capabilities declared for each worker. You can override:

```
da › assign build the iOS app --machine=mac-mini --llm=gemini --type=ios_build
```

---

### Multiple Agents, Not Just Multiple Machines

The four agents aren't interchangeable. They have different characters:

- **Claude** — strongest at reasoning, code review, and longer context
- **Gemini** — research-heavy tasks, large documents, broad context windows
- **Codex** — quick, targeted code generation and edits
- **Cursor Agent** — workspace-aware tasks that need deep project context

Here's what it looks like in practice:

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

Three agents, three machines, all working at the same time on related tasks. The results land in the queue when you're ready to review them.

You can also mix modes: `run claude` locally for an instant architecture opinion, then `assign` the implementation to a worker so your MacBook stays free.

---

### The Queue View

```
da › queue

  ID        Type        Status       Machine       LLM      Task
 ─────────────────────────────────────────────────────────────────────
  a3f29c1d  agent_run   in_progress  thinkpad      claude   review auth...
  f7b2a391  ios_build   done         mac-mini      -        flutter build ios
  9c41d022  agent_run   pending      -             gemini   summarise changes
```

Tasks aren't limited to agent runs. The system handles:

- `git_pull` — sync a repo on a remote machine
- `android_build` — Gradle build on the ThinkPad (Android SDK + JDK 17)
- `ios_build` — Flutter/Xcode build on the Mac Mini (Xcode + CocoaPods)
- `run_script` — arbitrary shell scripts on the right machine
- `test_run` / `lint` — CI-style checks distributed across the fleet
- `human_action` — the agent is stuck and needs you

```
da › status

  Machine               Role         Online   Active   Done   Failed   Top LLM
 ────────────────────────────────────────────────────────────────────────────────
  macbook-pro  orchestrator ✓        -        -      -        -
  mac-mini              worker       ✓        0        14     0        gemini (8)
  thinkpad-x1           worker       ✓        0        22     2        claude (15)
```

---

### What I Had to Figure Out

A few things that aren't obvious when you're setting this up:

**Xcode on a headless Mac.** The Mac Mini was running Xcode 26.3 (beta) on macOS 15, and `xcodebuild` was crashing because `DVTDownloads.framework` wasn't present. The fix was opening Xcode.app once to trigger component installation, then downloading the iOS platform from Xcode → Settings → Platforms. Not automatable over SSH. Worth knowing before you rely on headless builds.

**SQLite and async.** The first version of the queue server had a subtle bug: it opened one `aiosqlite` connection and reused it across async calls. This caused "threads can only be started once" errors under load. Fix: open a fresh `async with aiosqlite.connect()` context per function call.

**Codex needs a git repo.** The newer version of Codex CLI only works when called from inside a git repository. The worker now sets `cwd` to the project root before invoking it.

**PATH matters more than you think in launchd.** macOS launchd doesn't inherit your shell PATH. Every directory your agents might live in — Homebrew, npm-global, .local/bin, /usr/sbin for system tools — has to be listed explicitly in the plist. Missing one causes silent failures.

---

## Part Two: Teaching the Stack New Skills

So the stack is running. Claude routes tasks, workers execute them, the queue tracks everything. But what happens when you want to do something the system doesn't know about yet?

That's where the skills system comes in.

### What a "Skill" Is

A skill has three layers:

1. **A registry entry** in `config/skills.yaml` — the source of truth. Describes the skill, how to install it, how to check if it's installed, and what task type it enables.

2. **A task handler** in `worker/handlers/<name>.py` — the Python function that actually executes the work when a task of that type arrives.

3. **A capability declaration** in `config/machines.yaml` — tells the orchestrator which machines can handle which task types.

When you add all three, the system gains a new capability end-to-end: Claude can route to it, workers can execute it, and the `skills` command can check and install it.

---

### The Skills Registry

`config/skills.yaml` is the central catalog — every known skill, its install recipe, and its check command:

```yaml
skills:

  flutter:
    description: "Flutter SDK for building cross-platform mobile apps"
    category: mobile
    check: "flutter --version"
    install:
      macos: "brew install --cask flutter"
      linux: "sudo snap install flutter --classic"
    task_types: [ios_build, android_build]
    handler: worker/handlers/ios.py

  claude:
    description: "Claude Code CLI — Anthropic's AI coding agent"
    category: ai-agent
    check: "claude --version"
    install:
      macos: "npm install -g @anthropic-ai/claude-code"
      linux: "npm install -g @anthropic-ai/claude-code"
    task_types: [agent_run]
    handler: worker/handlers/agent.py
```

The registry covers AI agents, mobile tools, backend runtimes, and infrastructure — 15 skills out of the box across four categories.

From the `da` CLI:

```
da › skills available

  AI AGENTS
    claude        ✓ mac-mini  ✓ thinkpad   Claude Code CLI — Anthropic's AI coding agent
    gemini        ✓ mac-mini  ✓ thinkpad   Gemini CLI — Google's AI coding agent
    codex         ✓ mac-mini  ✓ thinkpad   OpenAI Codex CLI
    cursor-agent  ✓ mac-mini  ✓ thinkpad   Cursor Agent CLI

  MOBILE
    flutter       ✓ mac-mini  ✓ thinkpad   Flutter SDK
    cocoapods     ✓ mac-mini  ✗ thinkpad   CocoaPods dependency manager
    xcode         ✓ mac-mini  ✗ thinkpad   Xcode — Apple's IDE
    android-sdk   ✗ mac-mini  ✓ thinkpad   Android SDK and build tools

  BACKEND
    node          ✓ mac-mini  ✓ thinkpad   Node.js runtime and npm
    python        ✓ mac-mini  ✓ thinkpad   Python 3 runtime

  INFRASTRUCTURE
    docker        ✓ mac-mini  ✓ thinkpad   Docker container runtime
    git           ✓ mac-mini  ✓ thinkpad   Git version control
```

This view SSHs into each worker in real time and runs the check command to show what's actually installed vs. missing. You can filter by category:

```
da › skills available --category=mobile
```

To install a missing skill on a specific machine:

```
da › skills install thinkpad cocoapods
  Installing cocoapods on thinkpad…
  $ brew install cocoapods

  ✓ cocoapods installed on thinkpad
```

---

### Creating a New Skill

This is where it gets interesting. Say you want to add a `deploy` skill — something that pushes a build to a server. The stack doesn't know about that yet.

```
da › skills create deploy
```

The CLI walks you through it:

```
  Creating new skill: deploy

  Description (one line): Deploy a build artifact to a remote server
  Category [custom]: infrastructure
  Check command [deploy --version]: rsync --version
  Install command (macos): brew install rsync
  Install command (linux): sudo apt-get install -y rsync
  Task type this enables [run_script]: deploy

  ✓ Created handler: worker/handlers/deploy.py
  ✓ Registered in skills.yaml

  Next steps:
  1. Implement worker/handlers/deploy.py
  2. Add to worker/handlers/__init__.py dispatch:
       if task.type == "deploy":
           from worker.handlers.deploy import handle_deploy
           return await handle_deploy(task)
  3. Add the capability to machines.yaml:
       skills add <machine> deploy
```

Two things just happened automatically:

**A handler file was scaffolded** at `worker/handlers/deploy.py`:

```python
"""Handler for deploy tasks."""
from __future__ import annotations

from shared.models import Task


async def handle_deploy(task: Task) -> dict:
    """
    payload:
      # TODO: document expected payload keys
    """
    # TODO: implement handler
    return {"needs_human": True, "notes": "handle_deploy not yet implemented"}
```

**The registry was updated** — `config/skills.yaml` now has a `deploy` entry with everything you just entered.

Now you implement the handler. A real deploy skill might look like this:

```python
"""Handler for deploy tasks."""
from __future__ import annotations

import asyncio
import shlex

from shared.models import Task


async def _run(cmd: str, cwd: str | None = None) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode(), stderr.decode()


async def handle_deploy(task: Task) -> dict:
    """
    payload:
      artifact: str   — local path to the build artifact
      target: str     — user@host:/path on the deploy server
      timeout: int    — seconds (default 120)
    """
    artifact = task.payload.get("artifact", "")
    target   = task.payload.get("target", "")
    timeout  = int(task.payload.get("timeout", 120))

    if not artifact or not target:
        return {"needs_human": True, "notes": "artifact and target are required"}

    cmd = f"rsync -avz --progress {shlex.quote(artifact)} {shlex.quote(target)}"

    try:
        rc, out, err = await asyncio.wait_for(_run(cmd), timeout=timeout)
    except asyncio.TimeoutError:
        return {"needs_human": True, "notes": f"Deploy timed out after {timeout}s"}

    if rc != 0:
        return {"needs_human": True, "notes": f"rsync failed: {err[-500:]}"}

    return {"status": "deployed", "target": target, "stdout": out[-1000:]}
```

Wire it into the dispatch in `worker/handlers/__init__.py`:

```python
if task.type == "deploy":
    from worker.handlers.deploy import handle_deploy
    return await handle_deploy(task)
```

Register it on the machines that can run it:

```
da › skills add thinkpad deploy
  ✓ Added capability 'deploy' to thinkpad
```

And now:

```
da › assign deploy the Android APK to the staging server
     → Asking Claude for routing recommendation…
     → Suggested: ThinkPad / claude  (reasoning: deploy capability, artifact available)
     → Confirm? Y
     → ✓ Task queued  b7c14e2a  →  thinkpad / claude
```

The system knows how to route it, the worker knows how to execute it, and `skills available` will show its install status going forward.

---

### Auto-Discovery for Custom Handlers

One more detail worth knowing: the worker dispatch includes auto-discovery for custom skill handlers. If you've created `worker/handlers/deploy.py` but haven't wired it into `__init__.py` yet, the dispatch will still find it — it looks for `handle_<task_type>` in a matching file.

```python
# Auto-discovery for custom skills
handler_file = _HANDLERS_DIR / f"{task.type}.py"
if handler_file.exists():
    module = importlib.import_module(f"worker.handlers.{task.type}")
    fn = getattr(module, f"handle_{task.type}", None)
    if callable(fn):
        return await fn(task)
```

This means scaffolding a handler with `skills create` and implementing it is enough to make it work — no manual dispatch wiring required. Explicit wiring is still recommended for built-in task types (faster, no import overhead), but for custom skills the auto-discovery is a useful shortcut.

---

## Lessons

**Keep the queue simple.** SQLite is enough for a personal fleet. The async `aiosqlite` pattern works well — one new connection per function call, not reused across async contexts.

**Agents that know their limits are more useful.** The `needs_human` task state is the most important design decision. It separates "distributed agents" from "distributed hallucinations." An agent that stops and waits when it's uncertain is worth far more than one that guesses.

**The skills system makes the stack extensible without touching core code.** Adding a new capability means writing a handler, adding a YAML entry, and declaring the capability on the right machine. The orchestrator, queue, and routing all pick it up automatically.

**The psychological shift matters.** When you have a distributed agent stack, you stop thinking about AI as a tool you use interactively and start thinking about it as infrastructure you delegate to. The agents aren't competing — they're complementary, and the infrastructure makes using all of them feel natural.

---

## What's Next

**Smarter routing.** Right now Claude routes based on declared capabilities. A better version would learn from history — which agent consistently produces the best results for which task types — and route accordingly.

**Task streaming.** Right now you get the full output only when a task completes. For long agent runs, a real-time stream would be more useful than polling.

**Agent-to-agent delegation.** The natural next step is agents pushing sub-tasks to other agents. One agent delegates a subtask across the fleet — the system starts to look less like infrastructure and more like an actual team.

---

*The full implementation is roughly 1,200 lines of Python across the orchestrator, workers, agent modules, and skill handlers. Happy to share specifics on any part in the comments.*
