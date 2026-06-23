# Agent Terminal (Flutter)

A terminal-style chat client for the distributed agent fleet. Talks **directly to
the orchestrator queue API over Tailscale** — independent of the WhatsApp bridge.

See `../../docs/flutter-terminal.md` for the architecture.

## What it does (MVP)
- Pick an agent (`claude`, `agy`, `codex`, `groq`, `content`, `social`), type a
  prompt, send → it enqueues an `agent_run` task and polls for the result.
- **Multi-turn** for `claude`: replies continue the same conversation (a generated
  `session_id` with `resume`); "New session" or switching agent resets it.
- Settings: orchestrator URL, secret key, target machine (saved on device).

## Run it
This folder holds the Dart sources + `pubspec.yaml`. Generate the platform
scaffolding once, then run:

```bash
cd clients/flutter-terminal
flutter create .          # generates ios/ android/ etc. (keeps existing lib/ + pubspec)
flutter pub get
flutter run               # on a device/simulator that's on the Tailscale network
```

First launch → open **Settings** (gear icon) and set:
- **Orchestrator URL** — `http://100.97.176.37:8000` (MacBook Tailscale IP), or the
  MagicDNS name, e.g. `http://<macbook>.tail8bbe59.ts.net:8000`
- **Secret key** — the fleet `SECRET_KEY` (same value the workers/bridge use)
- **Target machine** — `mac-mini`

> The phone must be on the tailnet. `claude`/`agy`/`codex` must be logged in on the
> target machine (same as for the WhatsApp bridge).

## Not yet (see scrum BLI-054/055)
- Fleet status + task history views
- Artifact download (needs a token endpoint on the orchestrator)
- Live streaming (needs an SSE endpoint on the orchestrator)
