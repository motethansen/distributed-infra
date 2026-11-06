#!/usr/bin/env bash
# Scan for secrets before they reach GitHub. This repo is PUBLIC.
#
#   scripts/check-secrets.sh            scan the working tree and the staged changes
#   scripts/check-secrets.sh --history  scan every commit (slow on big repos, run before going public / after a rewrite)
#
# Needs gitleaks:  brew install gitleaks   |  https://github.com/gitleaks/gitleaks/releases
# Rules and allowlist: .gitleaks.toml. Findings are redacted in the output.
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

command -v gitleaks >/dev/null || { echo "gitleaks not found (brew install gitleaks)" >&2; exit 2; }

if [[ "${1:-}" == "--history" ]]; then
  gitleaks detect --source . --config .gitleaks.toml --baseline-path .gitleaks-baseline.json --redact --no-banner
else
  # Only files git would commit (tracked + new, not ignored): never .env, .venv or data/.
  tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
  git ls-files -z --cached --others --exclude-standard | while IFS= read -r -d '' f; do
    [[ -f "$f" ]] && printf '%s\0' "$f"
  done | tar --null -T - -cf - 2>/dev/null | tar -xf - -C "$tmp"
  gitleaks detect --source "$tmp" --no-git --config "$PWD/.gitleaks.toml" --redact --no-banner
fi
echo "check-secrets: clean"
