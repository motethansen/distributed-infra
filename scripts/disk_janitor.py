#!/usr/bin/env python3
"""Disk janitor for the fleet's worker machines (mac-mini, thinkpad-x1).

One script, three jobs, run every 30 minutes by launchd (mac) or a systemd user timer (linux):

  monitor    How full is the disk? Reports one status to Vantage (up / degraded / critical).
  clean      Deletes a SHORT list of things that regenerate on their own and are provably idle:
             stale /tmp scratch, test-run leftovers, old runner temp files and logs, old pip/npm
             logs, dangling docker images and old build cache. Nothing else, ever.
  recommend  Lists what a human should look at: big caches, worktrees that are merged and
             clean, dormant node_modules/.venv/build dirs, huge old files, unused applications,
             and the sudo-only commands (journal, old snaps, apt). It NEVER deletes any of it.

Usage:
  disk_janitor.py [run]           monitor + clean, and recommend once a day  (the scheduled tick)
  disk_janitor.py monitor         just the disk numbers
  disk_janitor.py heartbeat       monitor + push to Vantage only (run every minute so it never goes stale)
  disk_janitor.py clean [--dry-run]
  disk_janitor.py recommend       fresh scan, prints the Markdown report, writes the files
  add --json for machine-readable output, --dry-run to delete nothing, --verbose to list every item.

Settings (environment; nothing secret lives in this file):
  JANITOR_STATE_DIR      default ~/.local/state/disk-janitor (log, report.json, recommendations.md)
  JANITOR_COMPONENT      Vantage component id, default "<hostname>-disk"
  JANITOR_WARN_PCT / JANITOR_CRIT_PCT          default 85 / 92   (percent used)
  JANITOR_WARN_FREE_GB / JANITOR_CRIT_FREE_GB  default 50 / 20   (GB free)
  VANTAGE_URL, VANTAGE_INGEST_KEY              reporting is skipped if VANTAGE_URL is unset

Python 3.9 compatible (the mac-mini's system python is 3.9). Standard library only.
"""
from __future__ import annotations

import calendar
import fcntl
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

GB = 1024 ** 3
MB = 1024 ** 2
DAY = 86400
RANK = {"up": 0, "degraded": 1, "critical": 2}

IS_MAC = platform.system() == "Darwin"
HOME = os.path.expanduser("~")

# Nothing under these is ever deleted by the cleaner, whatever a rule says.
NEVER_TOUCH = ("/", "/tmp", "/private/tmp", "/home", "/Users", "/System", "/usr", "/etc", "/var", "/opt",
               "/bin", "/sbin", "/lib", "/boot", "/root", HOME)

TEST_RUNNER_NAMES = {"flutter_tester", "pytest", "py.test", "vitest", "jest"}


# --------------------------------------------------------------------------- helpers

