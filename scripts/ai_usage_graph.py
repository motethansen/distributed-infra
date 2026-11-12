#!/usr/bin/env python3
"""AI-usage knowledge graph + Wasabi transcript archive (stdlib only, Python 3.9+).

Reads the session transcripts of Claude Code (~/.claude/projects) and Codex
(~/.codex/sessions), and builds a small property graph in SQLite:

    nodes(id, type, label, props)      edges(src, dst, rel, props)

    machine  project  session  model  tool  file  repo  lesson

The graph holds metadata only (ids, counts, titles, paths, tokens, cost), no
transcript text beyond a redacted 160-character first prompt, so it is small
(a few MB) and every machine can carry the merged copy.  The transcripts
themselves are archived, redacted and gzipped, one object per session, to a
PRIVATE Wasabi prefix; local copies are only pruned after the remote object
has been verified and is older than the retention window.

    ai_usage_graph.py extract                 # scan transcripts -> graph.db
    ai_usage_graph.py notes                   # add memory-note ("lesson") nodes
    ai_usage_graph.py report [--days N]       # usage by project / model / day
    ai_usage_graph.py query "SELECT ..."      # raw SQL on the graph
    ai_usage_graph.py archive [--go]          # redact + upload (dry run by default)
    ai_usage_graph.py prune --older-than 30 [--go]
    ai_usage_graph.py push | pull             # share graph.db shards via Wasabi
    ai_usage_graph.py merge                   # union pulled shards into graph.db
    ai_usage_graph.py nightly                 # all of the above in order + Vantage report
    ai_usage_graph.py scan-secrets            # count redaction hits, upload nothing

Environment: AI_ARCHIVE_REMOTE (default wasabi:vizneo-backups/ai-archive),
AI_HOST, AI_GRAPH_DB.
"""
import argparse
import glob
import gzip
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict

HOME = os.path.expanduser("~")
def _host():
    h = socket.gethostname().split(".")[0].lower()
    for key, name in (("macbook", "macbook-pro"), ("mini", "mac-mini"), ("thinkpad", "thinkpad"), ("x1", "thinkpad")):
        if key in h:
            return name
    return h


HOST = os.environ.get("AI_HOST") or _host()
STATE = os.path.join(HOME, ".local", "state", "ai-usage")
DB_PATH = os.environ.get("AI_GRAPH_DB", os.path.join(STATE, "graph.db"))
REMOTE = os.environ.get("AI_ARCHIVE_REMOTE", "wasabi:vizneo-backups/ai-archive").rstrip("/")
CLAUDE_DIR = os.path.join(HOME, ".claude", "projects")
CODEX_DIR = os.path.join(HOME, ".codex", "sessions")
MEMORY_GLOB = os.path.join(CLAUDE_DIR, "*", "memory", "*.md")

# ----------------------------------------------------------------- redaction
# Replacement text contains no quote or backslash, so a redacted JSONL line is
# still valid JSON.
SECRET_PATTERNS = [
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|(?=\\n\\n)|$)", re.S)),
    ("anthropic", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai", re.compile(r"sk-(?:proj-)?[A-Za-z0-9_\-]{32,}")),
    ("stripe", re.compile(r"\b(?:sk|rk|pk|whsec)_(?:live|test)?_?[A-Za-z0-9]{16,}")),
    ("aws-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}")),
    ("slack", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("google", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("digitalocean", re.compile(r"\b(?:dop|doo|dor|dof)_v1_[a-f0-9]{40,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/\-]{20,}=*")),
    ("url-password", re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^\s:/@\"\\]+):[^\s@\"\\/]{3,}@")),
    ("assigned-secret", re.compile(
        r"(?i)\b([A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY|CREDENTIAL)[A-Z0-9_]*)"
        r"(\s*[=:]\s*\\?[\"']?)([^\s\"'\\,;]{8,})")),
]


def _looks_secret(v):
    """True for token-like values; false for identifiers, paths, placeholders and code."""
    if len(v) < 12 or v.startswith(("[REDACTED", "$", "<", "~", "/", ".", "{", "(", "http")):
        return False
    if any(c in v for c in "()/\\{}[]<>$ ") or v.count(".") > 1:
        return False
    return bool(re.search(r"[A-Za-z]", v) and re.search(r"\d", v)) or len(v) >= 24


def redact(text, counter=None):
    """Return text with secrets replaced by [REDACTED:kind]."""
    for kind, rx in SECRET_PATTERNS:
        def sub(m, kind=kind):
            if kind == "assigned-secret" and not _looks_secret(m.group(3)):
                return m.group(0)
            if counter is not None:
                counter[kind] += 1
            if kind == "url-password":
                return m.group(1) + ":[REDACTED:url-password]@"
            if kind == "assigned-secret":
                return m.group(1) + m.group(2) + "[REDACTED:secret]"
            return "[REDACTED:%s]" % kind
        text = rx.sub(sub, text)
    return text


