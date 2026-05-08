# LinkedIn Post

---

Most developers use AI coding agents one at a time. One machine, one model, one conversation.

I've been running an experiment: what if you treated them as a fleet?

**Three machines. Four AI agents. Two ways to work.**

- MacBook Pro acts as the orchestrator — lightweight queue server and an interactive CLI
- ThinkPad Ubuntu handles Android builds, backend work, and general coding tasks  
- Mac Mini Intel handles iOS/Xcode builds and Flutter compilation
- Claude, Gemini, Codex, and Cursor Agent run as worker processes across all three

The interesting part isn't the infrastructure — it's the two modes it unlocks.

**Local**: ask an agent directly on your machine, get an instant answer, stay in flow.

**Queued**: describe a task, Claude recommends which machine and which agent should handle it, it runs in the background while you work on something else.

You end up with Claude answering a quick architecture question locally, while Gemini implements a feature on the Mac Mini, while Codex writes tests on the ThinkPad — all at the same time. The agents aren't competing. They're complementary, and the right infrastructure makes using all of them feel natural.

The thing that surprised me most was the `needs_human` task state — when an agent hits something ambiguous, it stops and waits rather than guessing. That single design decision is what makes the whole system feel like delegation rather than automation.

Wrote up the full experiment on Substack: architecture, the `da` CLI, how each machine handles tasks, and what I'd change. Link in the comments.

#AIEngineering #DeveloperTools #Claude #Gemini #OpenAI #DistributedSystems #SoftwareDevelopment #BuildingInPublic