def run(cmd: list[str], timeout: float) -> tuple[int, str]:
    """Run a command with a hard timeout. Returns (rc, stdout); rc 124 = timed out, 127 = missing."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip()
    except subprocess.TimeoutExpired:
        return 124, ""
    except OSError:
        return 127, ""


def fmt(n: Optional[float]) -> str:
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def parse_size(text: str) -> int:
    """'1.2GB' / '340 MB' / '5.5kB' -> bytes (docker prints decimal units)."""
    m = re.match(r"\s*([\d.]+)\s*([kKMGT]?)B?", text)
    if not m:
        return 0
    mult = {"": 1, "k": 1000, "K": 1000, "M": 1000 ** 2, "G": 1000 ** 3, "T": 1000 ** 4}[m.group(2)]
    return int(float(m.group(1)) * mult)


def du_bytes(path: str, timeout: float = 60) -> Optional[int]:
    """Disk usage of a path via the native du (much faster than walking in Python). None = unknown."""
    try:
        p = subprocess.run(["du", "-skx", path], capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None
    line = p.stdout.strip().splitlines()[-1:] or [""]
    try:
        return int(line[0].split()[0]) * 1024
    except (ValueError, IndexError):
        return None


def walk_bytes(path: str) -> int:
    """Allocated bytes under path without following symlinks (used to measure what we delete)."""
    total = 0
    stack = [path]
    while stack:
        cur = stack.pop()
        try:
            st = os.lstat(cur)
        except OSError:
            continue
        total += getattr(st, "st_blocks", 0) * 512 or st.st_size
        if os.path.isdir(cur) and not os.path.islink(cur):
            try:
                with os.scandir(cur) as it:
                    stack.extend(e.path for e in it)
            except OSError:
                pass
    return total


def touched_since(path: str, cutoff: float) -> bool:
    """True if path or anything inside it was modified after `cutoff` (stops at the first hit)."""
    stack = [path]
    while stack:
        cur = stack.pop()
        try:
            st = os.lstat(cur)
        except OSError:
            continue
        if st.st_mtime > cutoff:
            return True
        if os.path.isdir(cur) and not os.path.islink(cur):
            try:
                with os.scandir(cur) as it:
                    stack.extend(e.path for e in it)
            except OSError:
                return True      # cannot look inside: assume it is in use
    return False


def newest_mtime(path: str) -> float:
    """Newest modification time anywhere under path (0 if unreadable)."""
    best = 0.0
    stack = [path]
    while stack:
        cur = stack.pop()
        try:
            st = os.lstat(cur)
        except OSError:
            continue
        best = max(best, st.st_mtime)
        if os.path.isdir(cur) and not os.path.islink(cur):
            try:
                with os.scandir(cur) as it:
                    stack.extend(e.path for e in it)
            except OSError:
                pass
    return best


def safe_to_delete(path: str, root: str) -> bool:
    """Defence in depth: whatever a rule produced, refuse anything outside its root, the root
    itself, a protected directory, a mount point, or something owned by someone else."""
    name = os.path.basename(path.rstrip("/"))
    if name in ("", ".", ".."):
        return False
    try:
        real_root = os.path.realpath(root)
        parent = os.path.realpath(os.path.dirname(path))
        st = os.lstat(path)
    except OSError:
        return False
    if parent != real_root and not parent.startswith(real_root + os.sep):
        return False                                  # not inside its root (e.g. a symlinked parent)
    if os.path.join(parent, name) in NEVER_TOUCH:
        return False
    if st.st_uid != os.getuid() or os.path.ismount(path):
        return False
    return True


def remove_path(path: str) -> None:
    if os.path.islink(path) or not os.path.isdir(path):
        os.unlink(path)
    else:
        shutil.rmtree(path)


def tests_running(ps_lines: Optional[list[str]] = None) -> bool:
    """Is a test run active for this user? Matches the program being run, not any command line
    that merely contains the word (an ssh session or an agent prompt must not block cleanup)."""
    if ps_lines is None:
        rc, out = run(["ps", "-U", str(os.getuid()), "-o", "args="], 15)
        ps_lines = out.splitlines() if rc == 0 else []
    me = str(os.getpid())
    for line in ps_lines:
        toks = line.split()
        if not toks or me in toks[:1]:
            continue
        names = {os.path.basename(t) for t in toks[:4]}
        if names & TEST_RUNNER_NAMES:
            return True
        if "pytest" in toks[:4] or ("-m" in toks[:4] and "pytest" in toks[:6]):
            return True
        if any("flutter_tools.snapshot" in t for t in toks[:4]) and "test" in toks[1:8]:
            return True
    return False


# --------------------------------------------------------------------------- monitor

@dataclass
class Mount:
    path: str
    label: str
    total: int
    used: int
    free: int
    ram: bool = False

    @property
    def pct(self) -> float:
        denom = self.used + self.free
        return 100.0 * self.used / denom if denom else 0.0


def env_num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def thresholds() -> dict:
    return {"warn_pct": env_num("JANITOR_WARN_PCT", 85), "crit_pct": env_num("JANITOR_CRIT_PCT", 92),
            "warn_free": env_num("JANITOR_WARN_FREE_GB", 50) * GB, "crit_free": env_num("JANITOR_CRIT_FREE_GB", 20) * GB}


def measure(path: str, label: str, ram: bool = False) -> Optional[Mount]:
    try:
        v = os.statvfs(path)
    except OSError:
        return None
    total = v.f_blocks * v.f_frsize
    free = v.f_bavail * v.f_frsize
    return Mount(path, label, total, total - v.f_bfree * v.f_frsize, free, ram)


def watched_mounts() -> list[Mount]:
    out: list[Mount] = []
    if IS_MAC:
        m = measure("/System/Volumes/Data" if os.path.isdir("/System/Volumes/Data") else "/", "data")
        if m:
            out.append(m)
    else:
        m = measure("/", "root")
        if m:
            out.append(m)
        try:
            if os.stat("/tmp").st_dev != os.stat("/").st_dev:
                t = measure("/tmp", "tmp", ram=True)       # tmpfs lives in RAM: only the percentage matters
                if t:
                    out.append(t)
        except OSError:
            pass
    return out


def evaluate(m: Mount, th: dict) -> tuple[str, Optional[str]]:
    pct, free = m.pct, m.free
    low_free = (not m.ram) and free < th["warn_free"]
    crit = pct >= th["crit_pct"] or ((not m.ram) and free < th["crit_free"])
    warn = pct >= th["warn_pct"] or low_free
    status = "critical" if crit else ("degraded" if warn else "up")
    msg = None
    if status != "up":
        msg = f"{m.label} {pct:.0f}% used, {fmt(free)} free"
    return status, msg


def monitor() -> dict:
    th = thresholds()
    mounts, status, errors = [], "up", []
    for m in watched_mounts():
        st, msg = evaluate(m, th)
        if RANK[st] > RANK[status]:
            status = st
        if msg:
            errors.append(msg)
        mounts.append({"path": m.path, "label": m.label, "total": m.total, "used": m.used,
                       "free": m.free, "pct": round(m.pct, 1), "status": st})
    if not mounts:
        status, errors = "degraded", ["could not read any watched filesystem"]
    return {"status": status, "mounts": mounts, "errors": errors}


# --------------------------------------------------------------------------- clean

@dataclass
class Candidate:
    rule: str
    path: str
    root: str
    note: str = ""


@dataclass
class CleanResult:
    removed: list = field(default_factory=list)       # [{rule, path, bytes}]
    skipped: list = field(default_factory=list)       # [str]
    errors: list = field(default_factory=list)        # [str]
    freed: int = 0


def tmp_candidates(now: float, running: bool, tmp_roots: Iterable[str] = ("/tmp",),
                   min_idle_s: float = 120 * 60) -> list[Candidate]:
    """Stale scratch in /tmp. Same rules as the thinkpad's original tmp-cleanup.sh, minus its bugs."""
    out: list[Candidate] = []
    cutoff = now - min_idle_s
    uid = os.getuid()
    for root in tmp_roots:
        if not os.path.isdir(root):
            continue
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue

        def owned(p: str) -> bool:
            try:
                return os.lstat(p).st_uid == uid
            except OSError:
                return False

        def matches(n: str, pats: tuple) -> bool:
            return any(re.fullmatch(p, n) for p in pats)

        for n in names:
            p = os.path.join(root, n)
            if not owned(p):
                continue
            isdir = os.path.isdir(p) and not os.path.islink(p)
            if isdir and matches(n, (r"\.?tmp.+",)):                      # python tempfile dirs
                if not touched_since(p, cutoff):
                    out.append(Candidate("tmp-tempdirs", p, root))
            elif (not running) and isdir and matches(n, (r"flutter_tools\..+",)):
                if not touched_since(p, cutoff):
                    out.append(Candidate("tmp-flutter", p, root))
            elif matches(n, (r"kotlin-daemon\..+\.log", r"kotlin-compiler-.+\.log")):
                if not touched_since(p, cutoff):
                    out.append(Candidate("tmp-kotlin-logs", p, root))
            elif isdir and n == "node-compile-cache":                     # node regenerates this
                if not touched_since(p, now - 3 * DAY):
                    out.append(Candidate("tmp-node-cache", p, root))
        if not running:
            for d in os.listdir(root):
                if re.fullmatch(r"pytest-of-.+", d):
                    base = os.path.join(root, d)
                    if not owned(base):
                        continue
                    runs = [os.path.join(base, x) for x in os.listdir(base) if x.startswith("pytest-")]
                    runs.sort(key=lambda x: os.lstat(x).st_mtime, reverse=True)
                    for r in runs[3:]:                                      # keep the 3 newest
                        if not touched_since(r, cutoff):
                            out.append(Candidate("tmp-pytest", r, base))
    return out