# --------------------------------------------------------------------- graph
SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes(id TEXT PRIMARY KEY, type TEXT NOT NULL, label TEXT, props TEXT);
CREATE TABLE IF NOT EXISTS edges(src TEXT NOT NULL, dst TEXT NOT NULL, rel TEXT NOT NULL, props TEXT,
                                 PRIMARY KEY(src, dst, rel));
CREATE TABLE IF NOT EXISTS archive(session TEXT PRIMARY KEY, host TEXT, tool TEXT, project TEXT,
                                   remote TEXT, raw_bytes INTEGER, gz_bytes INTEGER, sha256 TEXT,
                                   verified_at REAL, src_paths TEXT);
CREATE INDEX IF NOT EXISTS edges_dst ON edges(dst, rel);
CREATE INDEX IF NOT EXISTS nodes_type ON nodes(type);
"""


def db_open(path=None):
    path = path or DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    return con


def put_node(con, nid, ntype, label=None, **props):
    con.execute("INSERT INTO nodes(id,type,label,props) VALUES(?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET label=COALESCE(excluded.label,label), props=excluded.props",
                (nid, ntype, label, json.dumps(props, sort_keys=True)))


def put_edge(con, src, dst, rel, **props):
    con.execute("INSERT INTO edges(src,dst,rel,props) VALUES(?,?,?,?) "
                "ON CONFLICT(src,dst,rel) DO UPDATE SET props=excluded.props",
                (src, dst, rel, json.dumps(props, sort_keys=True)))


# ------------------------------------------------------------------ helpers
_STRIP_SUFFIX = re.compile(r"(-s\d+|-wt.*|-stories.*|-worktree.*)$", re.I)


def project_of(cwd):
    """Map a working directory to a stable project name."""
    if not cwd:
        return "unknown"
    p = cwd.rstrip("/")
    if p == HOME or p == "":
        return "home"
    parts = [x for x in p.replace(HOME, "").split("/") if x]
    if "Projects" in parts:
        parts = parts[parts.index("Projects") + 1:]
    parts = [x for x in parts if x.lower() not in ("github", "gitlab", "vizneo", "worktrees")]
    if not parts:
        return "projects"
    name = parts[0] if parts[0].lower() != "techstartups" or len(parts) == 1 else parts[1]
    name = name.lower().replace("_", "-")
    return _STRIP_SUFFIX.sub("", name) or name


SCRATCH_PROJECTS = {"tmp", "private", "var", "home", "projects", "unknown"}
TICKET_PROJECTS = {"at": "academictool", "cf": "cheersfriends", "wd": "winedragons", "ba": "budgetapp"}


def refine_project(proj, cwd, first_prompt):
    """Give scratch-directory sessions (fleet workers, probes, analysts) a real owner."""
    fp = (first_prompt or "").lstrip()
    if fp.startswith("You are an SRE analysing one home network"):
        return "netwatch"
    if proj == "wt" or "/wt/" in (cwd or ""):
        m = re.search(r"/wt/([a-z]+)\d", cwd or "")
        return TICKET_PROJECTS.get(m.group(1), "worktrees") if m else "worktrees"
    if proj in SCRATCH_PROJECTS or (cwd or "").startswith(("/tmp", "/private/tmp", "/private/var", "/var/folders")):
        if fp.startswith("# Task:"):
            return "distributed-infra"
        if "llmexp" in (cwd or ""):
            return "llm-experiment"
        if fp.startswith(("Reply with", "List every tool", "Answer in ONE line")):
            return "fleet-probes"
        return "scratch" if proj != "home" else "home"
    return proj


SERVICE_CLIS = {
    "doctl": "digitalocean", "wrangler": "cloudflare", "gh": "github", "restic": "restic",
    "rclone": "wasabi", "aws": "aws", "docker": "docker", "launchctl": "launchd", "stripe": "stripe",
    "psql": "postgres", "flutter": "flutter", "npm": "npm", "npx": "npm", "uv": "uv", "pip": "pip",
    "pip3": "pip", "brew": "homebrew", "systemctl": "systemd", "ssh": "ssh", "scp": "ssh",
    "rsync": "rsync", "git": "git", "pytest": "pytest", "curl": "http", "gradle": "gradle",
    "xcodebuild": "xcode", "pod": "cocoapods", "firebase": "firebase", "tailscale": "tailscale",
    "make": "make", "python3": "python", "python": "python", "node": "node", "bw": "bitwarden",
}
_CMD_SPLIT = re.compile(r"(?:^|&&|\|\||;|\||\n)\s*(?:sudo\s+|env\s+|[A-Z_]+=\S+\s+)*([A-Za-z0-9_./\-]+)")


def clis_in(command):
    out = Counter()
    for m in _CMD_SPLIT.finditer(command or ""):
        base = os.path.basename(m.group(1))
        if base in SERVICE_CLIS:
            out[SERVICE_CLIS[base]] += 1
    return out


def short_prompt(text, n=160):
    text = re.sub(r"\s+", " ", text or "").strip()
    return redact(text)[:n]


def rel_file(path, cwd):
    if not path:
        return None
    if cwd and path.startswith(cwd.rstrip("/") + "/"):
        return path[len(cwd.rstrip("/")) + 1:]
    return path.replace(HOME, "~")


def iso_day(ts):
    return (ts or "")[:10]


# ----------------------------------------------------------- Claude extractor
def read_jsonl(path):
    with open(path, "r", errors="replace") as fh:
        for line in fh:
            try:
                yield json.loads(line)
            except ValueError:
                continue


def claude_sessions():
    """Yield (session_id, [files]) - the main file plus any sub-agent files."""
    for slug in sorted(os.listdir(CLAUDE_DIR)) if os.path.isdir(CLAUDE_DIR) else []:
        d = os.path.join(CLAUDE_DIR, slug)
        if not os.path.isdir(d):
            continue
        for f in sorted(glob.glob(os.path.join(d, "*.jsonl"))):
            sid = os.path.basename(f)[:-6]
            subs = sorted(glob.glob(os.path.join(d, sid, "**", "*.jsonl"), recursive=True))
            yield sid, slug, [f] + subs


def parse_claude_session(sid, slug, files):
    s = {"id": sid, "tool": "claude", "title": None, "first_prompt": None, "cwd": None,
         "start": None, "end": None, "branch": None, "turns": 0, "cost": None,
         "tokens": Counter(), "models": Counter(), "tools": Counter(), "clis": Counter(),
         "files": Counter(), "prs": set(), "entrypoint": None, "bytes": 0, "paths": files,
         "model_cost": Counter()}
    seen_msg = {}
    cost_by_start = {}
    for f in files:
        s["bytes"] += os.path.getsize(f)
        for o in read_jsonl(f):
            t = o.get("type")
            ts = o.get("timestamp")
            if ts:
                s["start"] = min(s["start"], ts) if s["start"] else ts
                s["end"] = max(s["end"], ts) if s["end"] else ts
            if o.get("cwd") and not s["cwd"]:
                s["cwd"] = o["cwd"]
            if o.get("gitBranch") and not s["branch"]:
                s["branch"] = o["gitBranch"]
            if o.get("entrypoint") and not s["entrypoint"]:
                s["entrypoint"] = o["entrypoint"]
            if t == "ai-title" and o.get("aiTitle"):
                s["title"] = o["aiTitle"]
            elif t == "pr-link" and o.get("prRepository"):
                s["prs"].add((o["prRepository"], o.get("prNumber")))
            elif t == "cost-state":
                # cumulative within one process (startTime); a resumed session starts a new one
                cost_by_start[o.get("startTime")] = o
            elif t == "user" and not o.get("isSidechain") and not o.get("isMeta"):
                c = (o.get("message") or {}).get("content")
                txt = c if isinstance(c, str) else " ".join(
                    b.get("text", "") for b in (c or []) if isinstance(b, dict) and b.get("type") == "text")
                if txt.strip():
                    s["turns"] += 1
                    if not s["first_prompt"] and not txt.lstrip().startswith("<"):
                        s["first_prompt"] = short_prompt(txt)
            elif t == "assistant":
                msg = o.get("message") or {}
                mid = msg.get("id") or o.get("uuid")
                usage = msg.get("usage") or {}
                # streamed messages repeat under one id: keep the largest usage
                prev = seen_msg.get(mid)
                cur = (usage.get("output_tokens", 0) or 0)
                if prev is None or cur >= prev[0]:
                    seen_msg[mid] = (cur, msg.get("model"), usage)
                for b in msg.get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        name = b.get("name") or "?"
                        s["tools"][name] += 1
                        inp = b.get("input") or {}
                        if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                            fp = inp.get("file_path") or inp.get("notebook_path")
                            if fp:
                                s["files"][fp] += 1
                        elif name == "Bash":
                            s["clis"].update(clis_in(inp.get("command", "")))
    if cost_by_start:
        s["cost"] = sum(c.get("totalCostUSD") or 0 for c in cost_by_start.values())
        for c in cost_by_start.values():
            for m, mu in (c.get("modelUsage") or {}).items():
                m = re.sub(r"\[.*?\]$", "", m)
                s["model_cost"][m] += mu.get("costUSD") or 0
    for _, (_, model, u) in seen_msg.items():
        if not model or model == "<synthetic>":
            continue
        s["models"][model] += 1
        s["tokens"]["input"] += u.get("input_tokens", 0) or 0
        s["tokens"]["output"] += u.get("output_tokens", 0) or 0
        s["tokens"]["cache_read"] += u.get("cache_read_input_tokens", 0) or 0
        s["tokens"]["cache_write"] += u.get("cache_creation_input_tokens", 0) or 0
    return s


# ----------------------------------------------------------- Codex extractor
def codex_sessions():
    for f in sorted(glob.glob(os.path.join(CODEX_DIR, "*", "*", "*", "*.jsonl"))):
        m = re.search(r"rollout-.*?-([0-9a-f]{8}-[0-9a-f\-]{27})\.jsonl$", f)
        sid = m.group(1) if m else os.path.basename(f)[:-6]
        yield sid, "codex", [f]


def parse_codex_session(sid, slug, files):
    s = {"id": sid, "tool": "codex", "title": None, "first_prompt": None, "cwd": None,
         "start": None, "end": None, "branch": None, "turns": 0, "cost": None,
         "tokens": Counter(), "models": Counter(), "tools": Counter(), "clis": Counter(),
         "files": Counter(), "prs": set(), "entrypoint": None, "bytes": 0, "paths": files,
         "model_cost": Counter()}
    last_total = None
    for f in files:
        s["bytes"] += os.path.getsize(f)
        for o in read_jsonl(f):
            ts = o.get("timestamp")
            if ts:
                s["start"] = min(s["start"], ts) if s["start"] else ts
                s["end"] = max(s["end"], ts) if s["end"] else ts
            p = o.get("payload") or {}
            t = o.get("type")
            pt = p.get("type")
            if t == "session_meta" and p.get("cwd") and not s["cwd"]:
                s["cwd"] = p["cwd"]
            if t == "turn_context" and p.get("cwd") and not s["cwd"]:
                s["cwd"] = p["cwd"]
            if pt == "thread_settings_applied":
                model = (p.get("thread_settings") or {}).get("model")
                if model:
                    s["models"][model] += 1
            elif pt == "token_count":
                tot = ((p.get("info") or {}).get("total_token_usage")) or None
                if tot:
                    last_total = tot
            elif pt == "item_completed":
                item = p.get("item") or {}
                if item.get("type") == "UserMessage":
                    s["turns"] += 1
                    if not s["first_prompt"]:
                        txt = " ".join(c.get("text", "") for c in item.get("content") or []
                                       if isinstance(c, dict))
                        s["first_prompt"] = short_prompt(txt)
            elif t == "response_item" and pt in ("function_call", "custom_tool_call", "local_shell_call"):
                name = p.get("name") or pt
                s["tools"][name] += 1
                args = p.get("arguments") or p.get("input") or ""
                if isinstance(args, str):
                    s["clis"].update(clis_in(args.replace('\\"', '"')[:4000]))
    if last_total:
        s["tokens"]["input"] = last_total.get("input_tokens", 0) or 0
        s["tokens"]["output"] = last_total.get("output_tokens", 0) or 0
        s["tokens"]["cache_read"] = last_total.get("cached_input_tokens", 0) or 0
    return s


# ---------------------------------------------------------------- commands
def store_session(con, s):
    proj = refine_project(project_of(s["cwd"]), s["cwd"], s["first_prompt"])
    sid = "session:%s" % s["id"]
    models = [m for m, _ in s["models"].most_common()]
    put_node(con, sid, "session", s["title"] or s["first_prompt"] or s["id"][:8],
             tool=s["tool"], host=HOST, project=proj, start=s["start"], end=s["end"],
             day=iso_day(s["start"]), turns=s["turns"], cost_usd=s["cost"],
             tokens=dict(s["tokens"]), models=models, model_cost={k: round(v, 4) for k, v in s["model_cost"].items()}, branch=s["branch"],
             first_prompt=s["first_prompt"], entrypoint=s["entrypoint"],
             bytes=s["bytes"], tool_calls=sum(s["tools"].values()))
    put_node(con, "machine:%s" % HOST, "machine", HOST)
    put_node(con, "project:%s" % proj, "project", proj)
    put_edge(con, sid, "machine:%s" % HOST, "ran_on")
    put_edge(con, sid, "project:%s" % proj, "in_project")
    for model, n in s["models"].items():
        put_node(con, "model:%s" % model, "model", model)
        put_edge(con, sid, "model:%s" % model, "used_model", messages=n)
    for tool, n in s["tools"].items():
        put_node(con, "tool:%s" % tool, "tool", tool)
        put_edge(con, sid, "tool:%s" % tool, "called_tool", count=n)
    for svc, n in s["clis"].items():
        put_node(con, "service:%s" % svc, "service", svc)
        put_edge(con, sid, "service:%s" % svc, "touched_service", count=n)
    for fp, n in s["files"].most_common(200):
        rel = rel_file(fp, s["cwd"])
        fid = "file:%s:%s" % (proj, rel)
        put_node(con, fid, "file", rel, project=proj)
        put_edge(con, sid, fid, "edited_file", count=n)
        put_edge(con, fid, "project:%s" % proj, "in_project")
    for repo, num in s["prs"]:
        put_node(con, "repo:%s" % repo, "repo", repo)
        put_edge(con, sid, "repo:%s" % repo, "opened_pr", pr=num)
        put_edge(con, "repo:%s" % repo, "project:%s" % proj, "backs_project")


GRAPH_VERSION = 2   # bump to force one full re-extract (e.g. after a labelling change)


def cmd_extract(args):
    con = db_open()
    if con.execute("PRAGMA user_version").fetchone()[0] < GRAPH_VERSION:
        args.full = True
    started = time.time()
    n = 0
    # incremental: skip sessions whose recorded byte size has not changed
    known = {r[0]: json.loads(r[1] or "{}").get("bytes") for r in
             con.execute("SELECT id, props FROM nodes WHERE type='session'")}
    for gen, parse in ((claude_sessions, parse_claude_session), (codex_sessions, parse_codex_session)):
        for sid, slug, files in gen():
            size = sum(os.path.getsize(f) for f in files)
            if not args.full and known.get("session:%s" % sid) == size:
                continue
            store_session(con, parse(sid, slug, files))
            n += 1
            if n % 50 == 0:
                con.commit()
    con.execute("PRAGMA user_version = %d" % GRAPH_VERSION)
    con.commit()
    sess = con.execute("SELECT COUNT(*) FROM nodes WHERE type='session'").fetchone()[0]
    print("extracted %d new/changed sessions (%d total) in %.1fs -> %s" % (n, sess, time.time() - started, DB_PATH))


_FM = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.S)


def cmd_notes(args):
    con = db_open()
    projects = [r[0] for r in con.execute("SELECT label FROM nodes WHERE type='project'")]
    names = {}
    paths = glob.glob(MEMORY_GLOB)
    count = 0
    for p in paths:
        base = os.path.basename(p)
        if base == "MEMORY.md":
            continue
        text = open(p, errors="replace").read()
        m = _FM.match(text)
        if not m:
            continue
        fm = {}
        for line in m.group(1).splitlines():
            if ":" in line and not line.startswith(" "):
                k, v = line.split(":", 1)
                fm[k.strip()] = v.strip()
            elif line.strip().startswith("type:"):
                fm["type"] = line.split(":", 1)[1].strip()
        name = fm.get("name") or base[:-3]
        names[name] = p
        nid = "lesson:%s" % name
        put_node(con, nid, "lesson", fm.get("description", name)[:200], kind=fm.get("type"), file=p.replace(HOME, "~"))
        body = m.group(2)
        low = (fm.get("description", "") + " " + name + " " + body[:600]).lower()
        for proj in projects:
            if len(proj) > 3 and proj.replace("-", "") in low.replace("-", "").replace("_", ""):
                put_edge(con, nid, "project:%s" % proj, "about_project")
        for link in set(re.findall(r"\[\[([^\]]+)\]\]", body)):
            put_edge(con, nid, "lesson:%s" % link, "related_to")
        count += 1
    con.commit()
    print("indexed %d memory notes as lesson nodes" % count)


def props(row):
    return json.loads(row or "{}")


def cmd_report(args):
    con = db_open()
    since = time.strftime("%Y-%m-%d", time.localtime(time.time() - args.days * 86400))
    rows = [(r[0], props(r[1])) for r in con.execute(
        "SELECT id, props FROM nodes WHERE type='session'")]
    rows = [(i, p) for i, p in rows if (p.get("day") or "") >= since]
    by_proj = defaultdict(lambda: Counter())
    by_model = defaultdict(lambda: Counter())
    by_host = defaultdict(lambda: Counter())
    by_day = defaultdict(lambda: Counter())
    for _, p in rows:
        tok = p.get("tokens") or {}
        out = tok.get("output", 0)
        for bucket, key in ((by_proj, p.get("project")), (by_host, p.get("host")),
                            (by_day, p.get("day")), ):
            c = bucket[key]
            c["sessions"] += 1
            c["output_tok"] += out
            c["cost"] += p.get("cost_usd") or 0
            c["turns"] += p.get("turns") or 0
        mc = p.get("model_cost") or {}
        for m in (p.get("models") or ["?"]):
            c = by_model[m]
            c["sessions"] += 1
            c["cost"] += mc.get(m, 0)
        for m in mc:
            if m not in (p.get("models") or []):
                by_model[m]["cost"] += mc[m]
                by_model[m]["sessions"] += 1

    def table(title, data, top=15, key=lambda kv: -kv[1]["sessions"]):
        print("\n%s" % title)
        print("  %-34s %8s %8s %12s %9s" % ("", "sessions", "turns", "out tokens", "cost USD"))
        for k, c in sorted(data.items(), key=key)[:top]:
            print("  %-34s %8d %8d %12d %9.2f" % (str(k)[:34], c["sessions"], c["turns"], c["output_tok"], c["cost"]))

    print("AI usage since %s: %d sessions" % (since, len(rows)))
    table("By project", by_proj)
    table("By model (output tokens are per session, see by project)", by_model, key=lambda kv: -kv[1]["cost"])
    table("By machine", by_host)
    table("Busiest days", by_day, top=8)
    print("\nMost-edited files")
    for r in con.execute("""SELECT n.label, n.props, COUNT(*) c, SUM(json_extract(e.props,'$.count')) k
                            FROM edges e JOIN nodes n ON n.id=e.dst
                            WHERE e.rel='edited_file' GROUP BY e.dst ORDER BY k DESC LIMIT 10"""):
        print("  %5s edits in %2d sessions  %s [%s]" % (r[3], r[2], r[0], props(r[1]).get("project")))
    print("\nServices touched")
    for r in con.execute("""SELECT dst, COUNT(*), SUM(json_extract(props,'$.count')) FROM edges
                            WHERE rel='touched_service' GROUP BY dst ORDER BY 3 DESC LIMIT 10"""):
        print("  %-24s %4d sessions %6d calls" % (r[0].split(":", 1)[1], r[1], r[2]))


def cmd_query(args):
    con = db_open()
    cur = con.execute(args.sql)
    cols = [d[0] for d in cur.description or []]
    print("\t".join(cols))
    for row in cur.fetchall():
        print("\t".join("" if v is None else str(v) for v in row))


# ----------------------------------------------------------------- archive
_RCLONE = None
_RCLONE_ENV = None


def _rclone_setup():
    """Find rclone and, when this host has no `wasabi:` remote, build one from the
    backup toolkit's wasabi.env (the same key restic uses), so no key is copied."""
    global _RCLONE, _RCLONE_ENV
    if _RCLONE:
        return
    _RCLONE = shutil.which("rclone") or next(
        (c for c in (os.path.join(HOME, ".local/bin/rclone"), "/opt/homebrew/bin/rclone", "/usr/local/bin/rclone")
         if os.path.exists(c)), "rclone")
    env = dict(os.environ)
    have = subprocess.run([_RCLONE, "listremotes"], capture_output=True, text=True, env=env).stdout
    envfile = os.path.join(HOME, ".config", "mh-backup", "wasabi.env")
    if "wasabi:" not in have and os.path.exists(envfile):
        for line in open(envfile):
            m = re.match(r"\s*(?:export\s+)?(AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY)=(.*)", line)
            if m:
                env[m.group(1)] = m.group(2).strip().strip("\"'")
        env.update({"RCLONE_CONFIG_WASABI_TYPE": "s3", "RCLONE_CONFIG_WASABI_PROVIDER": "Wasabi",
                    "RCLONE_CONFIG_WASABI_ENDPOINT": "s3.ap-southeast-1.wasabisys.com",
                    "RCLONE_CONFIG_WASABI_REGION": "ap-southeast-1",
                    "RCLONE_CONFIG_WASABI_ACCESS_KEY_ID": env.get("AWS_ACCESS_KEY_ID", ""),
                    "RCLONE_CONFIG_WASABI_SECRET_ACCESS_KEY": env.get("AWS_SECRET_ACCESS_KEY", "")})
    _RCLONE_ENV = env


