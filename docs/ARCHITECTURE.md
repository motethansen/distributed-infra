# distributed-infra — Architecture

**As of 2026-11-06.** The current, running architecture of the fleet. This is the "how it
actually works now" reference; design rationale lives in [rqlite-failover.md](rqlite-failover.md),
the track history in [ROADMAP.md](ROADMAP.md), who may use which agent in
[AGENT_ROSTER_AND_ACCESS.md](AGENT_ROSTER_AND_ACCESS.md), and how to run it yourself in
[SETUP.md](SETUP.md). Secrets handling is in [SECURITY.md](SECURITY.md).

Addresses in this document are placeholders (`<mac-mini-ip>` and so on). Yours come from
`tailscale ip -4` on each machine and live in your gitignored `.env` and `config/machines.yaml`.

---

## 1 — What it is

A personal **distributed task queue with a worker fleet**, running across home
machines on a private **Tailscale** network. The contract is a single **queue**:
producers create tasks, workers pull the ones they can run, CLI agents do the
reasoning, results write back to the same task row. Control flows one direction —
**human → queue → worker → agent** — with no agent-to-agent hierarchy.

The queue is **highly available**: its state lives in a 3-node Raft cluster (two home
machines and one cloud Docker host, all on the tailnet) and the orchestrator API runs
active-active, so **the fleet survives any single node failing, including the home internet
connection going down** (demonstrated live).

Stack: FastAPI · httpx · Pydantic · **rqlite** (Raft-backed SQLite) · subscription-CLI agents.

---

## 2 — Fleet topology

Tailscale tailnet (`<tailnet>.ts.net`). Auth between services is a shared
`SECRET_KEY` in an `x-secret-key` header. Machine liveness in `/machines` is derived
from Tailscale itself (`tailscale status`), with worker-poll recency as a separate
`worker_active` signal.

| Machine | Tailscale IP | Role in the system | Supervision |
|---|---|---|---|
| **mac-mini** | `<mac-mini-ip>` | orchestrator + worker + **rqlite voter** + WhatsApp bridge + WAHA + Vantage monitor | launchd |
| **thinkpad-x1** | `<thinkpad-ip>` | orchestrator + worker + **rqlite voter** | systemd `--user` (lingering) |
| **cloud Docker host** (a small droplet) | `<droplet-ip>` | **rqlite voter** only, in Docker | Docker `--restart unless-stopped` |
| **macbook-pro** | `<macbook-ip>` | owner's own machine: dev checkout, `dis-agent` / `da` CLIs. **Never a worker.** | none |

- **mac-mini** is the always-on primary (orchestrator, WhatsApp bridge, Vantage, one voter).
- **thinkpad-x1** is the always-on secondary (second orchestrator, one voter).
- The **cloud Docker host** is the third Raft vote. It has no compute role, and rqlite listens
  **only on its Tailscale address**, never on a public interface. It keeps the queue writable
  when the home internet or router is down, because mac-mini and thinkpad are still 2 of 3.
- **macbook-pro** submits work and reviews it. It is not an rqlite node and runs no worker, so a
  sleeping laptop never stalls the fleet.

The earlier design used a Synology NAS as the third voter. The NAS was decommissioned
on 2026-11-01 and replaced by the cloud Docker host; nothing in the running system refers to it.

Two kinds of checkout: development happens on the owner's laptop; mac-mini and thinkpad run the
live services from `~/Projects/distributed-infra` and deploy with `git pull --ff-only` followed by
a service restart. Story work for the Scrum desk is built in a **separate** clone
(`~/Projects/distributed-infra-stories`), so an unreviewed branch is never loaded by a restart.

---

## 3 — The HA queue (rqlite cluster)

The queue is a **3-node rqlite Raft cluster** (v10.2.7). Each node serves HTTP on **:4001** and runs
Raft on **:4002**, advertising its tailnet address. Writes route to the elected leader (any node
forwards); a write is durable on a **quorum (2 of 3)** before it acks. Any one node can be down.

| Node | How it runs | Data |
|---|---|---|
| mac-mini | Docker `rqlite/rqlite`, ports published **only on its Tailscale address** (`-p <tailscale-ip>:4001:4001 -p <tailscale-ip>:4002:4002`), restart `unless-stopped` | named volume |
| thinkpad | native Linux binary `~/rqlite/rqlited`, systemd user unit `rqlite-node.service` ([template](../scripts/rqlite-node.service.example)) | `~/rqlite-data` |
| cloud Docker host | Docker `rqlite/rqlite`, ports published **only on its Tailscale address** | named volume |

