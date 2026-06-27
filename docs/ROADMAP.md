# distributed-infra — Roadmap

Living roadmap for new agents and capabilities. Each track lists the smallest valuable slice, open decisions, and dependencies. Order in this file is **priority order** — top is up next.

---

## Conventions

- **Smallest slice** = the minimum scope that delivers user-visible value. Everything past that is iteration.
- **Status:** `idea` (unscoped) · `planned` (slice agreed, not started) · `in-progress` · `shipped`.
- Tasks track via commits + the `da` CLI queue; this file captures intent, not day-to-day work.

---

## Architecture reality (read this before planning autonomy)

`distributed-infra` is a **distributed task queue with a worker fleet — not (yet) a multi-agent reasoning system.** The distinction drives every track below.

**What the code is today:**

- **Control:** centralized. One orchestrator (`orchestrator/main.py`, MacBook) owns a SQLite queue; workers poll `/tasks/claim` every 10s (`worker/poller.py`). `claim_next_task` (`db.py:101`) is pure SQL — priority DESC, created_at ASC, capability + `_target_machine` filtering. Routing is fully deterministic.
- **"Agents":** thin subprocess wrappers (`agents/claude_agent.py` shells out to `claude -p`, plus codex/groq/agy/content/social). They execute; they do **not** decompose, delegate, or call each other. The human is the planner today (via `da` CLI / WhatsApp self-chat).
- **Orchestration:** `worker/handlers/dispatch()` auto-discovers `handle_<type>(task)` by filename. A dispatcher, not an inter-agent hand-off mechanism.
- **Memory:** durable task state in SQLite (good short-term/state persistence). Claude multi-turn via `--session-id`/`--resume`. No vector DB, no cross-agent message history — and that's deliberate (the ChromaDB layer was cut from `ai_agent_assistant` for being over-engineered).
- **Failure handling:** per-call timeouts (1800s default), try/except → `fail` or `needs_human` escalation with a macOS notification (`main.py:165`). A real human-in-the-loop gate already exists.

**Proposed multi-agent layers/patterns, mapped to reality:**