def rclone(*a, check=True):
    _rclone_setup()
    return subprocess.run([_RCLONE] + list(a), capture_output=True, text=True, check=check, env=_RCLONE_ENV)


def build_archive_object(paths, dest_gz, counter):
    """Redact every line of the session's files into one gzip stream; return (raw, gz, sha)."""
    h = hashlib.sha256()
    raw = 0
    with gzip.GzipFile(dest_gz, "wb", compresslevel=6, mtime=0) as gz:
        for p in paths:
            gz.write(('{"_archive_source":"%s"}\n' % p.replace(HOME, "~").replace('"', "")).encode())
            with open(p, "r", errors="replace") as fh:
                for line in fh:
                    raw += len(line)
                    out = redact(line, counter).encode()
                    gz.write(out)
                    h.update(out)
    return raw, os.path.getsize(dest_gz), h.hexdigest()


def archive_candidates(con):
    for gen, parse_tool in ((claude_sessions, "claude"), (codex_sessions, "codex")):
        for sid, slug, files in gen():
            yield parse_tool, sid, slug, files


def project_for(con, sid):
    r = con.execute("SELECT props FROM nodes WHERE id=?", ("session:%s" % sid,)).fetchone()
    return props(r[0]).get("project", "unknown") if r else "unknown"


def cmd_scan_secrets(args):
    c = Counter()
    n = 0
    for tool, sid, slug, files in archive_candidates(None):
        for p in files:
            with open(p, errors="replace") as fh:
                for line in fh:
                    redact(line, c)
        n += 1
    print("scanned %d sessions; redaction hits:" % n)
    for k, v in c.most_common():
        print("  %-16s %d" % (k, v))