Nodes join over the **Raft** port (`-join <peer-ip>:4002`; rqlite v10 rejects the HTTP port and
the `http://` scheme). The schema is one `tasks` table plus the Scrum tables; the orchestrator creates
them at startup (`ensure_schema`). Design notes and the history of this choice: [rqlite-failover.md](rqlite-failover.md).

**Validated:** cross-node replication; a chaos drill (250 tasks, 8 concurrent claimers, leader hard-killed
3 times under load: 0 double-claims, 0 losses, always back to 3/3); leader election in about 4 s.

**Check it:** `curl -s "http://<mac-mini-ip>:4001/nodes?nonvoters&timeout=3s"` should list 3 voters, all
reachable. The `timeout=3s` matters: without it `/nodes` waits on any unreachable member.

## 4 — Orchestrator (active-active)

Two orchestrator instances front the same rqlite queue. Each is stateless given the
cluster, so both serve identical results.

- **mac-mini** — launchd `com.techstartups.orchestrator`, port 8000.
- **thinkpad** — systemd `--user` `orchestrator.service`, port 8000.
- Both: `QUEUE_BACKEND=rqlite`, `RQLITE_URL=http://<THIS_TAILSCALE_IP>:4001` (each talks to its
  local rqlite node; followers forward writes to the leader).

Backend selection is one line in `orchestrator/main.py`:
`QUEUE_BACKEND=rqlite` → `import db_rqlite as db` (else the aiosqlite `db`). The rqlite
backend (`orchestrator/db_rqlite.py`) mirrors `db.py`'s signatures over rqlite's HTTP
API (`/db/execute`, `/db/query`, `/db/request`).

**Key endpoints:** `POST /tasks` (create), `POST /tasks/claim` (worker atomic pull),
`/tasks/{id}/complete|fail|needs-human`, `GET /tasks`, `GET /machines`, `GET /health`.

---

## 5 — Request & data flow

```
   Ingress                Orchestrator API            Queue (Raft)          Workers            Agents
 ┌───────────┐   POST    ┌────────────────┐  db_rqlite ┌──────────────┐  claim ┌─────────┐  dispatch ┌──────────┐
 │ WhatsApp  │──/tasks──▶│  mac-mini :8000│──HTTP────▶│  rqlite :4001 │◀──────│ workers │──────────▶│ claude / │
 │ bridge    │           │       ⇅        │  execute  │  (leader gets │  pull  │ (poller)│           │ codex /  │
 │ da CLI    │──────────▶│ thinkpad :8000 │  query    │   the write)  │───────▶│  ×3     │           │ agy / …  │
 │ schedulers│  (either) │  ACTIVE-ACTIVE │  request  │  ● ● ●  2/3    │        └────┬────┘           └──────────┘
 └───────────┘           └────────────────┘           │  quorum       │             │ complete/fail/needs_human
                                                       └──────────────┘             ▼  → back to the task row
```

1. A producer `POST`s a task to **either** orchestrator (bridge/CLI/scheduler).
2. The orchestrator writes it to rqlite (its local node forwards to the leader).
3. Workers **poll `/tasks/claim`** on their preferred orchestrator (failover list),
   which runs the atomic claim on rqlite and hands back one task.
4. The worker dispatches to the matching handler (`worker/handlers/<type>.py`), which
   may shell out to a CLI agent, then reports `complete`/`fail`/`needs_human` — which
   writes back to the same rqlite row.
5. The producer polls the row and relays the result (e.g. WhatsApp reply).

---

## 6 — Task model

**Lifecycle:** `pending → claimed → in_progress → done | failed | needs_human`.
Claiming is **atomic** — a single `UPDATE … WHERE id=(SELECT … LIMIT 1) AND
status='pending' RETURNING *` via rqlite `/db/request`, linearizable through Raft, so
no task is ever claimed twice (even with orchestrators running active-active).