def age_candidates(rule: str, root: str, max_age_days: float, now: float,
                   suffixes: tuple = ()) -> list[Candidate]:
    """Top-level entries of `root` whose newest content is older than max_age_days."""
    out: list[Candidate] = []
    if not os.path.isdir(root):
        return out
    cutoff = now - max_age_days * DAY
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return out
    for n in names:
        if suffixes and not n.endswith(suffixes):
            continue
        p = os.path.join(root, n)
        if not touched_since(p, cutoff):
            out.append(Candidate(rule, p, root))
    return out


def runner_candidates(now: float, home: str = HOME) -> list[Candidate]:
    """GitHub Actions runners: their own diagnostics and per-job temp files, once old.
    Never the checked-out work directories (a running job lives there)."""
    out: list[Candidate] = []
    try:
        runners = sorted(d for d in os.listdir(home) if d.startswith("actions-runner"))
    except OSError:
        return out
    for r in runners:
        base = os.path.join(home, r)
        out += age_candidates("runner-diag-logs", os.path.join(base, "_diag"), 14, now)
        out += age_candidates("runner-temp", os.path.join(base, "_work", "_temp"), 3, now)
    return out


def trash_candidates(now: float, home: str = HOME, days: int = 30) -> list[Candidate]:
    """Freedesktop trash only: its .trashinfo records WHEN the item was trashed. (The macOS Trash does
    not, so an old file trashed a minute ago would look old: that one is a recommendation only.)"""
    if IS_MAC:
        return []
    base = os.path.join(home, ".local", "share", "Trash")
    out: list[Candidate] = []
    info_dir, files_dir = os.path.join(base, "info"), os.path.join(base, "files")
    if not os.path.isdir(info_dir):
        return out
    for n in sorted(os.listdir(info_dir)):
        if not n.endswith(".trashinfo"):
            continue
        try:
            with open(os.path.join(info_dir, n)) as f:
                m = re.search(r"^DeletionDate=(\S+)", f.read(), re.M)
            when = time.mktime(time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")) if m else None
        except (OSError, ValueError):
            when = None
        if when is not None and when < now - days * DAY:
            item = os.path.join(files_dir, n[: -len(".trashinfo")])
            if os.path.lexists(item):
                out.append(Candidate("trash-30d", item, files_dir))
    return out


def pip_npm_candidates(now: float, home: str = HOME) -> list[Candidate]:
    out: list[Candidate] = []
    pip = os.path.join(home, "Library", "Caches", "pip") if IS_MAC else os.path.join(home, ".cache", "pip")
    out += age_candidates("pip-cache-30d", pip, 30, now)
    out += age_candidates("npm-logs-14d", os.path.join(home, ".npm", "_logs"), 14, now)
    return out


def find_docker() -> Optional[str]:
    path = os.pathsep.join([os.environ.get("PATH", ""), "/usr/local/bin", "/opt/homebrew/bin",
                            os.path.join(HOME, ".orbstack", "bin"), "/usr/bin"])
    return shutil.which("docker", path=path)


def docker_clean(dry: bool, runner: Callable = run, docker: Optional[str] = None) -> tuple[list, list]:
    """Dangling images (untagged leftovers) and build cache unused for a week. Never touches a tagged
    image, a container or a volume. Skips quietly if the engine does not answer."""
    docker = docker or find_docker()
    if not docker:
        return [], []
    rc, out = runner([docker, "version", "--format", "{{.Server.Version}}"], 15)
    if rc != 0 or not out:
        return [], ["docker engine not answering: skipped image/build-cache cleanup"]
    removed, errors = [], []
    steps = [("docker-dangling-images", [docker, "image", "prune", "-f"]),
             ("docker-build-cache-7d", [docker, "builder", "prune", "-f", "--filter", "until=168h"])]
    for rule, cmd in steps:
        if dry:
            removed.append({"rule": rule, "path": " ".join(cmd[1:]), "bytes": 0, "dry": True})
            continue
        rc, out = runner(cmd, 300)
        if rc != 0:
            errors.append(f"{rule} failed (rc={rc})")
            continue
        m = re.search(r"Total reclaimed space:\s*(.+)", out)
        removed.append({"rule": rule, "path": " ".join(cmd[1:]), "bytes": parse_size(m.group(1)) if m else 0})
    return removed, errors


def collect_candidates(now: float, running: Optional[bool] = None) -> list[Candidate]:
    running = tests_running() if running is None else running
    cands = tmp_candidates(now, running, ["/tmp"])
    cands += runner_candidates(now)
    cands += pip_npm_candidates(now)
    cands += trash_candidates(now)
    if IS_MAC:
        cands += age_candidates("mac-diagnostic-reports-30d",
                                os.path.join(HOME, "Library", "Logs", "DiagnosticReports"), 30, now)
    return cands


def clean(dry: bool, now: Optional[float] = None, candidates: Optional[list[Candidate]] = None,
          with_docker: bool = True) -> CleanResult:
    now = time.time() if now is None else now
    res = CleanResult()
    cands = candidates if candidates is not None else collect_candidates(now)
    for c in cands:
        if not safe_to_delete(c.path, c.root):
            res.skipped.append(f"{c.path}: refused by safety check")
            continue
        size = walk_bytes(c.path)
        if dry:
            res.removed.append({"rule": c.rule, "path": c.path, "bytes": size, "dry": True})
            res.freed += size
            continue
        try:
            remove_path(c.path)
        except OSError as e:
            left = walk_bytes(c.path) if os.path.lexists(c.path) else 0
            size -= left
            res.errors.append(f"{c.rule}: {c.path}: {e.strerror or e}")
        res.removed.append({"rule": c.rule, "path": c.path, "bytes": size})
        res.freed += size
    if with_docker:
        removed, errors = docker_clean(dry)
        res.removed += removed
        res.errors += errors
        res.freed += sum(r["bytes"] for r in removed)
    return res


# --------------------------------------------------------------------------- recommend

@dataclass
class Rec:
    id: str
    title: str
    bytes: Optional[int]
    detail: str
    action: str
    sudo: bool = False
    confidence: str = "review"      # safe | review | keep


def dir_children_over(root: str, min_bytes: int, budget: float) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    if not os.path.isdir(root):
        return out
    end = time.time() + budget
    for n in sorted(os.listdir(root)):
        if time.time() > end:
            break
        p = os.path.join(root, n)
        b = du_bytes(p, timeout=max(5, min(60, end - time.time())))
        if b is not None and b >= min_bytes:
            out.append((p, b))
    out.sort(key=lambda x: -x[1])
    return out


# Known heavy developer directories: (path, what it is, advice, confidence).
# safe = rebuilds itself; review = real data or a download you chose; keep = may hold history you need.
HEAVY_DIRS = [
    (".gradle/caches", "Gradle caches", "Stop daemons first: `./gradlew --stop`, then `rm -rf ~/.gradle/caches/build-cache-*`; the rest re-downloads on the next build.", "safe"),
    (".android/avd", "Android emulator images", "Each emulator is a virtual phone with its own data. Delete the ones you no longer boot: `avdmanager delete avd -n <name>`.", "review"),
    (".pub-cache", "Dart/Flutter package cache", "`flutter pub cache clean` (re-downloads on the next `pub get`).", "safe"),
    (".cocoapods", "CocoaPods spec repos", "`pod cache clean --all`.", "safe"),
    (".npm/_cacache", "npm download cache", "`npm cache clean --force` (re-downloads).", "safe"),
    (".cache/ms-playwright", "Playwright browser builds", "Old versions pile up: remove all, then `npx playwright install` for the one you use.", "safe"),
    ("Library/Caches/ms-playwright", "Playwright browser builds", "Remove, then `npx playwright install`.", "safe"),
    (".cache/uv", "uv download cache", "`uv cache prune`.", "safe"),
    (".cache/tracker3", "GNOME file indexer database", "`tracker3 reset -s -r`; stop it growing again with `systemctl --user mask tracker-miner-fs-3.service`.", "safe"),
    (".lmstudio", "Local LLM models you downloaded", "Large downloads, not caches. Delete models you no longer use from inside the LM Studio app.", "review"),
    ("Library/Developer/Xcode/DerivedData", "Xcode build output", "`rm -rf ~/Library/Developer/Xcode/DerivedData` (rebuilt on the next build).", "safe"),
    ("Library/Developer/CoreSimulator", "iOS simulators", "`xcrun simctl delete unavailable` removes the ones for SDKs you no longer have.", "review"),
    ("Library/Developer/Xcode/Archives", "Xcode archives", "Keep the releases you may need to re-symbolicate; delete the rest in Xcode's Organizer.", "keep"),
    ("Library/Developer/Xcode/iOS DeviceSupport", "Per-device debug symbols", "Delete folders for iOS versions you no longer test on; Xcode recreates them when you plug that device in.", "safe"),
    ("Library/Android", "Android SDK", "Remove unused system images and NDKs in Android Studio's SDK Manager.", "review"),
    ("flutter/bin/cache", "Flutter engine cache", "Re-downloaded by `flutter precache`.", "safe"),
    (".Trash", "The Trash", "Empty it from Finder once you have looked.", "review"),
]


def rec_heavy_dirs(budget: float = 240) -> list[Rec]:
    out, end = [], time.time() + budget
    for rel, what, advice, conf in HEAVY_DIRS:
        p = os.path.join(HOME, rel)
        if not os.path.isdir(p) or time.time() > end:
            continue
        b = du_bytes(p, timeout=max(5, min(90, end - time.time())))
        if b is not None and b >= 1 * GB:
            out.append(Rec("heavy-dir", f"~/{rel}", b, what, advice, confidence=conf))
    return out


CACHE_COMMANDS = {
    "Homebrew": "`brew cleanup -s --prune=all` (or `rm -rf ~/Library/Caches/Homebrew`; downloads are re-fetched on the next install).",
    "Yarn": "`yarn cache clean`.",
    "pip": "`pip cache purge`.",
    "typescript": "`rm -rf ~/Library/Caches/typescript` (rebuilt by the editor).",
    "org.swift.swiftpm": "`rm -rf ~/Library/Caches/org.swift.swiftpm` (SwiftPM re-fetches).",
}


def rec_user_caches(budget: float = 240) -> list[Rec]:
    root = os.path.join(HOME, "Library", "Caches") if IS_MAC else os.path.join(HOME, ".cache")
    known = {os.path.basename(h[0]) for h in HEAVY_DIRS}
    out = []
    for p, b in dir_children_over(root, 500 * MB, budget):
        if os.path.basename(p) in known:
            continue
        action = CACHE_COMMANDS.get(os.path.basename(p)) or f"Quit the app, then `rm -rf '{p}'`."
        out.append(Rec("app-cache", p.replace(HOME, "~"), b, "application cache (regenerates)", action, confidence="safe"))
    return out


def rec_app_data(budget: float = 240) -> list[Rec]:
    """Big application data folders. These are NOT caches: they hold an app's real content (profiles,
    databases, downloaded models, VM disks), so they are only ever 'review'."""
    roots = ([os.path.join(HOME, "Library", "Group Containers"), os.path.join(HOME, "Library", "Application Support"),
              os.path.join(HOME, "Library", "Containers")] if IS_MAC else
             [os.path.join(HOME, ".config"), os.path.join(HOME, ".local", "share"), os.path.join(HOME, "snap")])
    known = {os.path.basename(h[0]) for h in HEAVY_DIRS}
    out = []
    for root in roots:
        for p, b in dir_children_over(root, 1 * GB, budget / len(roots)):
            if os.path.basename(p) in known:
                continue
            out.append(Rec("app-data", p.replace(HOME, "~"), b, "application data (not a cache)",
                           "Open the app and clear its storage, or uninstall the app if you no longer use it. Do not delete the folder by hand while the app is installed.",
                           confidence="review"))
    out.sort(key=lambda r: -(r.bytes or 0))
    return out


def git_out(path: str, *args: str, timeout: float = 30) -> tuple[int, str]:
    return run(["git", "-C", path, *args], timeout)


def classify_worktree(path: str) -> Optional[Rec]:
    if not os.path.isfile(os.path.join(path, ".git")):          # a linked worktree has a .git FILE
        return None
    rc, common = git_out(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if rc != 0 or not common:
        return None
    main_repo = os.path.dirname(common.rstrip("/"))
    size = du_bytes(path, 60)
    rc, dirty = git_out(path, "status", "--porcelain")
    rc2, _ = git_out(path, "merge-base", "--is-ancestor", "HEAD", "origin/main")
    squashed = False
    if rc2 != 0:
        # A squash- or rebase-merged PR leaves the branch's commits out of main's history, but its
        # CONTENT is in main: every file the branch changed is now identical in origin/main.
        rb, base = git_out(path, "merge-base", "HEAD", "origin/main")
        rf, files = git_out(path, "diff", "--name-only", base, "HEAD") if rb == 0 and base else (1, "")
        if rf == 0 and files:
            rd, _ = git_out(path, "diff", "--quiet", "origin/main", "HEAD", "--", *files.splitlines())
            squashed = rd == 0
    rc3, when = git_out(path, "log", "-1", "--format=%cs")
    last = when if rc3 == 0 else "unknown"
    cmd = f"git -C '{main_repo}' worktree remove '{path}'"
    short = path.replace(HOME, "~")
    if rc != 0:
        return Rec("worktree", short, size, "git status failed: leave it", "Inspect by hand.", confidence="keep")
    if dirty:
        return Rec("worktree", short, size, f"uncommitted changes; last commit {last}",
                   "Keep until committed or discarded.", confidence="keep")
    if rc2 == 0 or squashed:
        how = "fully merged into origin/main" if rc2 == 0 else "its changes are already in origin/main (squash or rebase merge)"
        return Rec("worktree", short, size, f"clean; {how}; last commit {last}", f"`{cmd}`", confidence="safe")
    return Rec("worktree", short, size, f"clean but has commits not in origin/main; last commit {last}",
               f"Merge or push first, then `{cmd}`.", confidence="review")


def rec_worktrees() -> list[Rec]:
    import glob
    pats = [os.path.join(HOME, "wt", "*"), os.path.join(HOME, "wt-*"), os.path.join(HOME, "worktrees", "*")]
    out = []
    for pat in pats:
        for p in sorted(glob.glob(pat)):
            if os.path.isdir(p):
                r = classify_worktree(p)
                if r:
                    out.append(r)
    out.sort(key=lambda r: -(r.bytes or 0))
    return out


# Snaps that are shared runtimes/themes, not applications you chose to install.
SNAP_RUNTIME = re.compile(r"^(core\d*|snapd|bare|gnome-.*|gtk-.*|mesa-.*|kf\d.*|kde-.*|qt\d.*|.*-core\d+|.*-runtime|.*-themes?|firmware-updater|snap-store|desktop-security-center|snapd-desktop-integration)$")

ARTEFACT_DIRS = ("node_modules", ".venv", "venv", ".dart_tool", "build", "target", ".gradle")


def rec_dormant_artefacts(root: Optional[str] = None, days: int = 60, min_bytes: int = 200 * MB,
                          budget: float = 180, now: Optional[float] = None) -> list[Rec]:
    """node_modules/.venv/build dirs in projects nobody has touched for `days`. All rebuildable."""
    root = root or os.path.join(HOME, "Projects")
    now = time.time() if now is None else now
    out, end = [], time.time() + budget
    if not os.path.isdir(root):
        return out
    rc, listing = run(["find", root, "-maxdepth", "5", "(", "-name", "node_modules", "-o", "-name", ".venv",
                       "-o", "-name", "venv", "-o", "-name", ".dart_tool", "-o", "-name", "target", "-o",
                       "-name", "build", ")", "-type", "d", "-prune", "-print"], 90)
    for p in listing.splitlines():
        if time.time() > end:
            break
        project = os.path.dirname(p)
        if os.path.basename(p) in ("build", "target") and not (
                os.path.exists(os.path.join(project, "pubspec.yaml")) or os.path.exists(os.path.join(project, "Cargo.toml"))
                or os.path.exists(os.path.join(project, "build.gradle")) or os.path.exists(os.path.join(project, "pom.xml"))):
            continue
        rc, ct = git_out(project, "log", "-1", "--format=%ct", timeout=10)
        last_used = float(ct) if rc == 0 and ct.isdigit() else 0.0
        try:                                         # recent edits in the project's top level count as use
            for n in os.listdir(project):
                if n not in ARTEFACT_DIRS:
                    last_used = max(last_used, os.lstat(os.path.join(project, n)).st_mtime)
        except OSError:
            pass
        last_used = last_used or os.lstat(p).st_mtime
        if last_used > now - days * DAY:
            continue
        b = du_bytes(p, 40)
        if b is not None and b >= min_bytes:
            age = int((now - last_used) / DAY)
            out.append(Rec("dormant-artefact", p.replace(HOME, "~"), b, f"project untouched for {age} days",
                           f"`rm -rf '{p}'` (rebuild with the project's install/build command).", confidence="safe"))
    out.sort(key=lambda r: -(r.bytes or 0))
    return out[:25]


def rec_big_old_files(min_bytes: int = 500 * MB, days: int = 30) -> list[Rec]:
    prune = ["-path", os.path.join(HOME, "Library"), "-o", "-path", os.path.join(HOME, ".cache"), "-o",
             "-path", os.path.join(HOME, ".Trash"), "-o", "-path", os.path.join(HOME, "Obsidian"), "-o",
             "-path", os.path.join(HOME, "snap"), "-o", "-path", os.path.join(HOME, "iCloud Drive (Archive)"),
             "-o", "-path", os.path.join(HOME, ".android"), "-o", "-path", os.path.join(HOME, ".lmstudio"), "-o",
             "-path", os.path.join(HOME, "Applications")]
    cmd = ["find", HOME, "-maxdepth", "4", "(", *prune, ")", "-prune", "-o", "-type", "f",
           "-size", f"+{min_bytes // MB}M", "-mtime", f"+{days}", "-print"]
    rc, out = run(cmd, 120)
    recs = []
    for p in out.splitlines():
        try:
            recs.append(Rec("big-old-file", p.replace(HOME, "~"), os.stat(p).st_size,
                            f"not modified for {int((time.time() - os.stat(p).st_mtime) / DAY)} days",
                            f"Look at it; if it is a log or a download you are done with: `rm '{p}'`.",
                            confidence="review"))
        except OSError:
            continue
    recs.sort(key=lambda r: -(r.bytes or 0))
    return recs[:15]


def rec_old_tmp(min_age_days: int = 7) -> list[Rec]:
    out, now = [], time.time()
    for n in sorted(os.listdir("/tmp")):
        p = os.path.join("/tmp", n)
        try:
            if os.lstat(p).st_uid != os.getuid():
                continue
        except OSError:
            continue
        if not touched_since(p, now - min_age_days * DAY):
            b = du_bytes(p, 20)
            if b and b >= 20 * MB:
                out.append(Rec("old-tmp", p, b, f"untouched {min_age_days}+ days",
                               f"If nothing uses it: `rm -rf '{p}'` (not auto-removed: a venv here would look idle but be in use).",
                               confidence="review"))
    return out


def rec_linux_system() -> list[Rec]:
    out: list[Rec] = []
    rc, js = run(["journalctl", "--disk-usage"], 20)
    m = re.search(r"take up ([\d.]+)([KMGT])", js)
    if m:
        b = int(float(m.group(1)) * {"K": 1024, "M": MB, "G": GB, "T": 1024 * GB}[m.group(2)])
        if b > 1 * GB:
            out.append(Rec("journal", "systemd journal", b, "logs kept by systemd",
                           "`sudo journalctl --vacuum-size=500M`", sudo=True, confidence="safe"))
    rc, snaps = run(["snap", "list", "--all"], 30)
    old = [l.split() for l in snaps.splitlines()[1:] if "disabled" in l]
    if old:
        total = 0
        for f in old:
            try:
                total += os.stat(f"/var/lib/snapd/snaps/{f[0]}_{f[2]}.snap").st_size
            except OSError:
                pass
        cmds = "; ".join(f"sudo snap remove {f[0]} --revision={f[2]}" for f in old[:60])
        out.append(Rec("old-snaps", f"{len(old)} disabled snap revisions", total or None,
                       "old versions snap keeps after every update", f"`{cmds}`", sudo=True, confidence="safe"))
    rc, apt = run(["apt-get", "-s", "autoremove"], 60)
    n = sum(1 for l in apt.splitlines() if l.startswith("Remv "))
    if n:
        out.append(Rec("apt-autoremove", f"{n} unused packages", None, "installed only as dependencies, no longer needed",
                       "`sudo apt autoremove --purge`", sudo=True, confidence="review"))
    b = du_bytes("/var/cache/apt", 20)
    if b and b > 500 * MB:
        out.append(Rec("apt-cache", "/var/cache/apt", b, "downloaded .deb files", "`sudo apt clean`", sudo=True, confidence="safe"))
    return out


# Build tooling the fleet needs on these machines: never suggested for removal.
KEEP_APPS = {"Xcode.app", "OrbStack.app", "Docker.app", "Docker Desktop.app", "Android Studio.app",
             "Simulator.app", "Terminal.app", "iTerm.app"}


def mac_app_usage(app: str, home: str = HOME) -> Optional[float]:
    """When was this app last opened? Spotlight's date if there is one (a headless mac-mini often has
    none), otherwise the newest of the app's own preference/state files, which the app rewrites as it runs."""
    rc, out = run(["mdls", "-name", "kMDItemLastUsedDate", "-raw", app], 10)
    if rc == 0 and out and not out.startswith("(null)"):
        try:
            return float(calendar.timegm(time.strptime(out[:19], "%Y-%m-%d %H:%M:%S")))   # mdls prints UTC
        except ValueError:
            pass
    rc, bundle = run(["plutil", "-extract", "CFBundleIdentifier", "raw", "-o", "-",
                      os.path.join(app, "Contents", "Info.plist")], 10)
    name = os.path.splitext(os.path.basename(app))[0]
    traces = [os.path.join(home, "Library", "Application Support", name)]
    if rc == 0 and bundle:
        traces += [os.path.join(home, "Library", "Preferences", bundle + ".plist"),
                   os.path.join(home, "Library", "Containers", bundle),
                   os.path.join(home, "Library", "Saved Application State", bundle + ".savedState")]
    seen = []
    for t in traces:
        try:
            seen.append(os.stat(t).st_mtime)
        except OSError:
            pass
    return max(seen) if seen else None


def rec_apps(min_bytes: int = 300 * MB, unused_days: int = 90) -> list[Rec]:
    """Applications worth removing: big and not opened for a long time."""
    out: list[Rec] = []
    now = time.time()
    if IS_MAC:
        import glob
        for pat in ("/Applications/*.app", os.path.join(HOME, "Applications", "*.app")):
            for app in sorted(glob.glob(pat)):
                if os.path.basename(app) in KEEP_APPS:
                    continue
                b = du_bytes(app, 20)
                if not b or b < min_bytes:
                    continue
                used = mac_app_usage(app)
                if used is None:
                    note, stale = "no sign it was ever opened here (no Spotlight date, no preferences)", True
                else:
                    days = int((now - used) / DAY)
                    note, stale = f"last sign of use {days} days ago", days >= unused_days
                if stale:
                    out.append(Rec("app", os.path.basename(app), b, note,
                                   f"Drag to the Trash (or `rm -rf '{app}'`); remove its folder in ~/Library/Application Support too.",
                                   confidence="review"))
    else:
        apps = os.path.join(HOME, "Applications")
        if os.path.isdir(apps):
            for n in sorted(os.listdir(apps)):
                p = os.path.join(apps, n)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                days = int((now - max(st.st_atime, st.st_mtime)) / DAY)
                if st.st_size >= min_bytes and days >= unused_days:
                    out.append(Rec("app", f"~/Applications/{n}", st.st_size, f"AppImage not touched for {days} days",
                                   f"`rm '{p}'`", confidence="review"))
        rc, snaps = run(["snap", "list"], 30)
        for line in snaps.splitlines()[1:]:
            f = line.split()
            if len(f) >= 3 and not SNAP_RUNTIME.match(f[0]):
                try:
                    b = os.stat(f"/var/lib/snapd/snaps/{f[0]}_{f[2]}.snap").st_size
                except OSError:
                    continue
                if b >= min_bytes:
                    out.append(Rec("app", f"snap: {f[0]}", b, "installed snap application",
                                   f"If you do not use it: `sudo snap remove {f[0]}`", sudo=True, confidence="review"))
        rc, dpkg = run(["dpkg-query", "-W", "-f", "${Installed-Size}\t${Package}\n"], 30)
        big = []
        for line in dpkg.splitlines():
            try:
                kb, name = line.split("\t")
                if int(kb) * 1024 >= 400 * MB:
                    big.append((int(kb) * 1024, name))
            except ValueError:
                continue
        for b, name in sorted(big, reverse=True)[:8]:
            out.append(Rec("app", f"deb: {name}", b, "large installed package (information)",
                           f"If unused: `sudo apt remove --purge {name}`", sudo=True, confidence="review"))
    out.sort(key=lambda r: -(r.bytes or 0))
    return out


def recommend() -> dict:
    sections = [
        ("Worktrees", rec_worktrees),
        ("Developer caches", rec_heavy_dirs),
        ("Application caches", rec_user_caches),
        ("Application data", rec_app_data),
        ("Dormant build artefacts", rec_dormant_artefacts),
        ("Big old files", rec_big_old_files),
        ("Old /tmp scratch", rec_old_tmp),
        ("Applications", rec_apps),
    ]
    if not IS_MAC:
        sections.append(("System (needs sudo)", rec_linux_system))
    result, errors = {}, []
    for title, fn in sections:
        try:
            result[title] = [r.__dict__ for r in fn()]
        except Exception as e:                     # one broken scanner must not hide the others
            result[title] = []
            errors.append(f"{title}: {type(e).__name__}: {e}")
    return {"sections": result, "errors": errors, "ts": int(time.time())}


def render_markdown(host: str, mon: dict, rec: dict, last_clean: Optional[dict]) -> str:
    lines = [f"# Disk report: {host}", ""]
    for m in mon["mounts"]:
        lines.append(f"- `{m['path']}` ({m['label']}): **{m['pct']}% used**, {fmt(m['free'])} free of {fmt(m['total'])} ({m['status']})")
    if last_clean:
        lines.append(f"- Last automatic cleanup freed {fmt(last_clean.get('freed', 0))} ({len(last_clean.get('removed', []))} items)")
    lines += ["", "Nothing below is deleted automatically. `safe` = regenerates on its own; `review` = check first; `keep` = has work in it.", ""]
    total_safe = 0
    for title, items in rec["sections"].items():
        if not items:
            continue
        lines += [f"## {title}", ""]
        for r in items:
            size = fmt(r["bytes"]) if r["bytes"] is not None else "size unknown"
            sudo = " (needs sudo)" if r["sudo"] else ""
            lines.append(f"- **{r['title']}** — {size} — _{r['confidence']}_: {r['detail']}.{sudo} {r['action']}")
            if r["confidence"] == "safe" and r["bytes"]:
                total_safe += r["bytes"]
        lines.append("")
    lines += [f"**Reclaimable if you act on every `safe` line: about {fmt(total_safe)}.**", ""]
    if rec["errors"]:
        lines += ["Scanner problems: " + "; ".join(rec["errors"]), ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------- state, log, report

def state_dir() -> str:
    d = os.environ.get("JANITOR_STATE_DIR") or os.path.join(HOME, ".local", "state", "disk-janitor")
    os.makedirs(d, exist_ok=True)
    return d


def write_json(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def log_line(msg: str, echo: bool = True) -> None:
    line = f"{time.strftime('%F %T')} {msg}"
    if echo:
        print(line, flush=True)
    path = os.path.join(state_dir(), "janitor.log")
    try:
        if os.path.exists(path) and os.path.getsize(path) > 1 * MB:
            os.replace(path, path + ".1")
        with open(path, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def push_vantage(url: str, key: str, component: str, status: str, errors: list[str], checks: dict) -> bool:
    body = json.dumps({"component": component, "status": status, "errors": errors[:20],
                       "checks": checks, "ts": int(time.time())}).encode()
    headers = {"Content-Type": "application/json"}
    if key:
        headers["x-vantage-key"] = key
    req = urllib.request.Request(url.rstrip("/") + "/api/ingest", data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return 200 <= r.status < 300
    except (urllib.error.URLError, OSError):
        return False


def host_name() -> str:
    return socket.gethostname().split(".")[0].lower()


def lock_or_exit():
    f = open(os.path.join(state_dir(), "lock"), "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit(0)                                  # another run is in progress
    return f


def tick(dry: bool, force_recommend: bool = False, with_recommend: bool = True) -> dict:
    """The scheduled run: monitor, clean, push to Vantage, and refresh the recommendations daily."""
    sd = state_dir()
    mon = monitor()
    cl = clean(dry)
    for r in cl.removed:
        log_line(f"{'[dry] would remove' if r.get('dry') else 'removed'} {r['rule']}: {r['path']} ({fmt(r['bytes'])})", echo=False)
    for e in cl.errors:
        log_line(f"error: {e}", echo=False)
    after = monitor()                                # re-measure: cleanup may have fixed the problem
    cleaned = {"freed": cl.freed, "removed": cl.removed, "errors": cl.errors, "skipped": cl.skipped, "dry": dry}

    report_path = os.path.join(sd, "report.json")
    prev = {}
    try:
        with open(report_path) as f:
            prev = json.load(f)
    except (OSError, ValueError):
        pass
    rec_ts = prev.get("recommendations", {}).get("ts", 0)
    recs = prev.get("recommendations")
    if with_recommend and (force_recommend or time.time() - rec_ts > DAY or not recs):
        os.nice(10)
        recs = recommend()
    report = {"host": host_name(), "ts": int(time.time()), "monitor": after, "cleanup": cleaned,
              "recommendations": recs}
    write_json(report_path, report)
    if recs:
        with open(os.path.join(sd, "recommendations.md"), "w") as f:
            f.write(render_markdown(host_name(), after, recs, cleaned))

    status, errors = after["status"], list(after["errors"])
    if cl.errors:
        errors.append(f"cleanup: {len(cl.errors)} problem(s), first: {cl.errors[0][:120]}")
        if status == "up":
            status = "degraded"
    if status != "up" and recs:
        safe = sum(r["bytes"] or 0 for items in recs["sections"].values() for r in items if r["confidence"] == "safe")
        if safe:
            errors.append(f"about {fmt(safe)} could be reclaimed: see {sd}/recommendations.md")
    log_line(f"{status} " + "; ".join(f"{m['label']} {m['pct']}% ({fmt(m['free'])} free)" for m in after["mounts"])
             + f"; freed {fmt(cl.freed)}{' (dry run)' if dry else ''}")
    url = os.environ.get("VANTAGE_URL", "")
    if url and not dry:
        push_vantage(url, os.environ.get("VANTAGE_INGEST_KEY", ""),
                     os.environ.get("JANITOR_COMPONENT", f"{host_name()}-disk"), status, errors,
                     {"disk": after["status"], "cleanup": "degraded" if cl.errors else "up"})
    return report


def heartbeat() -> dict:
    """Cheap once-a-minute report: disk numbers only, no cleaning and no scanning. Vantage treats a
    component as stale after 150 s, so the 30-minute tick alone would leave it 'unknown' most of the time.
    The last cleanup's problems (from report.json) are carried along so a failing cleanup stays visible."""
    mon = monitor()
    status, errors, cleanup = mon["status"], list(mon["errors"]), "up"
    try:
        with open(os.path.join(state_dir(), "report.json")) as f:
            last = json.load(f).get("cleanup", {})
        if last.get("errors"):
            cleanup = "degraded"
            errors.append(f"cleanup: {len(last['errors'])} problem(s), first: {last['errors'][0][:120]}")
            if status == "up":
                status = "degraded"
    except (OSError, ValueError):
        pass
    url = os.environ.get("VANTAGE_URL", "")
    if url:
        push_vantage(url, os.environ.get("VANTAGE_INGEST_KEY", ""),
                     os.environ.get("JANITOR_COMPONENT", f"{host_name()}-disk"), status, errors,
                     {"disk": mon["status"], "cleanup": cleanup})
    return {"status": status, "errors": errors, "mounts": mon["mounts"]}


def main(argv: Optional[list[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    dry = "--dry-run" in args
    as_json = "--json" in args
    cmds = [a for a in args if not a.startswith("--")]
    cmd = cmds[0] if cmds else "run"
    if cmd not in ("run", "monitor", "clean", "recommend", "heartbeat"):
        print(__doc__)
        return 2
    if cmd == "heartbeat":                           # no lock: it must still report while a long scan runs
        out = heartbeat()
        print(f"{time.strftime('%F %T')} heartbeat {out['status']}", flush=True)
        return 0
    lock = lock_or_exit()                            # noqa: F841 (held for the life of the process)
    if cmd == "monitor":
        out = monitor()
        print(json.dumps(out, indent=1) if as_json else
              "\n".join(f"{m['label']}: {m['pct']}% used, {fmt(m['free'])} free ({m['status']})" for m in out["mounts"]))
        return 0 if out["status"] == "up" else 1
    if cmd == "clean":
        res = clean(dry)
        if as_json:
            print(json.dumps(res.__dict__, indent=1))
        else:
            if "--verbose" in args:
                for r in res.removed:
                    print(f"{'would remove' if r.get('dry') else 'removed'} {r['rule']}: {r['path']} ({fmt(r['bytes'])})")
            else:
                by_rule: dict = {}
                for r in res.removed:
                    n, b = by_rule.get(r["rule"], (0, 0))
                    by_rule[r["rule"]] = (n + 1, b + r["bytes"])
                for rule, (n, b) in sorted(by_rule.items(), key=lambda kv: -kv[1][1]):
                    print(f"{'would remove' if dry else 'removed'} {rule}: {n} item(s), {fmt(b)}")
            for e in res.errors + res.skipped:
                print("problem:", e)
            print(f"{'would free' if dry else 'freed'} {fmt(res.freed)}")
        return 0
    if cmd == "recommend":
        os.nice(10)
        recs = recommend()
        md = render_markdown(host_name(), monitor(), recs, None)
        write_json(os.path.join(state_dir(), "recommendations.json"), recs)
        with open(os.path.join(state_dir(), "recommendations.md"), "w") as f:
            f.write(md)
        print(json.dumps(recs, indent=1) if as_json else md)
        return 0
    rep = tick(dry)
    return 0 if rep["monitor"]["status"] != "critical" else 1


if __name__ == "__main__":
    sys.exit(main())
