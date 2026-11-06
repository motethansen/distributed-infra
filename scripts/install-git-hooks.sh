#!/usr/bin/env bash
# Install a pre-commit hook that runs scripts/check-secrets.sh (which scans the STAGED content), so a secret cannot be committed by accident.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
hook="$(git -C "$ROOT" rev-parse --git-path hooks/pre-commit)"
case "$hook" in /*) ;; *) hook="$ROOT/$hook" ;; esac
mkdir -p "$(dirname "$hook")"
cat > "$hook" <<'HOOK'
#!/usr/bin/env bash
exec "$(git rev-parse --show-toplevel)/scripts/check-secrets.sh"
HOOK
chmod +x "$hook"
echo "installed pre-commit hook: $hook"
