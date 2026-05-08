# I Built a Distributed AI Agent Stack Across Three Machines — Here's What I Learned

*What happens when you stop treating AI agents as a single-machine tool and start treating them as a network?*

---

Most developers run one AI agent at a time. One machine, one model, one conversation. It works — but it misses something fundamental: you probably already have more compute than you're using, and the agents you rely on most are available as CLI tools that can run anywhere.

That realisation turned into a weekend project that became something I now use every day. A distributed agent stack where my MacBook Pro orchestrates tasks, my ThinkPad Ubuntu handles Android and backend work, and my Mac Mini Intel handles iOS and Xcode builds — all connected over Tailscale, all running Claude, Gemini, Codex, and Cursor Agent as worker processes.

Here's how it works, what surprised me, and why I think this is closer to how AI-assisted development should look.

---

## The Setup

Three machines, one mesh VPN:

| Machine | Role | Specialisation |
|---|---|---|
| MacBook Pro (M-series) | Orchestrator | Task queue, CLI, coordination |
| ThinkPad X1 (Ubuntu) | Worker | Android builds, backend dev, general coding |
| Mac Mini Intel | Worker | iOS/Xcode builds, Swift, Flutter iOS |

The network layer is Tailscale — each machine gets a static private IP, and they talk to each other as if they're on the same LAN. No port forwarding, no VPN config headaches.

The queue is a FastAPI server running on the MacBook (port 8000) backed by SQLite. Workers run their own FastAPI servers (port 8001) and poll the orchestrator every 10 seconds. When a task arrives that matches their capabilities, they claim it, execute it, and report back: `completed`, `failed`, or `needs_human`.

That last state — `needs_human` — is the one I'm most proud of. An agent that knows what it doesn't know is worth ten that hallucinate through the finish line.

---

## Many Agents, Many Modes

One of the most interesting design decisions was how to handle the agents themselves. Every major AI coding tool now ships a CLI — Claude Code, Gemini CLI, OpenAI Codex, Cursor Agent — and all of them can run headlessly via a single flag:

```
claude -p "your prompt here"
gemini -p "your prompt here"
agent -p "your prompt here" --trust
codex exec "your prompt here"
```

The distributed stack wraps each one. But more importantly, it opens up two distinct modes of working:

**Run locally on the MacBook** — no queue, instant response, great for interactive tasks where you want to stay in flow:

```bash
da › run claude explain the Riverpod keepAlive pattern
da › run gemini summarise the last 10 commits
da › test                    # smoke-test all four agents
```

**Push to a worker** — the task lands in the queue, the right machine picks it up, and you get the result without occupying your primary machine:

```bash
da › assign build the iOS app and run flutter analyze
      → Claude suggests: mac-mini / gemini
      → Confirm? Y  →  queued
```

The choice between them is natural: quick questions stay local, heavy or hardware-specific work goes to workers. You never have to think about which machine has Xcode or the Android SDK — the capabilities are declared in config and the router handles it.

The reason for four agents isn't redundancy. Each has a different character:

