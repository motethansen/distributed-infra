# distributed-infra — Orchestrator HA via rqlite  ·  `planned`

**Date:** 2026-07-12 · **Owner:** Michael · **Status:** design, not started

One-line: make the task queue survive any single machine by moving it from a
single-node SQLite file on mac-mini to a **3-node rqlite (Raft-backed SQLite)
cluster**, so the orchestrator stops being a single point of failure — without
throwing away the pull-based worker model or the deterministic SQL placement
that already work well.

---

## 1 — Problem

Today the orchestrator + queue are a **single point of failure** on mac-mini
(see [[orchestrator-spof-on-laptop]], [[infra-deployment-topology]]):

- `orchestrator/db.py` writes one SQLite file (`data/queue.db`) on mac-mini.
- `orchestrator/main.py` (FastAPI, `:8000`) is the only process that reads/writes it.
- Every client — the WhatsApp bridge, all workers (`worker/poller.py`), the `da`
  CLI — hardcodes `ORCHESTRATOR_URL = http://<mac-mini-ip>:8000`.

If mac-mini is down, asleep, or its process is wedged, the **entire fleet
stalls**: no task can be created, claimed, or completed, and any queue state that
was only on that disk is unreachable. Relocating the orchestrator off the laptop
to the always-on mac-mini (2026-06-30) reduced the *frequency* of this but did
not remove the SPOF.

### Why not "an orchestrator on every machine, pushing work to each other"

The tempting fix — run an independent orchestrator per machine and have the busy
one push tasks to the others — **replaces a SPOF with a worse problem: split-brain.**
Independent queues mean independent state: a task created on one node is invisible
to the others, "what's the status of task X?" has N different answers, and a task
pushed to two nodes gets claimed and executed twice (two git pushes, two WhatsApp
replies, two trades). That is a hand-rolled consensus problem, and hand-rolled
consensus is where this kind of system goes to die.

**The reframe that makes the instinct correct:** the orchestrator does two jobs —
(a) *serve the API + decide placement* (stateless given the DB) and (b) *hold the
durable queue state* (the DB). Job (a) is trivially replicable; job (b) is the hard
part. Running an orchestrator on every machine is fine **once they all share one
consistent queue.** rqlite provides exactly that shared, consistent queue. So the
end state actually delivers the collaboration the original idea was reaching for —
active-active orchestrators — but safely, because the state layer is single and
linearizable behind Raft.

---

## 2 — Goals / non-goals

**Goals**
- Tolerate any **one** machine failing (crash, sleep, reboot, network drop) with no
  queue data loss and automatic recovery — no manual promote step.
- Keep SQLite semantics so `orchestrator/db.py`'s SQL barely changes.
- Keep the **pull-based claim model** (`/tasks/claim`) and **capability + placement
  rules** (`_target_machine` / `_preferred_machine`, 20s overflow) exactly as they are.
- Let clients follow the live orchestrator without editing config on every box.

**Non-goals**
- Not scaling throughput — this fleet does a handful of tasks a minute, not thousands.
  HA is the only driver.
- Not multi-region / internet-facing — everything stays on the Tailscale tailnet.
- Not changing the worker/handler model, the agent runner, or model routing.
- Not solving agent-login expiry or WAHA session recovery — those are separate
  operational SPOFs (see [[mac-mini-claude-oauth-keychain]],
  [[whatsapp-waha-failed-needs-restart]]).

---

## 3 — Why rqlite (vs the alternatives)

| Option | HA / failover | Code change | Fit for a 3-node home fleet |
|---|---|---|---|
| **rqlite** (Raft-backed SQLite) | **Automatic** — leader election, tolerates 1 loss | Small — swap the DB layer for its HTTP API; it *is* SQLite | **Recommended** |
| Litestream (SQLite → replica/S3) | Recovery only, **manual promote** | ~none | Good **stopgap** (Phase 1), not true HA |
| Postgres + streaming replica | Automatic with a failover controller | Medium — rewrite SQL/driver, run a server | Heavier than needed |
| Multi-master (per-node queues) | — | Large | **No** — split-brain (see §1) |