def cmd_archive(args):
    con = db_open()
    done = {r[0]: r[1] for r in con.execute("SELECT session, raw_bytes FROM archive WHERE verified_at IS NOT NULL")}
    todo = []
    for tool, sid, slug, files in archive_candidates(con):
        size = sum(os.path.getsize(f) for f in files)
        if done.get(sid) == size:
            continue
        # skip sessions still being written (touched in the last 10 minutes)
        if max(os.path.getmtime(f) for f in files) > time.time() - 600:
            continue
        todo.append((tool, sid, slug, files, size))
    total = sum(t[4] for t in todo)
    print("%d sessions to archive (%.0f MB raw) -> %s/%s/" % (len(todo), total / 1e6, REMOTE, HOST))
    if not args.go:
        print("dry run; pass --go to redact, upload and verify")
        return
    stage = tempfile.mkdtemp(prefix="ai-archive-")
    try:
        hits = Counter()
        meta = {}
        for tool, sid, slug, files, size in todo:
            proj = project_for(con, sid)
            rel = "%s/%s/%s/%s.jsonl.gz" % (HOST, tool, proj, sid)
            dest = os.path.join(stage, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            raw, gz, sha = build_archive_object(files, dest, hits)
            meta[rel] = (tool, sid, proj, size, gz, sha, files)
        # PRIVATE objects: the rclone remote defaults to authenticated-read
        r = rclone("copy", stage, REMOTE, "--s3-acl", "private", "--transfers", "8",
                   "--s3-no-check-bucket", check=False)
        if r.returncode:
            sys.exit("upload failed: " + r.stderr[-800:])
        v = rclone("check", stage, REMOTE, "--one-way", "--download", check=False) if args.paranoid \
            else rclone("check", stage, REMOTE, "--one-way", "--size-only", check=False)
        if v.returncode:
            sys.exit("verification failed, nothing recorded: " + v.stderr[-800:])
        now = time.time()
        for rel, (tool, sid, proj, size, gz, sha, files) in meta.items():
            con.execute("INSERT OR REPLACE INTO archive VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (sid, HOST, tool, proj, "%s/%s" % (REMOTE, rel), size, gz, sha, now,
                         json.dumps([f for f in files])))
        con.commit()
        print("archived and verified %d sessions; redactions: %s" % (len(meta), dict(hits)))
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def cmd_prune(args):
    con = db_open()
    cutoff = time.time() - args.older_than * 86400
    freed = 0
    victims = []
    for sid, remote, paths_json, vat in con.execute(
            "SELECT session, remote, src_paths, verified_at FROM archive WHERE host=? AND verified_at IS NOT NULL", (HOST,)):
        paths = [p for p in json.loads(paths_json) if os.path.exists(p)]
        if not paths or max(os.path.getmtime(p) for p in paths) > cutoff:
            continue
        victims.append((sid, remote, paths))
    print("%d archived sessions older than %d days still on disk" % (len(victims), args.older_than))
    if not args.go:
        for sid, _, paths in victims[:10]:
            print("  would delete", sid, "(%d file(s))" % len(paths))
        print("dry run; pass --go to delete (each remote object is re-checked first)")
        return
    for sid, remote, paths in victims:
        ls = rclone("lsjson", remote, check=False)
        try:
            ok = ls.returncode == 0 and json.loads(ls.stdout)[0]["Size"] > 0
        except (ValueError, IndexError, KeyError):
            ok = False
        if not ok:
            print("  SKIP %s: remote object not confirmed" % sid)
            continue
        for p in paths:
            freed += os.path.getsize(p)
            os.remove(p)
        d = os.path.dirname(paths[0])
        sub = os.path.join(d, sid)
        if os.path.isdir(sub):
            shutil.rmtree(sub, ignore_errors=True)
    print("deleted local copies, freed %.0f MB" % (freed / 1e6))


