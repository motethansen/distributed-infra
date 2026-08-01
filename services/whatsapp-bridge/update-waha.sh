#!/usr/bin/env bash
# Keep the WAHA image current, because a stale one is a silent outage waiting.
#
# WHY THIS EXISTS
# ---------------
# On 2026-07-28 WhatsApp went dark for three days. The container was `Up 2 weeks`
# and the session was `FAILED`; `/api/sessions/default/restart` could not fix it
# because the running image (2026.4.3, built 2026-05-07) was ~3 months old and
# WhatsApp had retired its web-protocol version server-side. The session simply
# could not log in any more. `docker compose pull waha` fixed it in under a
# minute — the image was pinned to `latest` but had never actually been pulled.
#
# Vantage now warns when the running version is >60 days old, but a warning only
# helps if someone reads it. This closes the loop.
#
# DESIGN
#   * Compares image IDs and recreates ONLY when the pull produced a new image.
#     A no-op week costs one registry check and touches nothing, so this can run
#     often without disturbing a working session.
#   * Recreating restarts the WhatsApp session (~50s to reach WORKING, measured
#     2026-07-31). That is why it runs weekly at a quiet hour rather than daily,
#     and why it waits for the session to come back and shouts if it does not.
#   * Session credentials live on a host bind mount (./waha-sessions), so
#     recreating is non-destructive: no QR re-scan.
#
# Run by launchd: com.mh.waha-update (Sunday 04:00). Manual: bash update-waha.sh
set -uo pipefail

# Docker Desktop's bin dir must be on PATH or `docker compose pull` dies with
# "docker-credential-osxkeychain: executable file not found" under launchd's
# minimal environment — the exact failure that wasted the first attempt at this.
export PATH="/Applications/Docker.app/Contents/Resources/bin:/usr/local/bin:/opt/homebrew/bin:$PATH"

COMPOSE_DIR="${WAHA_COMPOSE_DIR:-$HOME/Projects/distributed-infra/services/whatsapp-bridge}"
WAHA_URL="${WAHA_URL:-http://localhost:3000}"
WAHA_API_KEY="${WAHA_API_KEY:-}"
SESSION="${WAHA_SESSION:-default}"
WAIT_TRIES="${WAHA_WAIT_TRIES:-24}"      # 24 x 5s = 2 min for the session to return

log() { printf '%s [%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "${1:-INFO}" "${2:-}"; }

cd "$COMPOSE_DIR" 2>/dev/null || { log ERROR "compose dir not found: $COMPOSE_DIR"; exit 1; }
command -v docker >/dev/null || { log ERROR "docker not on PATH"; exit 1; }

before="$(docker image inspect devlikeapro/waha --format '{{.Id}}' 2>/dev/null || echo none)"
log INFO "checking for a newer WAHA image (current ${before:0:19})"

if ! docker compose pull waha >/dev/null 2>&1; then
    log ERROR "docker compose pull failed — leaving the running container alone"
    exit 1
fi

after="$(docker image inspect devlikeapro/waha --format '{{.Id}}' 2>/dev/null || echo none)"
if [ "$before" = "$after" ]; then
    log INFO "already current — nothing to do"
    exit 0
fi

log INFO "new image pulled (${after:0:19}) — recreating the container"
if ! docker compose up -d waha >/dev/null 2>&1; then
    log ERROR "recreate FAILED — WhatsApp may be down, check: docker compose ps"
    exit 1
fi

# A pull that leaves the session unable to start is worse than the stale image
# we began with, so verify rather than assume.
if [ -z "$WAHA_API_KEY" ]; then
    log WARN "no WAHA_API_KEY set — cannot verify the session came back"
    exit 0
fi
i=0
while [ "$i" -lt "$WAIT_TRIES" ]; do
    s=$(curl -s -m 10 -H "X-Api-Key: $WAHA_API_KEY" "$WAHA_URL/api/sessions/$SESSION" 2>/dev/null \
        | python3 -c 'import sys,json;print(json.load(sys.stdin).get("status",""))' 2>/dev/null)
    [ "$s" = "WORKING" ] && { log INFO "session WORKING again — update complete"; exit 0; }
    i=$((i + 1)); sleep 5
done
log ERROR "session did not reach WORKING within $((WAIT_TRIES * 5))s (last status: ${s:-unknown}) — check the bridge"
exit 1
