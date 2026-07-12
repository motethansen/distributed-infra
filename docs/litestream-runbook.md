# Litestream failover — operational runbook  ·  `shipped` (Phase 1, 2026-07-12)

The **Phase 1 stopgap** from [[rqlite-failover]]: continuously replicate the
orchestrator's SQLite queue off mac-mini so the fleet's task state survives losing
the primary. Failover is **manual** (automatic failover is Phase 2 / rqlite) — this
doc is the runbook.

- **Primary:** mac-mini `REDACTED-MACMINI-IP` — runs the orchestrator + `litestream replicate`.
- **Standby:** thinkpad-x1 `REDACTED-THINKPAD-IP` — receives the replica via SFTP over Tailscale.
- **DB:** `~/Projects/distributed-infra/data/queue.db` (WAL mode).
- **Replica on standby:** `~/litestream-replicas/queue/` (sync interval 1s).

## What's deployed

| Where | What |
|---|---|
| mac-mini | litestream v0.3.13 → `/usr/local/bin/litestream` |
| mac-mini | config `config/litestream.yml`; launchd job `com.techstartups.litestream` (KeepAlive) → logs `/tmp/litestream.{log,err}` |
| mac-mini→thinkpad | SSH key auth (mac-mini `id_ed25519` in thinkpad `authorized_keys`) |
| thinkpad | litestream v0.3.13 → `~/.local/bin/litestream`; replica dir `~/litestream-replicas/queue` |

## Health checks

```bash
# On mac-mini — is replication alive and syncing?
launchctl print gui/$(id -u)/com.techstartups.litestream | grep -E 'state|pid'
tail -f /tmp/litestream.log                     # look for periodic "write snapshot" / wal sync

# On thinkpad — is the replica fresh and restorable?
find ~/litestream-replicas/queue -path '*snapshots*' -type f | wc -l   # ≥1
scripts/litestream-restore.sh /tmp/check.db     # prints restored task count
```

The restored count trails live by ~seconds (1s sync). A gap of a few tasks is
normal; a gap that never shrinks means replication is stuck — check the mac-mini log.

## Failover (primary lost)

Run **on the standby** once you've confirmed mac-mini is really down (a live primary
+ a promoted standby on the same queue = split-brain):

```bash
scripts/failover-promote.sh
```

It backs up any existing DB, restores the latest queue from the replica into the
orchestrator's `QUEUE_DB_PATH`, starts the orchestrator locally if a checkout+venv
exist, then prints the client-cutover steps (repoint `ORCHESTRATOR_URL` / the
Tailscale `orchestrator` name, restart the WhatsApp bridge + workers + `da`).

> **Phase-1 limitation:** the standby must have an orchestrator checkout + venv for
> the auto-start step; otherwise the script restores the DB and you start the
> orchestrator by hand. Making the standby fully turnkey (and automating cutover)
> is Phase 2.

## Failback (primary returns)

1. Decide the authoritative queue (usually the promoted standby's current DB).
2. Get that DB onto mac-mini (restore from the standby's replica, or copy it over).
3. Repoint clients back to mac-mini; start its orchestrator.
4. Re-establish replication in the correct direction **last**, so you never
   overwrite the good copy. Only one orchestrator per logical queue, ever.

## Re-provisioning from scratch

1. **Install litestream v0.3.13** — mac-mini: unzip `…-darwin-amd64.zip` → `/usr/local/bin`;
   thinkpad: untar `…-linux-amd64.tar.gz` → `~/.local/bin`.
2. **SSH key:** mac-mini `~/.ssh/id_ed25519.pub` → thinkpad `~/.ssh/authorized_keys`; `mkdir -p ~/litestream-replicas/queue` on thinkpad.
3. **WAL:** `sqlite3 <db> "PRAGMA journal_mode=WAL;"` on the primary.
4. **Config:** `config/litestream.yml` (paths/host as above).
5. **Supervise:** copy `services/litestream/com.techstartups.litestream.plist` (fix `YOUR_USERNAME`) to `~/Library/LaunchAgents/` and `launchctl bootstrap gui/$(id -u) …`.

## Notes / gotchas

- **WAL is required.** If the DB ever gets reset to `journal_mode=delete`, Litestream stops tracking changes. The app (aiosqlite, short-lived connections) inherits WAL from the file, so setting it once is enough.
- **Reconcile the two checkouts** ([[infra-deployment-topology]]): the live orchestrator runs from `~/Projects/distributed-infra`, so that's the DB being replicated — not the git checkout under `~/Projects/github/techstartups/…`.
- Litestream **v0.5.x is a rewrite** with a different config/replica model; this setup is pinned to **v0.3.13** deliberately. Don't auto-upgrade without re-testing.
- This is a **stopgap**. The durable fix is Phase 2 (rqlite, automatic failover) — see [[rqlite-failover]].