# ------------------------------------------------------ share graph shards
def cmd_push(args):
    con = db_open()
    con.commit()
    shard = os.path.join(tempfile.mkdtemp(prefix="ai-graph-"), "graph-%s.db" % HOST)
    out = sqlite3.connect(shard)
    con.backup(out)
    out.close()
    r = rclone("copyto", shard, "%s/graphs/graph-%s.db" % (REMOTE, HOST), "--s3-acl", "private", "--s3-no-check-bucket", check=False)
    print("pushed shard" if r.returncode == 0 else "push failed: " + r.stderr[-400:])


def cmd_pull(args):
    d = os.path.join(STATE, "shards")
    os.makedirs(d, exist_ok=True)
    r = rclone("copy", "%s/graphs" % REMOTE, d, "--s3-no-check-bucket", check=False)
    print("pulled shards:", sorted(os.listdir(d)) if r.returncode == 0 else r.stderr[-400:])


def cmd_merge(args):
    con = db_open()
    n = 0
    for shard in sorted(glob.glob(os.path.join(STATE, "shards", "graph-*.db"))):
        if shard.endswith("graph-%s.db" % HOST):
            continue
        con.execute("ATTACH DATABASE ? AS s", (shard,))
        con.execute("INSERT OR REPLACE INTO nodes SELECT * FROM s.nodes")
        con.execute("INSERT OR REPLACE INTO edges SELECT * FROM s.edges")
        con.execute("INSERT OR REPLACE INTO archive SELECT * FROM s.archive")
        con.commit()
        con.execute("DETACH DATABASE s")
        n += 1
    print("merged %d shard(s)" % n)


