# distributed-infra — Architecture

**As of 2026-07-12.** The current, running architecture of the fleet — after the
#20 high-availability work. This is the "how it actually works now" reference;
design rationale lives in [[rqlite-failover]] and the track history in [[ROADMAP]].

---

## 1 — What it is

A personal **distributed task queue with a worker fleet**, running across home
machines on a private **Tailscale** network. The contract is a single **queue**:
producers create tasks, workers pull the ones they can run, CLI agents do the
reasoning, results write back to the same task row. Control flows one direction —
**human → queue → worker → agent** — with no agent-to-agent hierarchy.

As of the #20 work the queue is **highly available**: its state lives in a 3-node
Raft cluster and the orchestrator API runs active-active, so **the fleet survives
any single machine failing — including the primary** (demonstrated live).

Stack: FastAPI · httpx · Pydantic · **rqlite** (Raft-backed SQLite) · subscription-CLI agents.

---

## 2 — Fleet topology

Tailscale tailnet (`REDACTED-TAILNET.ts.net`). Auth between services is a shared
`SECRET_KEY` in an `x-secret-key` header. Machine liveness in `/machines` is derived
from Tailscale itself (`tailscale status`), with worker-poll recency as a separate
`worker_active` signal.

| Machine | Tailscale IP | Role in the system | Supervision |
|---|---|---|---|
| **mac-mini** | REDACTED-MACMINI-IP | orchestrator + worker + **rqlite voter** + WhatsApp bridge + WAHA | launchd |
| **thinkpad-x1** | REDACTED-THINKPAD-IP | orchestrator + worker + **rqlite voter** | systemd `--user` (lingering) |
| **daffy** (Synology NAS) | REDACTED-NAS-IP | **rqlite voter** only | Docker `--restart` + DSM boot task |
| **macbook-pro** | REDACTED-ORCHESTRATOR-IP | worker only (sleeps); canonical dev checkout | launchd |

- **mac-mini** is the always-on primary (orchestrator + bridge + a voter).
- **thinkpad-x1** is the always-on secondary (2nd orchestrator + a voter).
- **daffy** is a voter-only NAS — the 3rd Raft vote for real quorum, no compute role.
- **macbook-pro** is a worker that may sleep; it is *not* a rqlite node.

Two git checkouts: dev is `~/Projects/github/techstartups/distributed-infra` (macbook);
mac-mini + thinkpad run from `~/Projects/distributed-infra` (same repo, deploy via
`git pull`). See [[infra-deployment-topology]].

---

## 3 — The HA queue (rqlite cluster)

The queue is a **3-node rqlite v10.2.7 Raft cluster** replacing the old single SQLite
file. Each node serves HTTP on **:4001** and runs Raft on **:4002**, advertising its
tailnet IP. Writes route to the elected leader (any node forwards); a write is durable
on a **quorum (2 of 3)** before it acks. Tolerates any one node down.

| Node | How it runs | Data |
|---|---|---|
| synology | Docker `rqlite/rqlite`, `--network host`, `--entrypoint rqlited`, `-bootstrap-expect 1` | named volume |
| mac-mini | Docker `rqlite/rqlite`, `-p 4001:4001 -p 4002:4002` (macOS host-net is VM-scoped) | named volume |
| thinkpad | native linux binary `~/rqlite/rqlited`, systemd unit `rqlite-node.service` | `~/rqlite-data` |

Nodes join over the **Raft** port (`-join <ip>:4002`; rqlite v10 rejects the HTTP port
and the `http://` scheme). The schema is one `tasks` table; the orchestrator creates it
at startup (`ensure_schema`). Details + the reboot-persistence story (incl. the Synology
Tailscale userspace→TUN fix) are in [[rqlite-cluster]].

**Validated:** cross-node replication; a chaos drill (250 tasks, 8 concurrent claimers,
leader hard-killed 3× under load → 0 double-claims, 0 losses, always recovered to 3/3).

---

## 4 — Orchestrator (active-active)

Two orchestrator instances front the same rqlite queue. Each is stateless given the
cluster, so both serve identical results.

- **mac-mini** — launchd `com.techstartups.orchestrator`, port 8000.
- **thinkpad** — systemd `--user` `orchestrator.service`, port 8000.
- Both: `QUEUE_BACKEND=rqlite`, `RQLITE_URL=http://localhost:4001` (each talks to its
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
down. Set to `http://REDACTED-MACMINI-IP:8000,http://REDACTED-THINKPAD-IP:8000` on the thinkpad +
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
deepseek, groq, content, social** — no API keys (they use their own login sessions;
only `claude` is multi-turn). When a caller routes by `task_kind`/`sensitivity`,
`config/routing.yaml` picks the cheapest agent+model:

- privacy / coding / planning → `claude · sonnet` · reasoning/bulk → `deepseek` · mechanical → `claude · haiku`
- **Privacy guard (hard):** email/finance/calendar/personal never leave to DeepSeek (China-hosted).
- **Opus is blocked** fleet-wide; all machines default to **Sonnet** (`~/.claude/settings.json`).

Headless `claude` needs the workspace trusted + an active login on each worker; a lapsed
login is a recurring failure mode ([[mac-mini-claude-oauth-keychain]]).

---

## 9 — Ingress / interfaces

- **WhatsApp bridge** — `services/whatsapp-bridge/bridge.py` on mac-mini (:3001) + WAHA
  Docker (:3000, NOWEB). Self-chat commands → tasks; polls results → replies. Family
  roster (#17), morning-brief scheduler. A `FAILED` WAHA session needs `/restart`, not
  `/start` ([[whatsapp-waha-failed-needs-restart]]).
- **`da` CLI** (`orchestrator/da.py`) — terminal control (queue, status, review, resolve).
- **Direct HTTP API** — any client with the secret key.

---

## 10 — Failure behavior

| Event | Queue writable? | Fleet impact |
|---|---|---|
| **mac-mini down** | ✅ yes (thinkpad + synology = quorum) | thinkpad orchestrator + thinkpad/macbook workers keep running (failover); WhatsApp down (bridge is on mac-mini); mac-mini-pinned tasks wait |
| **thinkpad down** | ✅ yes (mac-mini + synology) | mac-mini orchestrator + workers keep running; Android-pinned tasks wait |
| **synology down** | ✅ yes (mac-mini + thinkpad) | none — it's voter-only |
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
| rqlite runbook / topology | [[rqlite-cluster]] · design [[rqlite-failover]] |

**Rollback:** the pre-cutover SQLite snapshot (`~/Projects/distributed-infra/data/queue.db`)
is untouched. Reverting = remove `QUEUE_BACKEND` from the orchestrator plist + bootout/bootstrap.

**Superseded:** the Phase-1 Litestream replication ([[litestream-runbook]]) still runs on
mac-mini but now mirrors the frozen pre-cutover SQLite file — obsolete once you trust the
rqlite cluster; safe to decommission (`launchctl bootout … com.techstartups.litestream`).

---

## 12 — What's HA, and what isn't

**HA now:** queue data (3-node Raft, survives 1 loss) · orchestrator API (active-active) ·
worker task loop (endpoint failover). The SPOF that started #20 — *primary down = fleet
dead + data lost* — is closed and demonstrated closed.

**Still single-instance (by design / low value):** the WhatsApp bridge (co-located with the
mac-mini orchestrator; both fail together) and the `da` CLI are single-endpoint. A Tailscale
MagicDNS name for the orchestrator would be a convenience but isn't required — client-side
failover already covers it.
