# Disk janitor (mac-mini and thinkpad-x1)

`scripts/disk_janitor.py` keeps the two worker machines ready for builds. One script, stdlib only
(runs on the mac-mini's system Python 3.9 and on the thinkpad), three jobs:

| Command | What it does | Deletes? |
|---|---|---|
| `monitor` | Disk numbers and a status: `up`, `degraded` (85% used or under 50 GB free) or `critical` (92% or under 20 GB). On the thinkpad `/tmp` is RAM, so only its percentage counts. | No |
| `clean` | Removes a short list of idle, self-regenerating files (below). `--dry-run` shows what it would do. | Yes, only that list |
| `recommend` | Writes `~/.local/state/disk-janitor/recommendations.md`: what to clean up or uninstall, each with its size, a `safe` / `review` / `keep` label and the exact command. | Never |
| `run` | The scheduled tick: monitor, clean, push one status to Vantage, refresh the recommendations once a day. | Same as `clean` |

## What `clean` removes on its own

Only things that rebuild themselves and have not been touched for a while:

- `/tmp` scratch: python `tmpXXXX` dirs and `kotlin-daemon` logs idle 2 hours, `node-compile-cache` idle 3 days,
  `flutter_tools.*` and old `pytest-of-<user>` runs (newest 3 kept) only while no test run is active.
- GitHub runner `_diag` logs older than 14 days and `_work/_temp` older than 3 days. Never the checked-out work directories.
- pip cache files older than 30 days, npm `_logs` older than 14 days, macOS `DiagnosticReports` older than 30 days.
- Linux Trash items trashed more than 30 days ago (by the `.trashinfo` deletion date). The macOS Trash is never emptied automatically.
- Docker: dangling images and build cache unused for 7 days. Never tagged images, containers or volumes.

Every deletion goes through a safety check: inside its rule's root, not a protected directory, owned by this user,
not a mount point, and a symlink is unlinked, never followed. Everything it removes is written to
`~/.local/state/disk-janitor/janitor.log`.

## What `recommend` looks at

Git worktrees (clean and merged, including squash-merged, are `safe`; uncommitted work is `keep`), developer caches
(Gradle, Xcode DerivedData, pub, npm, Playwright, Homebrew), big application caches, application data folders,
`node_modules` / `.venv` / `build` in projects untouched for 60 days, files over 500 MB not modified for 30 days,
old `/tmp` scratch, unused applications (last-use date, falling back to the app's preference files; Xcode, OrbStack,
Docker and Android Studio are never suggested), and on Linux the sudo-only items (journal, disabled snap revisions,
unused apt packages, apt cache).

## Install

See the header of `com.techstartups.disk-janitor.plist.example` (mac-mini, launchd) and
`disk-janitor.service.example` + `disk-janitor.timer.example` (thinkpad, systemd user timer). Try it first:

    python3 scripts/disk_janitor.py clean --dry-run
    python3 scripts/disk_janitor.py recommend

Vantage components: `mm-disk` and `tp-disk` (set with `JANITOR_COMPONENT`).

## Why it replaces `tmp-cleanup.sh`

The thinkpad's shell version died halfway whenever cron ran it: cron does not set `$USER`, and the script ran with
`set -u`, so it stopped at the first line that used it. It removed temp directories, then never reached the test-artefact
step or the summary line. Run by hand it worked, which is why it looked fine. The Python version does not read `$USER`,
and a test runs the real script with only the environment cron provides.
