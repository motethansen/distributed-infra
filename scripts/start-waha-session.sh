#!/usr/bin/env bash
# Start (and verify) the WAHA WhatsApp session after the container boots.
#
# WHY THIS EXISTS
#   WAHA *Core* does NOT auto-start saved sessions when the container restarts
#   (WHATSAPP_RESTART_ALL_SESSIONS is a WAHA *Plus*-only feature). The session's
#   auth credentials ARE persisted on disk (services/whatsapp-bridge/waha-sessions/),
#   so no QR re-scan is needed — but the session comes back STOPPED until something
#   calls POST /api/sessions/<name>/start. This script does exactly that.
#
#   It is idempotent: if the session is already WORKING it exits 0 without doing
#   anything, so it is safe to run on boot and/or periodically (see the launchd
#   agent in scripts/com.techstartups.waha-session.plist.example).
#
# CONFIG (environment variables, with defaults)
#   WAHA_URL        WAHA base URL                         (default http://localhost:3000)
#   WAHA_SESSION    session name to start                 (default default)
#   WAHA_API_KEY    API key; if unset it is read from the running container
#   WAHA_CONTAINER  container to read the API key from     (default whatsapp-bridge-waha-1)
#   WAHA_WAIT_SECS  max seconds to wait for WAHA to come up (default 180)
#
# EXIT CODES
#   0  session is WORKING
#   1  WAHA never became reachable, or session did not reach WORKING
#   2  session needs re-linking (SCAN_QR_CODE) — stored credentials were lost
set -euo pipefail

WAHA_URL="${WAHA_URL:-http://localhost:3000}"
WAHA_SESSION="${WAHA_SESSION:-default}"
WAHA_CONTAINER="${WAHA_CONTAINER:-whatsapp-bridge-waha-1}"
WAHA_WAIT_SECS="${WAHA_WAIT_SECS:-180}"

# Resolve the API key: prefer the env var, else read it from the running
# container so the key never has to be hard-coded into this script or the plist.
resolve_key() {
  [ -n "${WAHA_API_KEY:-}" ] && return 0
  command -v docker >/dev/null 2>&1 || return 1
  local k
  k="$(docker inspect "$WAHA_CONTAINER" \
        --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
        | sed -n 's/^WAHA_API_KEY=//p' | head -n1)"
  [ -n "$k" ] && WAHA_API_KEY="$k"
}

api() {  # api <METHOD> <PATH>
  curl -s -X "$1" -H "X-Api-Key: ${WAHA_API_KEY:-}" "$WAHA_URL$2"
}

session_status() {
  api GET "/api/sessions/$WAHA_SESSION" | sed -nE 's/.*"status":"([^"]*)".*/\1/p'
}

# 1) Wait for the WAHA API key to be readable AND the API to answer 200.
#    (On a fresh boot Docker itself may not be up yet, so we retry both.)
echo "⏳ Waiting for WAHA at $WAHA_URL (up to ${WAHA_WAIT_SECS}s)…"
deadline=$(( $(date +%s) + WAHA_WAIT_SECS ))
while true; do
  if resolve_key; then
    code="$(curl -s -o /dev/null -w '%{http_code}' \
              -H "X-Api-Key: ${WAHA_API_KEY:-}" "$WAHA_URL/api/version" || true)"
    [ "$code" = "200" ] && break
  fi
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "✗ WAHA not reachable within ${WAHA_WAIT_SECS}s (Docker not up, or wrong/empty API key)." >&2
    exit 1
  fi
  sleep 3
done
echo "✓ WAHA is up."

# 2) Start the session only if it is not already WORKING.
st="$(session_status)"
if [ "$st" = "WORKING" ]; then
  echo "✓ Session '$WAHA_SESSION' already WORKING — nothing to do."
  exit 0
fi

echo "▶ Session '$WAHA_SESSION' is ${st:-unknown} — starting…"
api POST "/api/sessions/$WAHA_SESSION/start" >/dev/null || true

# 3) Wait for it to reach WORKING (creds restore from disk, no QR needed).
for _ in $(seq 1 30); do
  st="$(session_status)"
  case "$st" in
    WORKING)
      echo "✓ Session '$WAHA_SESSION' is WORKING."
      exit 0 ;;
    SCAN_QR_CODE)
      echo "✗ Session '$WAHA_SESSION' needs re-linking (SCAN_QR_CODE): stored credentials were lost." >&2
      echo "  Open the dashboard and scan the QR, or use a pairing code." >&2
      exit 2 ;;
  esac
  sleep 2
done

echo "✗ Session '$WAHA_SESSION' did not reach WORKING (last status: ${st:-unknown})." >&2
exit 1