def push_vantage(component, status, errors, checks, details=None):
    url = os.environ.get("VANTAGE_URL", "")
    if not url:
        return False
    import urllib.request
    payload = {"component": component, "status": status, "errors": errors[:20], "checks": checks,
               "ts": int(time.time())}
    if details:
        payload["details"] = details
    headers = {"Content-Type": "application/json"}
    if os.environ.get("VANTAGE_INGEST_KEY"):
        headers["x-vantage-key"] = os.environ["VANTAGE_INGEST_KEY"]
    req = urllib.request.Request(url.rstrip("/") + "/api/ingest", data=json.dumps(payload).encode(),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def cmd_nightly(args):
    """extract -> notes -> archive -> push graph -> pull+merge peers -> prune; report to Vantage."""
    import fcntl
    os.makedirs(STATE, exist_ok=True)
    lock = open(os.path.join(STATE, "lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another run is in progress")
        return
    logp = os.path.join(STATE, "nightly.log")
    if os.path.exists(logp) and os.path.getsize(logp) > 2_000_000:
        os.replace(logp, logp + ".1")
    errors, steps = [], {}
    ns = argparse.Namespace
    plan = [("extract", cmd_extract, ns(full=False)), ("notes", cmd_notes, ns()),
            ("archive", cmd_archive, ns(go=True, paranoid=False)), ("push", cmd_push, ns()),
            ("pull", cmd_pull, ns()), ("merge", cmd_merge, ns()),
            ("prune", cmd_prune, ns(older_than=args.retention_days, go=True))]
    with open(logp, "a") as log:
        log.write("\n== %s nightly on %s\n" % (time.strftime("%F %T"), HOST))
        for name, fn, a in plan:
            t0 = time.time()
            try:
                fn(a)
                steps[name] = "ok"
            except SystemExit as e:
                steps[name] = "failed"
                errors.append("%s: %s" % (name, e))
            except Exception as e:  # keep going: a failed upload must not stop the graph refresh
                steps[name] = "failed"
                errors.append("%s: %r" % (name, e))
            log.write("%-8s %-6s %.1fs\n" % (name, steps[name], time.time() - t0))
            if steps[name] != "ok":
                log.write("   %s\n" % errors[-1][:500])
    con = db_open()
    sess = con.execute("SELECT COUNT(*) FROM nodes WHERE type='session'").fetchone()[0]
    arch = con.execute("SELECT COUNT(*), COALESCE(SUM(gz_bytes),0) FROM archive WHERE host=?", (HOST,)).fetchone()
    status = "up" if not errors else ("critical" if steps.get("archive") == "failed" else "degraded")
    component = os.environ.get("AI_COMPONENT", "ai-usage")
    ok = push_vantage(component, status, errors, {k: v == "ok" for k, v in steps.items()},
                      {"sessions": sess, "archived_sessions": arch[0], "archived_mb": round(arch[1] / 1e6, 1),
                       "host": HOST})
    print("nightly %s: %s; vantage=%s" % (status, steps, ok))
    if errors:
        sys.exit(1)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    e = sp.add_parser("extract"); e.add_argument("--full", action="store_true"); e.set_defaults(fn=cmd_extract)
    sp.add_parser("notes").set_defaults(fn=cmd_notes)
    r = sp.add_parser("report"); r.add_argument("--days", type=int, default=90); r.set_defaults(fn=cmd_report)
    q = sp.add_parser("query"); q.add_argument("sql"); q.set_defaults(fn=cmd_query)
    sp.add_parser("scan-secrets").set_defaults(fn=cmd_scan_secrets)
    a = sp.add_parser("archive"); a.add_argument("--go", action="store_true")
    a.add_argument("--paranoid", action="store_true", help="verify by downloading and comparing content")
    a.set_defaults(fn=cmd_archive)
    p = sp.add_parser("prune"); p.add_argument("--older-than", type=int, default=30)
    p.add_argument("--go", action="store_true"); p.set_defaults(fn=cmd_prune)
    sp.add_parser("push").set_defaults(fn=cmd_push)
    sp.add_parser("pull").set_defaults(fn=cmd_pull)
    sp.add_parser("merge").set_defaults(fn=cmd_merge)
    n = sp.add_parser("nightly"); n.add_argument("--retention-days", type=int, default=30)
    n.set_defaults(fn=cmd_nightly)
    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
