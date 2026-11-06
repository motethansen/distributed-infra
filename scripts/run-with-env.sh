#!/usr/bin/env bash
# Load a .env file into the environment, then exec a command. Keeps secrets OUT of launchd
# plists and systemd units, so those files are safe to commit and share.
#
#   scripts/run-with-env.sh [--env-file PATH] -- COMMAND [ARGS...]
#
# * Default env file: <repo>/.env  (or $ENV_FILE if set).
# * A variable that is already set in the environment wins over the file, so a plist,
#   systemd `Environment=` line, CI secret or shell export can override any key.
# * The file is parsed, never sourced: only KEY=VALUE lines are read, so nothing in it can
#   run as code. Surrounding quotes are stripped; `#` comment lines and blank lines are skipped.
# * Refuses to run if the file is readable by group/others (it holds secrets): chmod 600 .env
# * Values are never printed.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$REPO_ROOT/.env}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file) ENV_FILE="${2:?--env-file needs a path}"; shift 2 ;;
    --) shift; break ;;
    *) break ;;
  esac
done
[[ $# -gt 0 ]] || { echo "usage: run-with-env.sh [--env-file PATH] -- COMMAND [ARGS...]" >&2; exit 2; }

if [[ -f "$ENV_FILE" ]]; then
  # stat differs between macOS (-f %Lp) and Linux (-c %a)
  mode="$(stat -f '%Lp' "$ENV_FILE" 2>/dev/null || stat -c '%a' "$ENV_FILE")"
  if [[ "${mode: -2}" != "00" ]]; then
    echo "run-with-env: $ENV_FILE is mode $mode; it holds secrets. Run: chmod 600 $ENV_FILE" >&2
    exit 1
  fi
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line#"${line%%[![:space:]]*}"}"                 # trim leading space
    [[ -z "$line" || "$line" == \#* ]] && continue
    line="${line#export }"                                  # tolerate `export KEY=value`
    [[ "$line" == *=* ]] || continue
    key="${line%%=*}"; val="${line#*=}"
    key="${key%"${key##*[![:space:]]}"}"                    # trim trailing space on key
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    val="${val#"${val%%[![:space:]]*}"}"; val="${val%"${val##*[![:space:]]}"}"
    if [[ "$val" == \"*\" && ${#val} -ge 2 ]]; then val="${val:1:${#val}-2}"
    elif [[ "$val" == \'*\' && ${#val} -ge 2 ]]; then val="${val:1:${#val}-2}"
    else val="${val%%[[:space:]]#*}"; val="${val%"${val##*[![:space:]]}"}"; fi   # strip trailing ` # comment`
    [[ -z "${!key+x}" ]] && export "$key=$val"
  done < "$ENV_FILE"
else
  echo "run-with-env: no env file at $ENV_FILE; using the process environment only" >&2
fi

exec "$@"
