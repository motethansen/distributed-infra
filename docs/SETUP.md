# Setup and testing

Run the stack on one machine in about ten minutes, test it, then grow it into a fleet.
Nothing here needs a real secret, a Tailscale account or a cloud host until you reach step 4.

Related: [ARCHITECTURE.md](ARCHITECTURE.md) (how it fits together) · [SECURITY.md](SECURITY.md) (secrets, public-repo rules).

## 1. What you need

| For | You need |
|---|---|
| Everything | Python 3.11 or 3.12, `git`, `curl`, `openssl` |
| A fleet (step 4) | [Tailscale](https://tailscale.com) on every machine, SSH between them |
| The agents (step 6) | The CLI of each agent you want (Claude Code, Codex, Gemini/Antigravity), each logged in once |
| HA queue (step 5, optional) | Docker on two hosts, a Linux binary of [rqlite](https://rqlite.io) on a third |

## 2. Try it on one machine

```bash
git clone https://github.com/motethansen/distributed-infra.git
cd distributed-infra
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

**Smoke test** (starts an orchestrator and a worker on localhost with a throwaway secret, runs one task, cleans up):

```bash
scripts/smoke-test.sh
# ok   orchestrator is up
# ok   task <id> queued
# ok   a worker claimed the task and returned its output
# PASS
```

**Run it by hand**, so you can poke at it. Create your own `.env` (it is gitignored):

```bash
cp .env.example .env && chmod 600 .env
# edit .env: set MACHINE_NAME, and SECRET_KEY to the output of:  openssl rand -hex 32
```

Two terminals (or two background jobs):

```bash
# 1. orchestrator: the queue API on :8000 (SQLite file in ./data, nothing else to install)
scripts/run-with-env.sh -- .venv/bin/uvicorn orchestrator.main:app --host 127.0.0.1 --port 8000

# 2. worker: polls the orchestrator and runs tasks it has the capability for
MACHINE_ROLE=worker MACHINE_CAPABILITIES=run_script,git_pull \
  scripts/run-with-env.sh -- .venv/bin/uvicorn worker.main:app --host 127.0.0.1 --port 8001
```

`scripts/run-with-env.sh` reads `.env` for you (see [SECURITY.md](SECURITY.md)); you can instead `export` the same variables in your shell.

Submit a task and read the result:

```bash
SECRET=$(grep '^SECRET_KEY=' .env | cut -d= -f2-)
ID=$(curl -s -X POST localhost:8000/tasks -H "x-secret-key: $SECRET" -H 'content-type: application/json' \
      -d '{"type":"run_script","payload":{"script":"uname -a"}}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')
sleep 12   # one poll interval
curl -s localhost:8000/tasks/$ID -H "x-secret-key: $SECRET"
```

Or use the interactive CLI: `./da` (type `help`). `GET /health` needs no key; everything else needs the `x-secret-key` header.

## 3. Run the tests

```bash
.venv/bin/pip install pytest pytest-asyncio
.venv/bin/python -m pytest -q          # ~450 tests, about 20 seconds, no network, no secrets
```

CI runs the same suite on Python 3.11 and 3.12, plus a secret scan (`.github/workflows/ci.yml`).
Before pushing, run `scripts/check-secrets.sh` (needs [gitleaks](https://github.com/gitleaks/gitleaks); `brew install gitleaks`)
and install the commit hook once with `scripts/install-git-hooks.sh`.

## 4. Make it a fleet (several machines over Tailscale)

1. Install Tailscale on each machine and sign in to the same tailnet. Note each address: `tailscale ip -4`.
2. Clone the repo on each machine and create `.venv` (step 2). Convention: `~/Projects/distributed-infra`.
3. Create one `.env` per machine from `.env.example`. **`SECRET_KEY` must be identical everywhere.** Set
   `ORCHESTRATOR_URLS=http://<orchestrator-ip>:8000` (comma-separate several orchestrators for failover),
   `MACHINE_NAME`, `MACHINE_ROLE` and `MACHINE_CAPABILITIES`. To copy one `.env` to the others:
   `THINKPAD_IP=… MACMINI_IP=… bash scripts/sync-env.sh` (it uses `scp` over Tailscale).
4. `cp config/machines.yaml.example config/machines.yaml` and fill in each machine's address and capabilities.
   This file is gitignored.
5. Pick the machine(s) that run the orchestrator (always-on ones). Install the services below.
6. Check: `curl -s -H "x-secret-key: $SECRET" http://<orchestrator-ip>:8000/machines` lists every machine with `online: true`.

**Services that start at boot.** All templates contain no secrets; they read `.env`.

| OS | Orchestrator | Worker |
|---|---|---|
| macOS (launchd) | `scripts/com.techstartups.orchestrator.plist.example` | `scripts/com.techstartups.worker.plist.example` |
| Linux (systemd, user) | `scripts/orchestrator.service` | `scripts/worker.service` |

macOS:

```bash
mkdir -p logs && chmod 600 .env
cp scripts/com.techstartups.worker.plist.example ~/Library/LaunchAgents/com.techstartups.worker.plist
# edit the copy: replace /Users/YOUR_USERNAME/Projects/distributed-infra with your checkout
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.techstartups.worker.plist
```

Linux:

```bash
mkdir -p ~/.config/systemd/user && cp scripts/worker.service ~/.config/systemd/user/infra-worker.service
systemctl --user daemon-reload && systemctl --user enable --now infra-worker
loginctl enable-linger "$USER"      # keep running after logout and across reboots
```

**Updating a machine:** `git pull --ff-only`, then restart its services
(`launchctl bootout gui/$(id -u)/com.techstartups.worker && launchctl bootstrap …`, or `systemctl --user restart infra-worker`).
Workers load their code at start, so a worker keeps running old code until it is restarted.

## 5. A highly available queue (optional)

One SQLite file on one machine is a single point of failure. For a queue that survives a machine going down, run a
3-node [rqlite](https://rqlite.io) cluster on three always-on hosts of the tailnet (two machines of yours plus one small
cloud host is enough) and set `QUEUE_BACKEND=rqlite` on the orchestrators. Design and drill results: [rqlite-failover.md](rqlite-failover.md).

Use **Tailscale addresses** for every `-http-adv-addr` and `-raft-adv-addr`. rqlite has no authentication in this setup,
so never publish 4001/4002 on a public interface.

First node (a brand-new cluster), on a host with Docker:

```bash
docker run -d --name rqlite --restart unless-stopped \
  -p <THIS_TAILSCALE_IP>:4001:4001 -p <THIS_TAILSCALE_IP>:4002:4002 -v rqlite-data:/rqlite/file \
  rqlite/rqlite:10.2.7 -node-id node1 -bootstrap-expect 1 \
  -http-addr 0.0.0.0:4001 -http-adv-addr <THIS_TAILSCALE_IP>:4001 \
  -raft-addr 0.0.0.0:4002 -raft-adv-addr <THIS_TAILSCALE_IP>:4002 \
  -raft-election-timeout 5s -raft-heartbeat-timeout 5s -raft-apply-timeout 30s
```

Every further node uses the same command with its own `-node-id` and address, plus `-join <FIRST_NODE_TAILSCALE_IP>:4002`
(join over the **Raft** port, no `http://`). On Linux without Docker use [scripts/rqlite-node.service.example](../scripts/rqlite-node.service.example).
Then:

```bash
curl -s "http://<any-node-ip>:4001/nodes?nonvoters&timeout=3s"     # expect 3 voters, all reachable
# on each orchestrator's .env:
#   QUEUE_BACKEND=rqlite
#   RQLITE_URL=http://localhost:4001
```

Workers take several orchestrators in `ORCHESTRATOR_URLS` and fail over between them.

## 6. The agents

Each agent runs through its own CLI and its own login on every worker, so no API key is stored for them:

| Agent | Install | Log in once |
|---|---|---|
| Claude Code | `npm install -g @anthropic-ai/claude-code` | `claude login` |
| Codex | `npm install -g @openai/codex` | `codex login` |
| Gemini / Antigravity | per its documentation | per its documentation |
| DeepSeek | none (HTTP API) | set `DEEPSEEK_API` in `.env` |

Routing, fallbacks and the privacy rule (private data never goes to DeepSeek) are in `config/routing.yaml`.
Test an agent on a worker: `./da`, then `assign say hello --agent=claude --type=agent_run --machine=<worker>` (see the README CLI reference).

## 7. Optional parts

- **WhatsApp bridge:** [services/whatsapp-bridge/README.md](../services/whatsapp-bridge/README.md). Its credentials live in `services/whatsapp-bridge/.env`.
- **Scrum desk** (`scripts/dis-agent`): plans, approves and runs stories on the workers; copy `config/scrum_projects.yaml`
  and edit it for your repos. See [ARCHITECTURE.md](ARCHITECTURE.md) §13 and [AGENT_ROSTER_AND_ACCESS.md](AGENT_ROSTER_AND_ACCESS.md).
- **Use the fleet from Claude Code, Codex and others:** [clients/fleet-mcp/README.md](../clients/fleet-mcp/README.md).

## 8. When something does not work

| Symptom | Check |
|---|---|
| Worker logs `Poller error: ConnectError` | Tailscale up on both machines; `ORCHESTRATOR_URLS` correct; orchestrator listening on its Tailscale address (`QUEUE_HOST=0.0.0.0` or the tailnet IP) |
| `401`/`403` from the API | `SECRET_KEY` differs between machines, or the `x-secret-key` header is missing |
| Task stays `pending` | No online worker advertises that task type in `MACHINE_CAPABILITIES`, or `_target_machine` names a machine that is offline |
| Queue returns `503` | rqlite lost quorum (2 of 3 nodes down); `curl …/nodes?nonvoters&timeout=3s` shows which |
| A service ignores a changed `.env` | Restart it; the file is read at start |
| `run-with-env: … mode 644` | `chmod 600 .env` |

More cases: the Troubleshooting section of the [README](../README.md).