| Proposed layer / pattern | Status in code | Gap |
|---|---|---|
| Control Layer (centralized/hier/decentral) | ✅ Centralized, clean | No hierarchy among agents — only human→queue→worker |
| Agent Layer (specialized, scoped, tool-first) | ⚠️ Partial — capability handlers, not reasoning agents | Agents shell out to CLIs (prompt-based), not native structured tool-calling |
| Orchestration Layer (comms, state, hand-offs) | ⚠️ Dispatch only | No agent→agent hand-off, no state machine across steps |
| Memory Layer (vector + history) | ⚠️ State yes, semantic no | No long-term/vector; intentionally so |
| Hierarchical Supervisor | ❌ Absent | The headline gap if you want autonomy |
| Plan-and-Execute / Re-planner | ❌ Absent | Tasks are human-decomposed |
| Debate/Adversarial | ❌ (seed: `code_review` handler) | Single-shot, no consensus loop |
| Model Routing | ✅ Best-developed principle | `claude_agent` blocks Opus, defaults Sonnet, allows Haiku — generalize across providers (Track #5) |
| Explicit (deterministic) Control Logic | ✅ Strong — SQL routing | Keep it; best asset |
| Circuit Breakers | ⚠️ Timeouts only | No token/step budget for autonomous loops |
| State Persistence | ✅ SQLite | No per-step plan state (no steps table) |
| Self-Correction / Validator | ❌ | `code_review` is the seed of a validator-in-loop |
| Observability | ❌ Weakest area | Logging only; no trace/graph |

**Key insight:** You don't need LangGraph/LangSmith/a vector DB to get a Hierarchical Supervisor — the stack already has the primitive. A `claude_agent` runs with `--dangerously-skip-permissions` and can hit the queue API; give it the `da` CLI / `POST /tasks` as a tool and it becomes a manager that decomposes a request into sub-tasks and enqueues them for specialist workers. This reuses everything already built (capabilities, `_target_machine`, `needs_human`, model routing) instead of bolting on a parallel framework.

**Warning:** `ai_agent_assistant` was torn down precisely because it ran n8n + ChromaDB + LangChain + Docker for a solo tool. The proposal's reflexes — vector databases, LangSmith, observability platforms, "state machine for every decision" — are that same failure mode. **Adopt these per concrete use case, not as a platform.**

**Pattern → track fit:**

- **#2 Stock alerts** already has risk gates + kill switch + paper-trading = circuit-breakers + HITL gates. Stay deterministic-Python; do **not** add an LLM orchestrator here.
- **#3 Research + #4 Writing** are the genuine fit for **Plan-and-Execute** (planner → executor → validator). This is where reasoning autonomy pays off (Track #8).
- **#1 Assistant integration** stays the thin subprocess adapter. No pattern needed.

---

## Agent catalog (at a glance)

The wish-list is **not** N separate agents — it's **5 specialist domains + 1 concierge + 1 bridge**, each scoped tight (the "atomic objectives" principle). The clean rule across all of them: **search/read = autonomous; order/trade/send = `needs_human` gate** (`escalate_task`, `main.py:165` — no new machinery needed).

| Domain | Task types | Data source (reality) | Mode | HITL | Track |
|---|---|---|---|---|---|
| Concierge / router | `find` | — (classifies + fans out) | reason | no | #9 |
| Commerce search | `shop_search` (`source=amazon_sg\|lazada\|redmart`) | PA-API / Lazada API (Redmart = Lazada SG) | read | no | #10 |
| Commerce action | `shop_cart`, `shop_order` | web-access agent (Playwright) | act | **yes** | #11 |
| Weather | `weather` | Open-Meteo (free, no key) | read | no | #12 |
| Finance | `market_brief`, `portfolio` | yfinance, IBKR, Saxo OpenAPI | read → act | trades = **yes** | #2, #13 |
| Email | `email_lookup` | Gmail API | read | send = **yes** | #14 |
| Calendar | `calendar` | Google Calendar API | read | edits = **yes** | #14 |
| Social | `social_read`, `social_reply` | Mastodon API (clean); LinkedIn (web, fragile) | read → act | reply/post = **yes** | #16 |
| Assistant bridge | `assistant_run` | `ai_agent_assistant` subprocess | read/write | no | #1 |

**Cross-cutting conventions (bake in from the first agent):**
- **Secrets:** every new source = its own OAuth/API key. Centralize per-machine in `.env` (never the repo), same as `SECRET_KEY`.
- **SG-localization:** SGD pricing, GST awareness, `.sg` domains — put these in the normalized result schema now, not later.
- **Privacy class (#5 routing):** email / finance / calendar / personal content is never routed to DeepSeek or any cloud-CN endpoint — **Claude only** for now (cloud), until a dedicated LLM machine exists (#7).
- **Connectors ≠ fleet creds:** IBKR + Gmail are reachable as connectors in dev sessions (handy for prototyping #6/#13/#14 reads), but won't survive headless/cron fleet runs — the fleet versions still need their own API creds.

---

## 1 — AI assistant integration  ·  `shipped`

Expose the `ai_agent_assistant` project as a worker capability on MacBook Pro so personal-productivity commands can be sent from WhatsApp self-chat.

**Integration boundary:** thin adapter only. `ai_agent_assistant` stays in its own repo; distributed-infra invokes it via subprocess and treats it as a black box.

**Smallest slice (v1):**
- MBP runs a worker process (port 8001) alongside the orchestrator, declared in `config/machines.yaml` with `capabilities: [assistant_run]`. Process managed by a new launchd plist `com.techstartups.worker.plist` (KeepAlive on, mirrors the Mac Mini pattern).
- New task type `assistant_run` with payload `{subcommand, args}`. Worker handler runs the assistant via **its own venv python**: `/Users/michaelhansen/Projects/github/ai_agent_assistant/.venv/bin/python main.py --<flag>`. Stdout returned as the task result.
- WhatsApp bridge gets one new command `assist <subcommand> [args]`. v1 subcommands: `today`, `sync`, `status`, `plan [today|week]`.
- Reply: assistant's stdout, truncated to ~1400 chars, sent to self-chat.

**Out of scope for v1:** freeform chat (`assist chat <prompt>`) — depends on the assistant gaining a non-interactive single-prompt mode. Event-driven triggers, shared SQLite state, repo merge.

**Unlocks:** Tracks #3, #4, and the email/Obsidian wishes (#14) sit on top of the assistant's existing `notes_agent` / `knowledge_agent`.

---

## 2 — Stock market agents (alerts-only)  ·  `idea`

Scheduled tasks that pull market data and surface signals via WhatsApp. **No order routing in v1** — alerts only, to derisk.

**Smallest slice (v1):**
- New task type `market_brief` triggered by cron on the orchestrator at market open + close.
- Pulls quotes for a small watchlist (yfinance, free).
- Computes 2–3 well-understood signals (e.g. RSI(14), 50/200 MA cross, gap-up/down > X%).
- Sends a single WhatsApp message: ticker · last · 1d% · which signal fired.

**Open decisions:**
- Universe: personal watchlist in `config/watchlist.yaml`, S&P sector ETFs, or both?
- Data source: yfinance (free, occasionally flaky) vs. Alpaca/polygon.io.
- Worker placement: orchestrator-as-cron, dedicated Mac Mini worker, or a new "trading worker".
- Signal split: dumb-but-readable Python at v1 vs. LLM "explain the move".

**Risk gates (must satisfy before any execution-mode v2):** max daily loss limit, max position size, kill switch via WhatsApp command, paper-trading dry-run period.

**Independent of** #1, #3, #4. Extended into a real portfolio/brokerage track by #13.

---

## 3 — Academic research agent  ·  `idea`

Pull recent papers on a topic, summarise abstracts, write a structured note into Obsidian. Extension of the assistant's `notes_agent` / `knowledge_agent`.

**Smallest slice (v1):**
- New `ai_agent_assistant` subcommand: `python main.py --research <topic>`.
- Queries arXiv + Semantic Scholar APIs (both free), top N=5 by recency × citations.
- Writes a markdown note to `Obsidian/Resources/research/<slug>.md` (title, authors, abstract, link, BibTeX).
- Surfaces via the `assist research <topic>` bridge command from #1.

**Open decisions:** default areas (cs.LG/cs.AI, q-fin, …); abstract-only vs. PDF summarisation; citation-graph follow.

**Depends on:** #1 (same subprocess adapter). First real consumer of the Plan-and-Execute foundation (#8).

---

## 4 — Writing agents (Medium + Substack)  ·  `idea`

From a topic + recent activity, draft a long-form post matching your voice. **Stop at draft** in v1 — no auto-publish.

**Smallest slice (v1):**
- Bridge command `assist draft <topic>` → assistant subcommand → output to `Obsidian/Inbox/drafts/<slug>.md`.
- Prompt inputs: recent git commits across watched repos, recent research notes (#3), existing posts in `docs/*.md` as voice exemplars.
- Single draft per call, no platform-specific render yet.

**Open decisions:** one agent + two render passes vs. two agents; zero-shot voice vs. pre-built "style card".

**Depends on:** #1, ideally #3. Second consumer of the Validator loop (#8) — draft checked against voice exemplars before `needs_human`.

---

## 5 — Model routing layer (multi-provider)  ·  `shipped` (2026-06-27)

**Shipped:** `config/routing.yaml` policy + `agents/router.py` `route(task_kind, sensitivity, agent, model)`; wired into `runner.run_agent` (opt-in via `task_kind`/`sensitivity`, backward-compatible) and the `agent_run` payload. Hard privacy guard verified end-to-end (private work never reaches DeepSeek). Local model (#7) slots into the `privacy`/`mechanical` classes when it lands.


Generalize the cost/privacy logic already in `claude_agent` into a single router across **all** providers, so every agent call picks the cheapest model that fits.

**Smallest slice (v1):**
- A `route(task_kind, sensitivity) -> (agent, model)` policy table, read from `config/routing.yaml`.
- Default policy:
  - `privacy` (email, finance, personal) → `claude` (cloud) — never DeepSeek/cloud-CN. *(Routes to local once #7 lands.)*
  - `cheap-reasoning / bulk` → **deepseek** (#6).
  - `coding / planning` → `claude` sonnet.
  - `mechanical / classify / reformat` → `haiku` (cheap cloud) — local once #7 lands.
- `runner.run_agent()` consults the policy when caller passes `task_kind` instead of a hard-coded agent.

**Keeps the existing guardrail:** Opus stays blocked (`BLOCKED_MODEL_SUBSTRINGS`); per-call override still wins.

**Unlocks:** #6 plugs in as a routing target; #7 (local) slots into the `privacy` + `mechanical` classes when a dedicated machine arrives. The concierge (#9) routes classification to Haiku cheaply until then.

---

## 5b — Workload placement & overflow (Mac Mini primary)  ·  `shipped` (2026-06-27)

**Shipped:** opt-in soft preference in `claim_next_task` (`db.py`) + `OVERFLOW_GRACE_SECS` (default 20); orchestrator injects `_preferred_machine=mac-mini` for agent-style task types only (builds stay unpreferred → immediate); poller `MAX_CONCURRENT` cap (set to 4 on mac-mini + thinkpad). Verified live: agent tasks land on mac-mini immediately; `_preferred_machine` elsewhere routes there; an un-preferred machine overflows after the grace window (~28s incl. poll). v2 (liveness-aware/tiered) still open below.


Run agents **primarily on the Mac Mini**, spilling over to ThinkPad / MacBook Pro only when the Mini is busy or offline. Keep it deterministic and pull-based — no scheduler service, no message broker. This is the "orchestration feature" the fleet needs once most agent work lands on one preferred box.

**Model — soft preference + time-based overflow, all in the existing claim SQL:**
- New optional payload key `_preferred_machine` (default `mac-mini`, from `DEFAULT_PREFERRED_MACHINE`). Distinct from the existing **hard** pin `_target_machine`.
- `claim_next_task(worker, caps)` (`db.py:101`) lets a worker claim a task when **any** holds:
  - `_target_machine == worker` (existing hard pin), or
  - no preference set, or
  - `_preferred_machine == worker` — the primary claims **immediately**, or
  - `_preferred_machine != worker` **and** the task has been pending ≥ `OVERFLOW_GRACE_SECS` (e.g. 20s) — the Mini didn't grab it → **overflow** to a free worker.
- **Worker concurrency cap:** the poller (`worker/poller.py`) skips claiming when `len(active_tasks) >= MAX_CONCURRENT` (per-machine env). This is what makes the Mini "full" so tasks age past the grace window and overflow.

**Why it works:** the Mini polls every 10s, so a 20s grace gives it ~2 cycles of first refusal. If it's saturated (cap reached) or offline (not polling), tasks naturally age out and a free worker claims them — **automatic failover, no health-check logic in v1.**

**Config:**
- `machines.yaml`: per-machine `max_concurrent` (Mini high; ThinkPad/MBP modest); global `default_preferred_machine: mac-mini`.
- `.env`: `MAX_CONCURRENT`, `OVERFLOW_GRACE_SECS`.

**Open decisions / v2:**
- Liveness-aware overflow: skip the grace wait when the orchestrator already knows the Mini is offline (it tracks `_last_seen`, `main.py`) — faster failover.
- Tiered overflow (ThinkPad before MBP) vs. open overflow to any capable worker (**v1 = open**).
- Default preference per task-type: only `agent_run` / `assistant_run` prefer the Mini; builds stay pinned by capability anyway.

**Depends on:** nothing — pure extension of `claim_next_task` + the poller. Foundational; lands in Sprint 1 alongside the router.

---

## 6 — DeepSeek agent (API)  ·  `idea`

Add DeepSeek as a cheap reasoning/coding provider. **API, not CLI** — no dependable first-party CLI; the API is OpenAI-compatible, so the agent is a near-copy of `groq_agent.py`.

**Smallest slice (v1):**
- `agents/deepseek_agent.py`: OpenAI-compatible client, `base_url=https://api.deepseek.com`, `DEEPSEEK_API` in `.env`. Models `deepseek-chat` (V3) and `deepseek-reasoner` (R1).
- Register `"deepseek"` in `runner.py` `AGENTS` + `--agent` choices; add to `machines.yaml` `agents:` on the workers.
- Smoke-test via `runner.py --test`.

**Privacy caveat (hard rule):** DeepSeek API is China-hosted. **Never** route email/finance/personal-data tasks (#6 finance content, #7, assistant data) to it — that's what #5's `privacy` class enforces. DeepSeek is for non-sensitive coding/reasoning/bulk summarization only.

**Depends on:** ideally #5 to be useful (otherwise it's just another manual `--agent` choice).

---

## 7 — Local LLM (dedicated machine)  ·  `deferred`

A zero-cost, private model for classification, routing, and sensitive-content summarization that must not leave the fleet.

**Decision (2026-06-26):** the Mac Mini is confirmed **Intel** → Ollama would be CPU-only (small quantized models, modest throughput) — not worth the wiring. **Park this track.** Until a dedicated LLM box exists (Apple Silicon Mac or a GPU machine), **privacy-class work runs on Claude (cloud)** and mechanical/classify runs on Haiku. Revisit when the hardware lands.

**When revisited — smallest slice (v1):**
- Install Ollama on the dedicated machine; pick a model sized to its capability (e.g. `qwen2.5:14b`+ on Apple Silicon; `llama3.2:3b` only if stuck on CPU).
- `agents/local_agent.py`: OpenAI-compatible wrapper, `base_url=http://<host>:11434/v1`.
- Add capability `local_llm` to that machine in `machines.yaml`; register `"local"` in `runner.py`.
- Flip the `privacy` + `mechanical` routing classes in #5 from Claude/Haiku to `local`.

**Role (when live):** intent classification for the concierge (#9), summarizing email/finance/calendar text (#13/#14) without sending it to a cloud, mechanical reformatting. **Not** a Sonnet-class reasoner.

**Depends on:** new hardware; #5 routing already leaves the slot open.

---

## 8 — Autonomy foundation: Plan-and-Execute + Supervisor  ·  `idea`

The smallest real step from "task queue" to "reasoning system", grounded in existing primitives. Proven against #3 (research) and #4 (writing) — **not** built as a generic platform.

**Smallest slice (v1):**
1. **`plan` task type** + a `steps` array stored in the existing `payload` JSON column — Plan-and-Execute state with **no schema change**.
2. **Supervisor agent:** a `claude_agent` run given the queue API (`da` / `POST /tasks`) as its one tool; it decomposes a request into `steps` and enqueues specialist sub-tasks.
3. **Circuit breaker:** a step/token budget in `runner.py`, extending the existing timeout guard — terminate the loop on budget exhaustion or no-progress.
4. **Validator loop:** promote `code_review` into a Validator that checks output against a threshold and forces a retry-with-error-context; `needs_human` is the existing escape hatch when it can't converge.

**Explicitly out of scope:** vector/long-term memory and a tracing/observability platform. Defer both until a track actually hurts without them — `result` dict + SQLite is enough for three machines.

**Depends on:** nothing structural; #5 makes it cheaper (route planner vs. executor vs. validator to different models).

---

## 9 — Concierge / router agent  ·  `shipped` (2026-06-27)

**Shipped & live:** `find <query>` (bridge + `find` handler/TaskType). Deterministic keyword classifier (weather/email/calendar/tasks/shop) with LLM fallback routed to Haiku via #5; maps the category to a specialist task on the right machine, enqueues it, polls, returns the combined answer (the Hierarchical-Supervisor primitive over the queue). Verified: `find weather in Tokyo`, `find unread email`, `find my schedule`, and the LLM tail (`should I bring an umbrella` → weather). `shop` category replies "coming in #10".

The front door for freeform requests ("find me X", "what's the weather", "any deals on Y") — the Hierarchical Supervisor applied to everyday lookups. Classifies intent, suggests/selects sources, fans out to specialists, synthesizes the reply.

**Smallest slice (v1):**
- New task type `find`. Bridge command `find <query>` from WhatsApp self-chat.
- **Deterministic first:** keyword→category map (`book/grocery/electronics/finance/email/weather`) handles the common cases at zero LLM cost.
- **LLM only for ambiguity** (route classification to **Haiku** cheaply via #5; swaps to local when #7 lands). Asks the user where to look when sources are ambiguous (wish #3 in the brief).
- Enqueues the right specialist sub-task(s) (#10, #12, #13, #14) and returns a combined answer.

**Depends on:** #5/#7 (cheap classify), and at least one specialist (#12 weather is the easiest first).

---

## 10 — Commerce search (Amazon.sg / Lazada / Redmart)  ·  `deferred` (2026-06-27)

**Decision (2026-06-27):** parked. Unlike email's quick app password, commerce search needs an **approval-gated affiliate account** (Lazada Affiliate/Open Platform App Key+Secret, or Amazon Associates+PA-API which requires qualifying sales first). The concierge (#9) already answers `find shop…` with "coming in #10". Revisit when an affiliate API account exists; the `shop_search` slice below is unchanged and ready to build against whichever source lands. (Scraping/third-party was considered and not chosen.)

Search products + groceries across SG marketplaces; return a normalized result list (title, price SGD, url, rating, availability).

**Reality check (decides the design):** there is **no clean consumer ordering API**.
- **Search data is gettable:** Amazon **PA-API** (SG marketplace; affiliate-account-gated), **Lazada Open Platform** (covers **Redmart** too — Redmart is Lazada SG's grocery arm, so one integration serves two wishes). Both are seller/affiliate-scoped, return product data only.
- **Ordering has no API** → see #11.

**Smallest slice (v1):**
- Task type `shop_search` with payload `{query, source}` where `source ∈ {amazon_sg, lazada, redmart}`.
- Start with **one** source — **Redmart via Lazada API** (real API, recurring grocery use) — return a normalized schema with SGD pricing + GST awareness.
- Surfaced through the concierge (#9) or directly via `shop <source> <query>`.

**Open decisions:** PA-API affiliate eligibility; result ranking; caching to respect rate limits.

**Depends on:** none to start; best behind #9.

---

## 11 — Commerce action: cart + order (web-access, HITL)  ·  `idea`  ·  v2

Last-mile ordering. **Autonomous cart, human checkout** — the line for a personal-money agent.

**Reality:** ordering only works via **authenticated web automation** (Playwright) on your own logged-in account. Fragile (DOM changes, captcha, OTP/2FA) and carries account-risk if flagged as a bot.

**Smallest slice (v1):**
- New `web_shop` capability on **one** machine with a persistent, logged-in browser profile.
- Task types `shop_cart` (build the cart — autonomous) and `shop_order` (**stops before the purchase-confirm click** → `needs_human` for the final approval).
- The `needs_human` gate (`main.py:165`) + macOS/WhatsApp notification is the approval step.

**Depends on:** #10 (search → choose item), the `needs_human` gate.

---

## 12 — Weather agent  ·  `idea`

Today's weather at your location. The easiest end-to-end proof of the new-agent loop (task type → handler → state → WhatsApp reply).

**Smallest slice (v1):**
- Task type `weather`. Data source **Open-Meteo** (free, no API key).
- Location from a `set-location <place>` command stored in `config/location.yaml`; falls back to last-known ("this is your last location").
- Reply: today's high/low, conditions, rain %.

**Depends on:** none. **Recommended first build** (Sprint 0).

---

## 13 — Investment / portfolio agents  ·  `idea`

Read-only portfolio + market view across **Saxo, IBKR, Yahoo Finance**. Extends #2 from watchlist-alerts to real accounts.

**Smallest slice (v1):**
- Task type `portfolio` — read positions/balances. **IBKR** has an API (already reachable as a connector in dev sessions); **Saxo** has OpenAPI (OAuth); **yfinance** for quotes/marks.
- Reply: positions · market value · day P/L, one WhatsApp message.

**Out of scope for v1:** any order placement. Trades are v2 and inherit #2's risk gates + the `needs_human` checkout pattern from #11.

**Privacy:** finance content is `privacy`-class in #5 — never routed to DeepSeek/cloud-CN; summarize via Claude (cloud; local once #7 lands).

**Depends on:** #2 (shares `market_brief`), #5 for safe routing.

---

## 14 — Personal data: email + calendar + Obsidian/planning  ·  `shipped` (2026-06-27)

**Shipped & live:** Obsidian/tasks/planning via #1 (`assist …`, `assistant_query`); **`calendar`** — today's events + next free slot via the assistant's ICS calendar (new `/calendar` API endpoint; bridge `calendar`/`day`); **`email`** — read-only Gmail search over IMAP (bridge `email [query]` w/ Gmail `X-GM-RAW` syntax; `email_lookup` handler on macbook-pro). Decisions: IMAP + app password (not OAuth); assistant ICS calendar (not live Google). Send = v2 `needs_human`.


Read/search/summarize email and calendar; reach Obsidian tasks & planning (the existing assistant).

**Smallest slice (v1):**
- Task type `email_lookup` — search + summarize inbox via Gmail API. **Read-only**; sending is a v2 `needs_human` action.
- Task type `calendar` — "what's my day" / "next free 2h slot" via Google Calendar API. **Read-only**; creating/moving events is a v2 `needs_human` action.
- Obsidian/tasks/planning go through the **#1 assistant bridge** (`assist …`) — no new integration.

**Privacy:** `privacy`-class routing (#5) — summarize via Claude (cloud; local once #7 lands), never DeepSeek.

**Depends on:** #1 (Obsidian side); Gmail + Google Calendar OAuth creds per machine.

---

## 16 — Social presence: Mastodon + LinkedIn (read + reply)  ·  `idea`

Read your social feeds, and — future version — when you share a post link, fetch that post and draft (or, behind HITL, submit) your reply. Reuses #4's voice exemplars for the drafting, and the same **draft = autonomous / post = `needs_human`** line as commerce and email.

**Reality check (decides the design — the two platforms are opposites):**
- **Mastodon: clean, first-class API.** Full documented REST API with OAuth / personal access tokens — read home timeline, read your own posts, resolve a post URL to its content (`/api/v2/search?resolve=true`), and post a reply (`POST /api/v1/statuses` with `in_reply_to_id`). ToS-compliant, low account-risk. **Build this side first.**
- **LinkedIn: effectively no usable API.** The official APIs (Marketing / Community Management) are partner-gated and don't expose your personal feed or arbitrary post replies. Reading your feed or posting a reply therefore needs **authenticated web automation** (Playwright on your logged-in profile) — fragile (DOM churn, feed virtualization), against LinkedIn's ToS, and carries real account-restriction risk. Treat as **v2, opt-in, and risk-flagged** (mirrors the #11 web-shop posture). Default the LinkedIn reply path to **draft-only** (you paste it) to avoid automated posting entirely.

**Smallest slice (v1 — Mastodon, read + draft):**
- Task type `social_read` — fetch home timeline / your recent posts via the Mastodon API; return a normalized list (author, text, url, time, reply-count). `MASTODON_INSTANCE` + `MASTODON_TOKEN` in `.env`.
- Task type `social_reply` with payload `{url, intent?}` — resolve the post URL → fetch its content → **draft** a reply in your voice (style card from #4). v1 **returns the draft only** ("help me formulate a reply"); no posting.
- Bridge commands: `social feed` and `reply <url> [hint]` from WhatsApp self-chat → draft comes back to self-chat for copy-paste.

**v2 (submit + LinkedIn):**
- Mastodon: `social_reply` gains a `post=true` mode → `POST` the reply behind the `needs_human` gate (`main.py:165`) — approve in WhatsApp, then it submits.
- LinkedIn: add a `social_web` capability on the one machine with a persistent logged-in browser profile (same box as #11's `web_shop`). `social_read`/`social_reply` for `source=linkedin` route through Playwright; **posting always behind `needs_human`**, draft-only by default.
- **LinkedIn read scope (what you asked for):** two read modes, both via the logged-in web session (no API for either):
  - `social_read source=linkedin mode=relevant` → **latest posts relevant to me**: scrape the top of your home feed, return a normalized top-N (author, text, url, time, reaction/comment counts). "Relevant" ranking is best-effort — LinkedIn's own feed ordering first, optionally re-ranked by keyword/topic affinity (your watched topics) so it surfaces what matters to you rather than raw chronology.
  - `social_read source=linkedin mode=mentions` → **activities where I'm part of or mentioned in**: scrape the Notifications + your own Activity pages for items where you're tagged, mentioned, commented-on, or replied-to. Return author · what-happened · the post url · time, so you can jump straight to `reply <url>`. This is the natural feeder into the reply flow.
  - Both are read-only scraping → autonomous (no `needs_human`); only the reply/post step gates. Caveat: scraping volume/cadence is itself an account-risk signal — keep it low-frequency (on-demand or a once-or-twice-daily digest), not continuous polling.

**Privacy / routing:** feed content and your account creds are sensitive — keep account-touching calls and reply drafting on **Claude (cloud; local once #7 lands)**, never DeepSeek/cloud-CN (consistent with #5's `privacy` class). Reply drafting can use Sonnet (voice quality matters); classification/triage of the feed can use Haiku.

**Open decisions:** which Mastodon instance is home; whether `social_read` is pull-on-demand vs. a periodic digest (feeds into #15 morning brief); LinkedIn web automation is a genuine ToS/account-risk call to make explicitly before building the v2 path.

**Depends on:** #4 (voice exemplars / style card) for good drafts; #5 for routing; the `needs_human` gate for posting; #11's logged-in-browser machine pattern reused for the LinkedIn path.

---

## 17 — Multi-user / family access  ·  `idea`

Let approved **family members have their own accounts** and use the assistant over WhatsApp — each with their own state (location, sessions) and a **restricted, safe command set**. This replaces the bridge's current single-user lock, so it has to be done security-first.

**Reality check (this is a security-model change, not just a feature):** today the bridge hard-scopes to the owner's self-chat — `webhook()` drops every message where `_digits(chat_id) != _self_number` (`bridge.py`). That lock is *why* it's safe to expose arbitrary-exec commands (`agent`, `run`, `assign`, `code review`, `assist`) — they can only ever come from you. Opening the door to other people means that lock is gone, so the **permission model below is mandatory, not optional**.

**Two decisions that shape everything:**
1. **Whose WhatsApp number is the assistant?** The Waha session is currently linked to *your personal* number — so family "contacting the assistant" would mean the bot auto-replying inside your real chats with them. **Strongly recommend a dedicated assistant WhatsApp number** (separate SIM/account linked to Waha) so the bot has its own identity and family DM *it*, not you. Keeps personal chats clean and makes per-sender routing unambiguous. (Owner keeps the existing self-chat path, or moves to the dedicated number too.)
2. **Permission tiers.** Roles, not a flat allowlist:
   - **owner** (you) → everything, including the arbitrary-exec/private-data commands.
   - **family** → a **safe, curated subset only**: `weather` / `weather in <place>`, `find` (concierge, #9), commerce **search** (#10), maybe `write post`. **Hard-denied:** `agent` / `run` / `assign` / `code review` (arbitrary code on the fleet via `--dangerously-skip-permissions`), `assist` + email/finance/calendar/Obsidian (#14/#13 — *your* private data and connectors). Default-deny: a command is family-usable only if explicitly on the safe list.

**Smallest slice (v1):**
- **Roster:** `config/family.yaml` (gitignored — it's PII / phone numbers) mapping `number → {name, role}`. Bridge loads it; `webhook()` accepts a message when the sender is the owner **or** an allowlisted family number, and tags the request with that role.
- **Command gate:** a `role → allowed-commands` table in the bridge; `_parse` result is checked against the sender's role before dispatch. Non-allowed → a polite "not available" reply. **This is the core of the track** — everything else is plumbing.
- **Per-user state:** key `location.yaml` (and multi-turn sessions) by sender number instead of a single global, so each member has their own saved location / context. (Weather's last-known becomes per-user.)
- **Enrollment:** owner-only command `family add <number> <name> [role]` / `family remove <number>` / `family list` — writes `family.yaml`. New members default to the `family` role.

**Privacy / isolation:** family members never reach the owner's private connectors or data; each member's data (location, history) is scoped to them. Audit line per family-initiated task (who asked what) in `notes`. DeepSeek/cloud-CN routing rules (#5) still apply per request.

**Open decisions:** dedicated number vs. owner number (decide first — it changes the enrollment + scoping design); whether family members get *any* write/action commands in v1 or read-only to start; rate-limiting per member; whether to support group chats or 1:1 only (1:1 first).

**Depends on:** nothing structural to start (it's a bridge-layer change), but it gates how every later capability is exposed — best landed **before** money-touching or private-data tracks are broadly used. Reuses `needs_human` if any family action ever needs owner approval.

---

## 18 — Autonomous project lifecycle (intake → plan → scaffold → execute)  ·  `idea`

One or more agents that take a project from idea to delivery: you *start* a new project or ask to *review* an existing one, the agent proposes tasks and scaffolds it on a chosen machine, you discuss and refine the plan together, and on your go-ahead the fleet **autonomously executes** it — coding, writing, publishing, shopping — with money/publish steps gated. This is the headline "more autonomy" ask.

**Reality check (this is the *product* on top of #8's *engine* — reuse, don't rebuild):** the primitives already exist. The Supervisor (#8) is a `claude_agent` given the queue API as its one tool; it decomposes a request into `steps` and enqueues specialist sub-tasks. Workers already run `claude`/`codex` with a `cwd` and `--dangerously-skip-permissions`, so they can create folders, write code, run tests, and commit on whichever machine holds the repo. Multi-turn claude sessions already power the back-and-forth "discuss the plan" phase. `needs_human` already gates money/publish. **What's genuinely new here is the orchestration + guardrails around those, not a new framework.**

**The lifecycle (each phase maps to an existing primitive):**
1. **Intake / review** — `project start <name> …` or `project review <path|name>`. For *review*, an `agent_run` with `cwd=<repo>` on the machine that holds it (claude reads the code, summarizes state, proposes a task list). For *new*, the agent proposes a structure. Output: a draft plan, not action yet.
2. **Plan (collaborative)** — a multi-turn claude session (#1 pattern): you refine scope, the agent updates the plan. The plan is persisted as `steps` in the task payload (#8) **and** as a human-readable `PLAN.md` in the project (or an Obsidian note via #1) so it's reviewable/editable.
3. **Approve (HITL gate)** — execution does **not** begin until you explicitly approve the plan (`project go <name>`). This is the single most important gate: it's the line between "drafting" and "the fleet starts doing things."
4. **Scaffold** — on approval, a worker task creates the project folder + skeleton (git init, base files) on the chosen machine (**mac-mini or thinkpad**, picked via #5b placement / `_preferred_machine`; the repo must live on that worker).
5. **Execute (autonomous)** — the Supervisor decomposes the agreed plan into sub-tasks and enqueues them to specialists: **coding** → `claude`/`codex` with `cwd` on the project's machine; **writing** → content/social agents (#4); **publishing** → #16 (LinkedIn) / drafts for Substack/Medium; **shopping** → #11. A **Validator** (#8) checks each output and retries-with-context; `needs_human` is the escape hatch when it can't converge.
6. **Iterate / report** — progress back to WhatsApp; at decision points or gate hits it asks you (`needs_human`), and you can amend the plan mid-flight (back to phase 2).

**Interaction — requirements come from your request AND from the agent asking you (all over WhatsApp):**
- **Two-source requirements:** the agent extracts what it can from your initial message — project type/goal **and the target machine** (e.g. "develop a CLI on **thinkpad**" → scaffold + execute pinned there via `_target_machine`/`_preferred_machine`) — then **asks you clarifying questions** for the rest (stack? framework? deps? scope of v1?) instead of guessing.
- **AskUserQuestion over WhatsApp = the multi-turn session (already shipped).** WhatsApp/WAHA has no native choice-button UI, so the agent asks in chat — posing concrete numbered options ("1) FastAPI  2) Flask  3) other") — and you reply with a number or free text; the claude session (`--session-id`/`--resume`, the bridge's existing multi-turn) carries the context. Functionally the same gather-by-asking loop as the IDE's AskUserQuestion, just conversational. (A structured quick-reply UI is a later nicety, gated on WAHA Plus / a richer client like the Flutter terminal.)
- **Whole loop runs from self-chat:** `project start <name> on <machine>` → agent asks its questions → you answer in chat → it writes `PLAN.md` → you `project go` → it scaffolds + executes on that machine → progress + `needs_human` prompts come back to the same chat. No desktop needed.
- **The clarify phase respects autonomy level + gates:** questions are free (L1+); only the `project go` approval and the money/publish gates pause for `needs_human`.

**Autonomy levels (you set per project — this is how "autonomous" it actually is):**
- **L1 plan-only** — proposes + maintains the plan, executes nothing.
- **L2 develop-but-gate** *(recommended default)* — writes code/drafts and runs tests autonomously, but **stops at**: `git push`, publish, deploy, and any spend → `needs_human`.
- **L3 full-auto within budget** — executes the whole plan under a step/token **circuit breaker** (#8), still hard-gating money/publish (those are never silently autonomous).

**Hard gates (always, regardless of level):** purchases (#11 — cart autonomous, **purchase-confirm = `needs_human`**, "on my command" honored literally); publishing to LinkedIn/Substack/Medium (#16/#4 — note Substack/Medium have **no clean auto-publish API** → realistically draft → `needs_human` to post); anything spending money or acting as you externally.

**New pieces to build (small):** a `project` task type + WhatsApp verbs (`project start|review|plan|go|status|stop <name>`); a gitignored **project registry** (`config/projects.yaml`: name → machine, path, autonomy level, status, plan ref) — lightweight, not a new DB; a scaffold handler. Everything else is #8 + #5b + existing agents.

**Circuit breakers (must-have before L3):** per-project step/token budget and a no-progress detector (extend #8's), plus the `needs_human` stop and a `project stop <name>` kill switch. Autonomy without a brake is the main risk here.

**Open decisions:** default autonomy level (recommend L2); is `git push` itself a gate or autonomous within a project's own branch; how plans are stored (PLAN.md in-repo vs Obsidian vs both); one generalist project-agent vs. a Supervisor that routes to per-domain specialists (coding/writing/commerce); how tightly to bind a project to one machine vs. allow cross-machine steps.

**Depends on:** #8 (the engine — Supervisor/Plan-Execute/Validator/circuit-breaker), #5b (which machine runs it), #1 (assistant/Obsidian for plans), #4 (writing), #11 (shopping HITL), #16 (publishing HITL). Lands **after** #8; it's the capstone that makes the rest feel like one assistant.

---

## 15 — Composite features  ·  `idea`

Compositions of the agents above — high value-per-effort once the parts exist.

- **Morning brief** (cron, 7am → WhatsApp): weather (#12) + `market_brief` (#2) + calendar + top-3 emails (#14) in one message.
- **Price-watch / deal alerts:** save a product (#10) → cron `price_watch` → WhatsApp when below threshold.
- **Grocery list → cart:** read a recurring list from Obsidian (#14) → build Redmart cart (#11) → HITL checkout.
- **Receipt/order email → expense note** in Obsidian (#14 + #1).

**Depends on:** the specific parts each composes.

---

## Sprint plan (summary)

Time-boxed groupings of the tracks above, in dependency + value order. Each sprint ships something usable.

| Sprint | Theme | Tracks | Deliverable |
|---|---|---|---|
| **0** | Quick win + cheap provider | #12 Weather · #6 DeepSeek | One new agent working end-to-end via WhatsApp; DeepSeek selectable in `runner` |
| **1** | Routing + placement | #5 Router · #5b Placement/overflow  ·  *#7 deferred* | Cheapest-fit model per call; agents prefer Mac Mini, overflow to ThinkPad/MBP when it's busy or down |
| **2** | Personal data (read) | #1 Assistant integration · #14 Email/Calendar/Obsidian read | `assist …` + `email_lookup` + `calendar` from self-chat; Obsidian/tasks reachable |
| **3** | Front door + commerce search | #9 Concierge · #10 Commerce search (Redmart first) | `find <query>` classifies + searches one marketplace |
| **4** | Reasoning autonomy | #8 Plan-and-Execute + Supervisor + Validator, proven on #3/#4 | Supervisor decomposes a research/writing request into queued steps with a validator loop |
| **5** | Finance (read) | #2 Market alerts · #13 Portfolio read (IBKR/Saxo/yfinance) | Market brief + portfolio snapshot via WhatsApp |
| **6** | Actions behind HITL + composites | #11 Web-shop cart/order · #16 Social (Mastodon read+reply; LinkedIn web v2) · #15 Morning brief, price-watch, grocery→cart · finance trades (v2) | Autonomous cart / human checkout; Mastodon feed + reply drafts; morning brief; deal alerts |
| **7** | Multi-user / family access | #17 Family roster + role-based command gate (dedicated assistant number) | Approved family members use the safe command subset (weather/find/search) over WhatsApp, each with their own state — owner-only commands stay owner-only |
| **8** | Autonomous project delivery | #18 Project lifecycle (intake → plan → approve → scaffold → execute) on the #8 engine | `project start/review <name>` → agent proposes + co-develops a plan → on `project go`, the fleet builds/executes it on mac-mini or thinkpad at autonomy level L2, money/publish gated |

**Notes**
- Sprint 0 is deliberately tiny — it proves the full loop (and de-risks DeepSeek) before anything ambitious.
- Sprints 0–1 are foundational; 2–3 deliver daily-use value; 4 adds reasoning autonomy; 5–6 add money-touching actions, all behind `needs_human`.
- **#17 (family) can move earlier** if family access is wanted sooner — it's a self-contained bridge-layer change. But it must land **before** any family member is given access, since it replaces the single-user security lock; until then the bridge stays owner-only.
- **#18 (autonomous projects) is the capstone** and hard-requires #8's engine (Supervisor/Plan-Execute/Validator/circuit-breaker) — don't attempt it before Sprint 4 lands. It then *composes* the writing (#4), shopping (#11), and publishing (#16) tracks into one project-delivery flow, so those are worth having first too. Start it at autonomy **L2** (develop-but-gate); never ship **L3** without the step/token circuit breaker and `project stop` kill switch.
- Defer vector memory + observability platform until a sprint demonstrably hurts without them.

---

## Sequencing summary

```
Sprint 0  #12 Weather ─┐                              ┌─→ Sprint 3  #9 Concierge ─→ #10 Commerce search
          #6 DeepSeek ─┴─→ Sprint 1  #5 Router + #5b ─┤
                          (#7 local deferred)          └─→ Sprint 2  #1 Assistant ─→ #14 Email/Calendar/Obsidian
                          #5b = Mac Mini primary, overflow to ThinkPad/MBP

Sprint 4  #8 Autonomy (Plan-Execute + Supervisor + Validator)  ── on top of #3 Research, #4 Writing
Sprint 5  #2 Alerts ─→ #13 Portfolio (read)
Sprint 6  #11 Web-shop (HITL) · #16 Social (Mastodon first, LinkedIn web v2) · #15 Composites · finance trades (v2)
```

**Recommendation:** ship Sprint 0 first (weather + DeepSeek — both small, independent, prove the loop). Then the model-routing layer (Sprint 1) so DeepSeek actually earns its place — the local model is parked until you have a dedicated (non-Intel) machine, so privacy-class work stays on Claude meanwhile. Everything else sits on those two foundations.