rqlite is almost purpose-built for this case: **3 nodes, one elected leader, writes
go through the Raft log, survives any single node loss, and the on-disk store is
SQLite** — so migrating `orchestrator/db.py` is a driver swap, not a rewrite. It
exposes a simple HTTP API (`/db/execute`, `/db/query`) plus a DB-API client
(`pyrqlite`), and it enforces single-writer for us (no fencing to hand-build).

---

## 4 — Target architecture

```
                         Tailscale tailnet
   ┌────────────┐   ┌────────────┐   ┌───────────────┐
   │  mac-mini  │   │ thinkpad-x1│   │  witness node │   ← small always-on box
   │  VOTER     │   │  VOTER     │   │  VOTER         │     (Pi / $5 VPS)
   │  rqlite ●──┼───┼──● rqlite ─┼───┼──● rqlite      │   Raft group (quorum = 2/3)
   │  orch API  │   │  orch API  │   │  (db only)     │
   │  worker    │   │  worker    │   └───────────────┘
   └─────┬──────┘   └─────┬──────┘
         │                │
   ┌─────┴──────┐         │        ┌──────────────┐
   │ macbook-pro│         │        │ clients:     │
   │ NON-VOTER  │─────────┘        │ WhatsApp     │
   │ read replica (sleeps)        │ bridge, da   │
   │ worker                        │ CLI, workers │
   └────────────┘                  └──────┬───────┘
                                          │ resolve → orchestrator.<tailnet>.ts.net
```

- **rqlite cluster** replaces the single `data/queue.db`. Writes route to the Raft
  leader (any node forwards); a committed write is durable on a quorum before it acks.
- **Orchestrator API can run on every voter**, each talking to its *local* rqlite
  node. Because state is single and linearizable behind Raft, this is now safe
  active-active — the original "orchestrator on every machine" idea, done right.
- **Workers keep polling the orchestrator API** over HTTP exactly as today; only the
  DB layer underneath changed.

### 4.1 The quorum gotcha (this decides topology)

3-node Raft tolerates one failure **only if the three nodes are genuinely
independent and available.** A **sleeping MacBook is a poor voter** — it would
constantly drop quorum (the same reason it was demoted from orchestrator in
`config/machines.yaml`). We realistically have **two reliable machines: mac-mini +
thinkpad-x1.** Therefore:

- **Voters (quorum):** mac-mini, thinkpad-x1, **+ one small always-on witness**
  (Raspberry Pi, or a cheap VPS on the tailnet). The witness runs rqlite only — no
  worker, no builds — purely to make quorum = 2-of-3 real.
- **macbook-pro:** join as a **non-voting read replica** (rqlite supports
  read-only followers). It gets the data for local reads but never counts toward
  quorum, so it can sleep freely.

