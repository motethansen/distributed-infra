# distributed-infra — Roadmap

Living roadmap for new agents and capabilities. Each track lists the smallest valuable slice, open decisions, and dependencies. Order in this file is **priority order** — top is up next.

---

## Conventions

- **Smallest slice** = the minimum scope that delivers user-visible value. Everything past that is iteration.
- **Status:** `idea` (unscoped) · `planned` (slice agreed, not started) · `in-progress` · `shipped`.
- Tasks track via commits + the `da` CLI queue; this file captures intent, not day-to-day work.

---

## 1 — AI assistant integration  ·  `planned`

Expose the `ai_agent_assistant` project as a worker capability on MacBook Pro so personal-productivity commands can be sent from WhatsApp self-chat.

**Integration boundary:** thin adapter only. `ai_agent_assistant` stays in its own repo; distributed-infra invokes it via subprocess and treats it as a black box.

**Smallest slice (v1):**
- MBP runs a worker process (port 8001) alongside the orchestrator, declared in `config/machines.yaml` with `capabilities: [assistant_run]`. Process managed by a new launchd plist `com.techstartups.worker.plist` (KeepAlive on, mirrors the Mac Mini pattern).
- New task type `assistant_run` with payload `{subcommand, args}`. Worker handler runs the assistant via **its own venv python**: `/Users/michaelhansen/Projects/github/ai_agent_assistant/.venv/bin/python main.py --<flag>`. Stdout returned as the task result.
- WhatsApp bridge gets one new command `assist <subcommand> [args]`. v1 subcommands: `today`, `sync`, `status`, `plan [today|week]`.
- Reply: assistant's stdout, truncated to ~1400 chars, sent to self-chat.

**Out of scope for v1:** freeform chat (`assist chat <prompt>`) — depends on the assistant gaining a non-interactive single-prompt mode. Event-driven triggers (assistant pushing notifications), shared SQLite state, repo merge.

**Out of scope for v1:** event-driven triggers (assistant pushing notifications), shared SQLite state, merging the two repos.

**Unlocks:** Tracks #3 and #4 sit on top of the assistant's existing `notes_agent` and `knowledge_agent`.

---

## 2 — Stock market agents (alerts-only)  ·  `idea`

Scheduled tasks that pull market data and surface signals via WhatsApp. **No order routing in v1** — alerts only, to derisk.

**Smallest slice (v1):**
- New task type `market_brief` triggered by cron on the orchestrator at market open + close.
- Pulls quotes for a small watchlist (yfinance, free).
- Computes 2–3 well-understood signals (e.g. RSI(14), 50/200 MA cross, gap-up/down > X%).
- Sends a single WhatsApp message: ticker · last · 1d% · which signal fired.

**Open decisions:**
- Universe: personal watchlist file in `config/watchlist.yaml`, S&P sector ETFs, or both?
- Data source: stick to yfinance (free, occasional flaky), or budget for Alpaca/polygon.io?
- Worker placement: orchestrator-as-cron, dedicated worker on Mac Mini, or new "trading worker" machine?
- Signal/strategy split: keep dumb-but-readable Python at v1 vs. pull in an LLM for "explain the move"?

**Risk gates (must satisfy before any execution-mode v2):** max daily loss limit, max position size, kill switch via WhatsApp command, paper-trading dry-run period.

**Independent of** #1, #3, #4 — can run in parallel.

---

## 3 — Academic research agent  ·  `idea`

Pull recent papers on a topic, summarise abstracts, write a structured note into Obsidian. Extension of the assistant's `notes_agent` / `knowledge_agent` rather than a new top-level agent.

**Smallest slice (v1):**
- New `ai_agent_assistant` subcommand: `python main.py --research <topic>`.
- Queries arXiv + Semantic Scholar APIs (both free), takes top N=5 by recency × citations.
- Writes a markdown note to `Obsidian/Resources/research/<slug>.md` with title, authors, abstract, link, BibTeX.
- Surfaces via the same `assist research <topic>` bridge command added in #1.

**Open decisions:**
- Research areas to wire by default: AI/ML (arXiv cs.LG, cs.AI), quant finance (arXiv q-fin), other?
- Abstract-only vs. PDF download + summarisation in v1?
- Citation graph (follow refs) — feature or out of scope?

**Depends on:** #1 (uses the same subprocess adapter).

---

## 4 — Writing agents (Medium + Substack)  ·  `idea`

From a topic + your recent activity, draft a long-form post matching your existing voice. **Stop at draft** in v1 — no auto-publish, no API integration.

**Smallest slice (v1):**
- Bridge command `assist draft <topic>` → assistant subcommand → output written to `Obsidian/Inbox/drafts/<slug>.md`.
- Inputs to the prompt: recent git commits across watched repos, recent research notes (from #3), the existing posts in `docs/*.md` as voice exemplars.
- Single draft per call, no platform-specific render yet.

**Open decisions:**
- One agent + two render passes (Medium vs. Substack formatting), or two agents?
- Voice-tuning: zero-shot off existing posts, or pre-build a "style card" from `docs/medium-distributed-agent-stack.md` + `docs/substack-distributed-agent-stack.md`?
- Eventual publishing: Medium API (drafts only) feasible; Substack is email/RSS so likely manual indefinitely.

**Depends on:** #1, ideally #3 for research-grounded posts.

---

## Sequencing summary

```
#1  AI assistant integration   ──┬──→  #3  Research agent
                                 └──→  #4  Writing agents
#2  Stock alerts                 ─── independent track ───
```

**Recommendation:** ship #1 first; pick up #2 in parallel only if there's appetite (it's a sizable independent track). #3 → #4 follow naturally once #1 lands.
