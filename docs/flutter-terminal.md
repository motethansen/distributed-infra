# Flutter Terminal Client — architecture & plan

A phone/desktop chat client for the distributed agent fleet. It is a **thin client
over the existing orchestrator queue API** (over Tailscale) — no WhatsApp, no Waha.
The WhatsApp bridge stays as-is; this is a parallel, richer interface.

Lives in `clients/flutter-terminal/`.

## Why
The WhatsApp bridge is great for quick, anywhere control, but a dedicated app adds:
native file handling (no Waha-Plus limit), live streaming, multiple concurrent
sessions, scrollback/history, an agent picker, and code/markdown rendering — without
the echo-filter / dedup / chunking workarounds the bridge needs.

## Architecture
```
 Flutter app (phone, on Tailscale)
        │  HTTPS over Tailscale, x-secret-key
        ▼
 Orchestrator API  (MacBook :8000)          ← already exists
        │  queue (SQLite)
        ▼
 Worker (mac-mini)  →  agents/runner.py  →  claude -p / codex exec / agy -p
```
The app never touches the worker or the agent CLIs directly — it only enqueues
tasks and polls results, exactly like the WhatsApp bridge does.

## API contract (already implemented)
Auth: header `x-secret-key: <SECRET_KEY>` on every call.
- `POST /tasks` `{ "type": "agent_run", "payload": { "agent", "prompt",
  "_target_machine", "session_id"?, "resume"? }, "notes"? }` → `201 { id, ... }`
- `GET /tasks/{id}` → `{ id, status, result: { response | error, ok, model }, notes }`
  status ∈ `pending | claimed | in_progress | done | failed | needs_human`
- `GET /tasks?status=&limit=` → recent tasks (for a history/queue view)
- `GET /machines` → fleet roster + liveness (for a status view)

**Important:** submit **backend** agent names, not the WhatsApp aliases. Valid
backends (worker `agents/runner.py`): `claude, agy, codex, groq, content, social`.
(`code`/`gpt` are WhatsApp-only aliases handled by the bridge — not the orchestrator.)

## Multi-turn
Same mechanism as the bridge: only `claude` is resumable. The app generates a UUID
`session_id` on the first message of a claude conversation and sends `resume:false`;
subsequent messages send the same id with `resume:true`. Switching agent or tapping
"New session" clears it. Other agents are one-shot.

## Artifacts (later)
The agent names a created file's path in its reply. The app can't read the Mac Mini
filesystem, so to download it needs a token endpoint like the bridge's
`GET /artifact/{token}`. MVP: render the path as text. Enhancement: add an artifact
token endpoint to the orchestrator (or reuse the bridge's) and make paths tappable.

## Streaming (later)
The queue is poll-based; MVP polls `GET /tasks/{id}` every ~2 s. For live
token-by-token output, add an SSE endpoint on the orchestrator
(`GET /tasks/{id}/stream`) that tails the worker's incremental output; the queue
stays the source of truth.

## Security
- Tailscale-only: orchestrator `:8000` is reachable on the tailnet, not public.
- `x-secret-key` shared secret (already used fleet-wide). Stored in the app's
  settings (consider Keychain/secure storage for a real build).

## Tech
Flutter (cross-platform iOS + Android + desktop), reusing existing Flutter skills
(budgetapp). Deps kept minimal: `http`, `shared_preferences`.

## Screens (MVP)
1. **Chat** — agent dropdown, message list (mono, selectable), input, send; polls
   for the result; multi-turn for claude; "New session" + settings actions.
2. **Settings** — orchestrator URL (default `http://100.97.176.37:8000`),
   secret key, target machine (default `mac-mini`).

## Milestones (see scrum Sprint-11 / Epic E22)
1. Scaffold + API client + settings (BLI-052) — *delivered as initial scaffold*
2. Chat screen + multi-turn (BLI-053) — *delivered as initial scaffold*
3. Fleet status + task history views (BLI-054)
4. Artifacts (token endpoint) + streaming SSE (BLI-055, stretch)