- **Claude** is strongest at reasoning, code review, and longer context tasks
- **Gemini** handles research-heavy tasks and large documents well
- **Codex** (OpenAI's coding agent) is good for quick, targeted code generation
- **Cursor Agent** integrates with project context when you want something workspace-aware

You can also mix and match: run Claude locally for an immediate answer, then push a deeper version of the same question to Gemini on the ThinkPad while you get on with the next thing.

---

## What Tasks Look Like

From the interactive CLI (`da`), you describe what you want:

```
da › assign review the Stripe webhook handler for edge cases
     → Asking Claude for routing recommendation…
     → Suggested: ThinkPad / claude (reasoning: code review, no build required)
     → Confirm? Y
     → ✓ Task queued  a3f29c1d  →  thinkpad / claude
```

No flags, no JSON payloads. Claude reads the task description and decides which machine and which agent should handle it based on declared capabilities.

The queue view shows where everything is and who's working on it:

```
da › queue

  ID        Type        Status       Machine      LLM      Task
 ─────────────────────────────────────────────────────────────────────
  a3f29c1d  agent_run   in_progress  thinkpad     claude   review Stripe...
  f7b2a391  ios_build   done         mac-mini     -        flutter build ios
  9c41d022  agent_run   pending      -            gemini   summarise changes
```

Tasks aren't limited to agent runs. The system handles:

- `git_pull` — sync a repo on a remote machine
- `android_build` — trigger a Gradle build on the ThinkPad
- `ios_build` — trigger a Flutter/Xcode build on the Mac Mini
- `run_script` — run an arbitrary shell script on the right machine
- `test_run` / `lint` — CI-style checks distributed across the fleet
- `human_action` — the agent is stuck and needs you

---

## The Machines Actually Working Together

Here's a real example of the stack doing something useful.

I'm working on a Flutter app targeting both iOS and Android. I push four tasks from the MacBook:

1. `git_pull` → all workers sync the repo
2. `android_build` → ThinkPad compiles the APK (it has the Android SDK and JDK 17)
3. `ios_build` → Mac Mini builds `Runner.app` (it has Xcode and CocoaPods)
4. `agent_run` (claude) → ThinkPad reviews the diff and flags anything worth looking at

All four run in parallel. The MacBook isn't doing any of the heavy lifting — it's just watching the queue. Ten minutes later I have build artifacts and a code review sitting in the results, and I haven't opened a single terminal on the remote machines.

---

## What I Had to Figure Out

A few things that aren't obvious:

**Xcode 26 beta and missing frameworks.** The Mac Mini was running Xcode 26.3 (beta) on macOS 15, and `xcodebuild` was crashing because `DVTDownloads.framework` wasn't present. The fix was opening Xcode.app once to trigger component installation, then downloading the iOS 26.2 platform from Xcode → Settings → Platforms. Not automatable over SSH. Worth knowing before you rely on headless Xcode.

**SQLite and async.** The first version of the queue server had a subtle bug: it opened one aiosqlite connection and reused it across async calls. This caused "threads can only be started once" errors under load. Fix: open a fresh `async with aiosqlite.connect()` context per function call. Simple, but easy to get wrong.

**Codex needs to run from a git repo.** The newer version of Codex CLI only works when called from inside a git repository. The worker now sets `cwd` to the project root before invoking it.

**The Cursor Agent doesn't need an API key.** It uses your logged-in session. Just `agent login` once per machine, no environment variables needed.

**PATH matters more than you think in launchd.** macOS launchd doesn't inherit your shell PATH. Every directory your agents might live in — Homebrew, npm-global, .local/bin, /usr/sbin for system tools — has to be listed explicitly in the plist. Missing one will cause silent failures that are hard to debug.

---

## Auto-Start on Every Machine

The workers start automatically at boot:

- **macOS (MacBook + Mac Mini):** launchd plists in `~/Library/LaunchAgents/`
- **Ubuntu (ThinkPad):** systemd user service (`~/.config/systemd/user/infra-worker.service`)

Once it's set up, the fleet is just always there. You open the `da` CLI and everything is already running.

---

## What I'd Change

The current setup is intentionally simple — SQLite is not a production message queue, and there's no retry logic beyond re-queuing manually. For a team, you'd want proper job persistence and dead-letter handling.

I'd also add task streaming. Right now you get the full output only when the task completes. For long agent runs, watching a stream would be more useful than polling.

The most interesting extension is smarter agent selection. Right now you pick the agent explicitly or Claude routes based on capability matching. A better version would learn from history: which agent consistently produces the best results for which task types, and route accordingly. That starts to look less like infrastructure and more like an actual team.

---

## The Bigger Point

The real shift is psychological. When you have a distributed agent stack, you stop thinking about AI as a tool you use interactively and start thinking about it as infrastructure you delegate to. The MacBook becomes a control plane. The agents — local and remote, online and offline-capable — become a workforce.

You can run Claude locally for a quick answer. You can push a harder problem to Gemini on a different machine. You can have Codex and Cursor Agent working in parallel on the ThinkPad while you're reading the iOS build logs from the Mac Mini. The agents aren't competing — they're complementary, and the infrastructure makes using all of them as natural as picking the right tool from a shelf.

For solo development across multiple machines, it's made a real difference to how I work. The floor of what's feasible in a day has gone up, not because any individual agent is smarter, but because they're running in parallel on the right hardware.

The stack is open — I'll share the repo once I've cleaned it up a bit. In the meantime, everything you need to build your own is in this post.

---

*Questions or building something similar? Reply below.*
