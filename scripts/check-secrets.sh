#!/usr/bin/env bash
# Scan for secrets before they reach GitHub. This repo is PUBLIC.
#
#   scripts/check-secrets.sh             scan what would be COMMITTED: the staged (index) content of every tracked file.
#                                        This is what the pre-commit hook runs; an unstaged edit cannot hide a staged secret.
#   scripts/check-secrets.sh --worktree  scan the working-tree files git would commit (tracked + new, not ignored), staged or not
#   scripts/check-secrets.sh --history   scan the history reachable from the checked-out commit (HEAD), like the CI's weekly scan.
#                                        Other branches and remote refs are NOT scanned (a bare `gitleaks detect` scans every ref).
#   scripts/check-secrets.sh --range A..B  scan only the commits in A..B, e.g. origin/main..HEAD: what CI checks for a pull request
#
# Needs gitleaks:  brew install gitleaks   |  https://github.com/gitleaks/gitleaks/releases
# Rules and allowlist: .gitleaks.toml (the staged copy when scanning the index). Findings are redacted in the output.
# New files that are not staged yet are not scanned in the default mode: they are not part of the commit.
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

command -v gitleaks >/dev/null || { echo "gitleaks not found (brew install gitleaks)" >&2; exit 2; }

tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
case "${1:-}" in
  --history)
    gitleaks detect --source . --config .gitleaks.toml --redact --no-banner --log-opts "--full-history HEAD" ;;
  --range)
    [[ -n "${2:-}" ]] || { echo "usage: check-secrets.sh --range A..B" >&2; exit 2; }
    gitleaks detect --source . --config .gitleaks.toml --redact --no-banner --log-opts "$2" ;;
  --worktree)
    # Only files git would commit (tracked + new, not ignored): never .env, .venv or data/.
    git ls-files -z --cached --others --exclude-standard | while IFS= read -r -d '' f; do
      if [[ -f "$f" ]]; then printf '%s\0' "$f"; fi
    done | tar --null -T - -cf - 2>/dev/null | tar -xf - -C "$tmp"
    gitleaks detect --source "$tmp" --no-git --config "$PWD/.gitleaks.toml" --redact --no-banner ;;
  "")
    # The index, not the working tree: `git checkout-index` writes the staged blobs, so what is scanned is exactly what a
    # commit would contain, whatever the working files look like. No stash, nothing in the repo is touched.
    git checkout-index -a --prefix="$tmp/"
    cfg="$tmp/.gitleaks.toml"; [[ -f "$cfg" ]] || cfg="$PWD/.gitleaks.toml"
    gitleaks detect --source "$tmp" --no-git --config "$cfg" --redact --no-banner ;;
  *) echo "usage: check-secrets.sh [--worktree | --history | --range A..B]" >&2; exit 2 ;;
esac
echo "check-secrets: clean"