Without a witness you can't get automatic 1-failure tolerance from only two reliable
nodes (2-node Raft can't form quorum when one dies). If a witness is genuinely off
the table, fall back to the Phase-1 Litestream + manual-promote design below and
stop there.

---

## 5 — What changes in the code

### 5.1 `orchestrator/db.py` — DB layer swap

Replace `aiosqlite.connect(DB_PATH)` with an rqlite client (HTTP, or `pyrqlite`
DB-API). The SQL strings are unchanged; only the connection/execute plumbing moves.

- New env: `RQLITE_URL` (e.g. `http://localhost:4001`) — each orchestrator talks to
  its **local** rqlite node; the node forwards writes to the leader.
- `_ensure_schema` runs once at cluster bootstrap, not per-connection.

### 5.2 `claim_next_task` — make the claim a single atomic statement

Today it does `SELECT … LIMIT 1` then a guarded `UPDATE … WHERE status='pending'`
inside one aiosqlite transaction. rqlite favors single-statement (or explicitly
batched-transaction) requests over interactive cross-request transactions. Refactor
the claim into **one atomic write** using `UPDATE … RETURNING`:

```sql
UPDATE tasks
   SET status = 'claimed', assigned_to = :worker, updated_at = :now
 WHERE id = (
     SELECT id FROM tasks
      WHERE status = 'pending'
        AND type IN (:caps)
        AND ( <same _target_machine / _preferred_machine / 20s-overflow predicate> )
      ORDER BY priority DESC, created_at ASC
      LIMIT 1
   )
   AND status = 'pending'
RETURNING *;
```

- Atomic at the Raft leader → no SELECT-then-UPDATE race, no double-claim, even with
  the orchestrator API running on multiple nodes simultaneously. This is strictly
  cleaner than today's two-step-plus-rowcount-guard and is the main required change.
- Read consistency: use rqlite **`weak`** (leader) reads for `/tasks/claim` and
  status checks so a worker never claims off a stale follower; **`none`** (any node,
  possibly stale) is fine for the read-only `da queue` / `status` dashboards.

### 5.3 Discovery — kill the hardcoded `ORCHESTRATOR_URL`

Clients (WhatsApp bridge, `worker/poller.py`, `da`, schedulers) must resolve the
*current* orchestrator rather than `<mac-mini-ip>:8000`:

- **Preferred:** a stable **Tailscale MagicDNS name** — `orchestrator.<tailnet>.ts.net`
  — that maps to whichever voter is serving the API. With active-active API on both
  reliable voters, this can even be a two-address round-robin; a dead node's API just
  fails and the client retries the other.
- Client change is a one-liner: `ORCHESTRATOR_URL` default becomes the DNS name.
- `config/machines.yaml` gains the witness + macbook non-voter roles and each node's
  `rqlite_port` / `rqlite_raft_port`.

### 5.4 Supervision

Each voter runs **two** supervised services: `rqlited` and the orchestrator API
(launchd on macOS, systemd on the Linux witness/thinkpad). rqlite gets its own
data dir per node; bootstrap the cluster once with `-bootstrap-expect 3`, thereafter
nodes rejoin automatically.

---

## 6 — Failure behavior (design target)

| Event | Queue writable? | Worker impact | Recovery |
|---|---|---|---|
| **mac-mini down** | ✅ yes (thinkpad + witness = quorum) | thinkpad/macbook keep claiming; mac-mini-pinned tasks (`ios_build`) wait | mac-mini rejoins, catches up via Raft |
| **thinkpad down** | ✅ yes | mac-mini keeps claiming; Android-pinned tasks wait | auto rejoin |
| **witness down** | ✅ yes (two real voters = quorum) | none | auto rejoin |
| **macbook sleeps** | ✅ yes (non-voter) | it just stops polling | wakes, resyncs |
| **any 2 voters down** | ❌ no quorum → read-only/unavailable | fleet stalls (accepted: 3-node tolerates 1, not 2) | bring one back |
| **network partition** | Majority side stays writable; minority goes read-only | Raft prevents split-brain by design | heals on reconnect |

---

## 7 — Migration plan (staged, each phase shippable)

**Phase 0 — Reconcile the two checkouts (prerequisite).** The live orchestrator runs
from `~/Projects/distributed-infra`, not the git checkout at
`~/Projects/github/techstartups/distributed-infra` (see [[infra-deployment-topology]]).
Pick one canonical deployment path before adding HA, or we'll cluster the wrong copy.

**Phase 1 — Litestream stopgap (days, ~no code).** Continuously replicate the current
`queue.db` mac-mini → thinkpad + a scripted promote (restore, start orchestrator, flip
the Tailscale name). Removes the "data is gone if mac-mini dies" risk immediately, with
manual failover. Ship this first for peace of mind while Phase 2 is built.

**Phase 2 — rqlite cluster (the real work).**
1. Stand up 3 rqlite nodes (mac-mini, thinkpad, witness); macbook as non-voter.
2. Port `orchestrator/db.py` to the rqlite client behind the same function
   signatures; refactor `claim_next_task` to the atomic `UPDATE … RETURNING` (§5.2).
3. One-time import of existing tasks into the cluster.
4. Run orchestrator API against local rqlite on both reliable voters (active-active).

**Phase 3 — Client cutover.** Point `ORCHESTRATOR_URL` at the MagicDNS name across the
bridge, workers, `da`, schedulers. Decommission the single-node path.

---

## 8 — Risks & open questions

- **Write latency:** Raft commit adds a round-trip vs a local file write. At this
  fleet's task rate it's negligible, but confirm `/tasks/claim` stays well under the
  10s poll interval.
- **Witness node:** requires one more always-on box. If unavailable, HA degrades to
  Phase-1 (manual promote). Decision needed: Pi vs VPS vs "accept manual failover."
- **Bootstrap/ops burden:** two supervised services per node, cluster bootstrap,
  backup of the Raft data dir. More moving parts than one SQLite file — the price of
  HA. Document the bootstrap + disaster-recovery runbook alongside this doc.
- **rqlite transaction model:** any handler doing multi-statement interactive
  transactions must move to batched/transactional requests. Audit for these (the
  claim is the only hot path today).
- **Clock/consistency for the overflow predicate:** the 20s grace uses
  `julianday('now')` evaluated at the leader — fine, but note it's leader-clock, not
  per-node, once active-active.

---

## 9 — Testing / acceptance

- **Chaos drills:** kill each node in turn (mac-mini, thinkpad, witness) under a
  steady trickle of tasks; assert no task is lost, double-claimed, or double-executed,
  and the queue stays writable when exactly one voter is down.
- **Partition test:** firewall the witness+thinkpad off from mac-mini; assert the
  majority side keeps serving and the minority goes read-only (no split-brain).
- **Client failover:** stop the API on the node the MagicDNS name currently favors;
  assert the WhatsApp bridge and workers reconnect to the other voter within one poll.
- **Idempotency check:** since HA raises the chance of retried writes, confirm
  task-completion is safe to re-apply (it is today — `complete`/`fail` are simple
  status writes — but re-verify after the claim refactor).

---

## 10 — Recommendation

Ship **Phase 1 (Litestream)** now for immediate durability, then build **Phase 2/3
(rqlite)** for true automatic failover, contingent on adding one small always-on
witness node. The payoff is exactly the collaboration the original "orchestrator on
every machine" idea wanted — active-active orchestrators — made safe by putting a
single consistent, replicated queue underneath them.

---

## 11 — Phase 2 PoC results (2026-07-12)

The rqlite backend adapter is built and **validated end-to-end** against a live
rqlite v10.2.7 node.

- **`orchestrator/db_rqlite.py`** — same function signatures as `orchestrator/db.py`
  (drop-in via `QUEUE_BACKEND=rqlite`, wiring is a follow-up), talking to the rqlite
  HTTP API: `/db/execute` (writes), `/db/query` (reads, with a consistency `level`),
  `/db/request` for the atomic claim.
- **`claim_next_task` refactored** to a single `UPDATE … WHERE id=(SELECT … LIMIT 1)
  AND status='pending' RETURNING *` via `/db/request` — the SELECT-then-UPDATE race
  is gone.
- **`scripts/rqlite_poc_test.py`** — exercises schema/insert/get/list, placement
  rules, and concurrency. **All checks pass**, including the one that matters:
  **12 concurrent claims → exactly 12 unique tasks, zero double-claims**, with
  priority order + hard-pin + soft-pref-grace all respected.

### Deployment findings (feed into the cluster build)

- **rqlite v10 ships NO macOS binary** (Linux/Windows only). So on **mac-mini** rqlite
  must run as a **Docker container** (OrbStack is already there for WAHA). **thinkpad**
  runs the native Linux binary. This does not affect the orchestrator process (it's an
  HTTP client of rqlite).
- **Witness = the Synology NAS** (already on the tailnet, always-on) as the 3rd voter,
  running rqlite via Container Manager (Docker). Voters: **mac-mini + thinkpad +
  Synology**; **macbook-pro** joins as a **non-voting read replica** so it can sleep.
  This resolves the open witness decision at zero new hardware cost.
- **Supervise rqlite with the platform's init**, not hand-backgrounding: `systemd`
  on thinkpad (a `systemd-run --user` transient unit worked cleanly in the PoC),
  Docker restart-policy on mac-mini + Synology.

### Remaining for the cluster (next)

1. Stand up the 3-node cluster (mac-mini Docker + thinkpad native + Synology Docker),
   macbook as non-voter.
2. Wire `QUEUE_BACKEND=rqlite` + `RQLITE_URL` into the orchestrator (one selector).
3. Import existing tasks; run the orchestrator API active-active on the two reliable
   voters behind a Tailscale MagicDNS name (Phase 3 discovery).
4. Chaos-drill (kill each voter under load; assert no lost/double-claimed tasks).
