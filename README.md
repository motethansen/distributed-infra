# distributed-infra

Private mesh of three machines connected via [Tailscale](https://tailscale.com).

| Machine | Tailscale IP | Role | Speciality |
|---|---|---|---|
| `michaels-macbook-pro` | 100.97.176.37 | **Orchestrator** | Main dev, queue server, CLI |
| `michaelhansen-thinkpad-x13-gen-4` | 100.112.241.6 | **Worker** | Android builds, backend dev |
| `mac-mini` | 100.76.214.54 | **Worker** | iOS / Xcode builds |

## Architecture

```
MacBook Pro
  ├── FastAPI queue server  (port 8000)   ← workers poll this
  └── CLI (orch)                          ← you push tasks / review escalations

ThinkPad / Mac Mini
  └── FastAPI worker        (port 8001)
        ├── polls queue every N seconds
        ├── claims & runs tasks
        └── escalates to "needs_human" when it can't proceed
```

Workers only communicate with the MacBook queue. No direct worker-to-worker traffic.

## Quick start

### 1. Clone on each machine
```bash
git clone git@github.com:<org>/distributed-infra.git
cd distributed-infra
```

### 2. Create `.env` (not committed — share via 1Password / Tailscale file transfer)
```bash
cp .env.example .env
# Edit .env: set MACHINE_NAME, MACHINE_ROLE, SECRET_KEY (same on all machines)
```

### 3. Run setup script for your machine
```bash
# MacBook Pro
bash scripts/setup-macbook.sh

# ThinkPad (Ubuntu)
bash scripts/setup-ubuntu.sh

# Mac Mini
bash scripts/setup-macmini.sh
```

### 4. Start services
```bash
# MacBook — queue server
source .venv/bin/activate
uvicorn orchestrator.main:app --host 0.0.0.0 --port 8000

# ThinkPad / Mac Mini — worker
source .venv/bin/activate
uvicorn worker.main:app --host 0.0.0.0 --port 8001
```

### 5. Verify connectivity
```bash
bash scripts/health-check.sh
```

## CLI usage (MacBook only)

```bash
source .venv/bin/activate

# Check all machines
python orchestrator/cli.py status

# Push a task
python orchestrator/cli.py push android_build \
  --payload '{"project_path":"/home/michael/myapp","variant":"assembleDebug"}'

# Push an iOS build
python orchestrator/cli.py push ios_build \
  --payload '{"project_path":"/Users/michael/MyApp","scheme":"MyApp","action":"build"}'

# List queue
python orchestrator/cli.py ls
python orchestrator/cli.py ls --status needs_human

# Review tasks that need your action
python orchestrator/cli.py review

# Resolve a task
python orchestrator/cli.py resolve <task-id> --action done

# SSH into a machine by name
python orchestrator/cli.py ssh mac-mini
python orchestrator/cli.py ssh michaelhansen-thinkpad-x13-gen-4 --command "df -h"
```

## Task types

| Type | Handler | Machine |
|---|---|---|
| `android_build` | Gradle wrapper | ThinkPad |
| `ios_build` | xcodebuild | Mac Mini |
| `git_pull` | git | Any worker |
| `run_script` | shell | Any worker |
| `human_action` | — | You (MacBook review) |

## Adding capabilities to a worker

Edit `.env` on the worker machine:
```
MACHINE_CAPABILITIES=android_build,gradle,git_pull,run_script
```
Restart the worker. The poller only claims tasks matching its capabilities.

## Ubuntu systemd service
```bash
sudo cp scripts/worker.service /etc/systemd/system/infra-worker.service
# Edit the unit file: replace %i with your username
sudo systemctl daemon-reload
sudo systemctl enable --now infra-worker
```