**Placement (#5b):** agent-style tasks default to `_preferred_machine=mac-mini`; other
capable workers may claim only after a 20s overflow grace. A hard `_target_machine` pin
overrides. Ordering: `priority DESC, created_at ASC`. Capability-scoped — a worker only
claims task types it advertises (iOS builds → mac-mini, Android → thinkpad, etc.).

**Handlers are auto-discovered:** task type `X` runs `worker/handlers/X.py::handle_X()`;
drop in a file and the type exists. **24 types** across build / dev / agent / content /
info / reasoning-autonomy (`plan`, `project`) / finance / composites.

---

## 7 — Workers & failover

Each worker (`worker/main.py` + `worker/poller.py`, port 8001) polls every 10s and
claims by capability, with a concurrency cap (overflow to peers).

**Multi-orchestrator failover (#20 Phase 3):** the poller takes `ORCHESTRATOR_URLS`
(comma-separated), uses the last-known-good endpoint, and fails over to the next on any
connection error — so a worker keeps claiming/reporting even if one orchestrator is
down. Set to `http://<mac-mini-ip>:8000,http://<thinkpad-ip>:8000` on the thinkpad +
macbook workers. Demonstrated live: mac-mini orchestrator stopped → thinkpad worker
kept processing via thinkpad's orchestrator.

| Worker | Supervision | Endpoints |
|---|---|---|
| mac-mini | launchd `com.techstartups.worker` | single (dies with mac-mini anyway) |
| thinkpad | systemd `--user` `infra-worker.service` | mac-mini, thinkpad (failover) |
| macbook | launchd `com.techstartups.worker` | mac-mini, thinkpad (failover) |

---

## 8 — Agents & model routing

One runner (`agents/runner.py`) fronts subscription-CLI agents — **claude, codex, agy,
deepseek, content, social** — the CLIs use their own login sessions, DeepSeek uses
`DEEPSEEK_API` (only `claude` is multi-turn). Groq was removed 2026-10-28. When a caller routes by `task_kind`/`sensitivity`,
`config/routing.yaml` picks the cheapest agent+model:

- privacy / coding / planning → `claude · sonnet` · reasoning/bulk → `deepseek` · mechanical → `claude · haiku`
- **Privacy guard (hard):** email/finance/calendar/personal never leave to DeepSeek (China-hosted).
- **Failover:** when the routed agent is unavailable (usage/session limit, rate limit, quota,
  not logged in) the task moves down its class's `fallbacks` list in `routing.yaml`
  (e.g. claude → codex → agy). The limited agent is skipped until the reset time it
  reported (`data/agent_cooldowns.json`, per machine). Timeouts and real errors do not
  fail over. Multi-turn chats send their recent `history`, so the next agent picks up
  mid-conversation; the reply says who answered. See `agents/failover.py`.
- **DeepSeek is text-only.** It has no shell or file access, so a developer task for it returns code as text and a
  Claude integrator applies it. If it answers an agentic prompt with a tool call written out as text, the agent retries
  once, then fails the run (`printed a tool call as text`) and the task moves to Claude Sonnet, which can edit files;
  Claude is tried first; if Claude is unavailable or already tried, the rest of the normal chain follows (which may include codex). No cooldown is set, so DeepSeek keeps answering plain questions.
- **Harness access (`clients/fleet-mcp`):** a stdio MCP server registered in Claude Code (so
  T3 Code's Claude threads), Codex, OpenCode and Antigravity gives them `fleet_ask` /
  `fleet_result` / `fleet_tasks` / `fleet_machines`. It submits `agent_run` tasks, so the
  failover above applies; named conversations are kept locally and can be continued from a
  different harness.
- **Opus is reserved for the Product Owner role** and is capped per day; every other role defaults to **Sonnet** (`config/roles.yaml`).

Headless `claude` needs the workspace trusted + an active login on each worker; a lapsed
login is a recurring failure mode (on macOS the CLI keeps its login in the Keychain, which a launchd job can fail to unlock).

---

## 9 — Ingress / interfaces

- **WhatsApp bridge** — `services/whatsapp-bridge/bridge.py` on mac-mini (:3001) + WAHA
  Docker (:3000, NOWEB). Self-chat commands → tasks; polls results → replies. Family
  roster (#17), morning-brief scheduler. A `FAILED` WAHA session needs `/restart`, not
  `/start` (see Troubleshooting in the README).
- **`da` CLI** (`orchestrator/da.py`) — terminal control (queue, status, review, resolve).
- **Direct HTTP API** — any client with the secret key.

---

## 10 — Failure behavior

| Event | Queue writable? | Fleet impact |
|---|---|---|
| **mac-mini down** | ✅ yes (thinkpad + cloud host = quorum) | thinkpad orchestrator + thinkpad worker keep running (failover); WhatsApp down (bridge is on mac-mini); mac-mini-pinned tasks wait |
| **thinkpad down** | ✅ yes (mac-mini + cloud host) | mac-mini orchestrator + workers keep running; Android-pinned tasks wait |
| **cloud host down** | ✅ yes (mac-mini + thinkpad) | none: it is voter-only. Losing it AND the home internet together is the case to watch |
| **home internet down** | ✅ yes (mac-mini + thinkpad are still 2 of 3) | cloud-hosted services keep running; agents that need their provider's API stall |
| **macbook sleeps** | ✅ yes | it just stops polling |
| **any 2 voters down** | ❌ no quorum → read-only | fleet stalls (3-node tolerates 1, not 2) |
| **network partition** | majority side writable; minority read-only | Raft prevents split-brain |

---

## 11 — Key paths & operations

| What | Where |
|---|---|
| Orchestrator API | `orchestrator/main.py` · backends `orchestrator/db.py` (sqlite) / `orchestrator/db_rqlite.py` (cluster) |
| Worker | `worker/main.py` · `worker/poller.py` · handlers `worker/handlers/<type>.py` |
| Agents | `agents/runner.py` + `agents/*_agent.py` · routing `config/routing.yaml` |
| Machine roster | `config/machines.yaml` (gitignored, per-host) |
| rqlite runbook / design | [rqlite-failover.md](rqlite-failover.md) · unit template `scripts/rqlite-node.service.example` |

**Rollback:** the pre-cutover SQLite snapshot (`~/Projects/distributed-infra/data/queue.db`)
is untouched. Reverting = set `QUEUE_BACKEND=sqlite` where it is actually defined and restart. Removing it is not
enough: the default is `sqlite` only when NEITHER the service definition (plist `EnvironmentVariables`, unit `Environment=`)
NOR `.env` sets it, and a `QUEUE_BACKEND=rqlite` line in `.env` keeps the cluster in use. Check both places, then restart
(launchd: bootout + bootstrap; systemd: `systemctl --user restart orchestrator`).

**Decommissioned (2026-07-12):** the Phase-1 Litestream replication (the stopgap before
rqlite) has been removed now that the cluster is live — the `com.techstartups.litestream`
launchd job on mac-mini is stopped and disabled, and the thinkpad replica deleted. The
pre-cutover `queue.db` snapshot is kept for cold rollback.

---

## 12 — What's HA, and what isn't

**HA now:** queue data (3-node Raft, survives 1 loss) · orchestrator API (active-active) ·
worker task loop (endpoint failover). The SPOF that started #20 — *primary down = fleet
dead + data lost* — is closed and demonstrated closed.

**Still single-instance (by design / low value):** the WhatsApp bridge (co-located with the
mac-mini orchestrator; both fail together) and the `da` CLI are single-endpoint. A Tailscale
MagicDNS name for the orchestrator would be a convenience but isn't required — client-side
failover already covers it.

### Queue cluster operations

All three voters are addressed by **Tailscale IP** (LAN addresses drift; `/machines` shows each machine's
current LAN IPs and whether Tailscale reaches it directly or through a relay).

- **Version:** all nodes run the same rqlite version. v10.2.7 has **no promote/demote command**: to change a
  node between voter and non-voter, `DELETE /remove` it, then restart it with/without `-raft-non-voter` and
  `-join <other-node>:4002` (it re-syncs from a snapshot in about a second).
- **Changing a node's advertised address:** restart it with the new `-raft-adv-addr` and `-join` while a
  quorum is running.
- **Before any membership change:** `curl <THIS_TAILSCALE_IP>:4001/db/backup -o ~/Backups/rqlite-queue-<date>.sqlite`.
- **Never** publish 4001/4002 on a public interface: rqlite here has no authentication. Reach it over Tailscale only.
- Leadership sits wherever the last election put it (it may be the cloud host); it does not matter for
  correctness. `.stepdown <node>` in the rqlite CLI asks for a move but did not take effect in testing, so do
  not rely on it.

---

## 13 — The Scrum desk (planning and delivery on top of the queue)

On top of the task queue sits a Scrum layer driven by the `dis-agent` CLI
(`scripts/dis-agent`, `scripts/dis_agent.py`). It turns a request into reviewed, merged work:

| Step | Command | Who |
|---|---|---|
| Propose stories | `dis-agent po` | Product Owner (Claude Opus, capped per day) |
| Plan a story: tasks, roles, machines, order | `dis-agent plan` | Scrum Master (Claude Sonnet) |
| Approve exactly that plan | `dis-agent approve` | the owner |
| Run it | `dis-agent go` | developers on mac-mini / thinkpad |
| Independent acceptance tests | `dis-agent validate` | tester (agy) |
| Code review | `dis-agent ask --role reviewer` | reviewer (Codex, read-only) |
| Close | `dis-agent close` | **only the owner**, after seeing validation and review |

Rules the desk enforces: plans are bound to a digest, so editing the story or tasks voids the approval;
stories build in a separate clone and branch (`dis/<project>-<sprint>-S-<n>`); a story closes only on the
owner's confirmation; DeepSeek never sees private data (the privacy guard in `config/routing.yaml`).
Roles and who may use which agent: [AGENT_ROSTER_AND_ACCESS.md](AGENT_ROSTER_AND_ACCESS.md).
Plans and run logs are local files under `data/scrum/` (gitignored); the backlog and sprint notes live in the
owner's notes repository.

The **Vantage** monitor (a separate service) shows the board and each component's health. Every component
reports status, availability and errors to Vantage with the drop-in reporter in `clients/fleet-mcp/vantage_reporter.py`.
