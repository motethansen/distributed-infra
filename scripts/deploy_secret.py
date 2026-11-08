#!/usr/bin/env python3
"""Put ONE secret into a project's .env.deploy on this machine and the two workers, without ever printing it (DI-071).

    pbpaste | python3 scripts/deploy_secret.py budgetapp CLOUDFLARE_API_TOKEN

The value is read from stdin (your clipboard), never from the command line, so it is not in the shell history or the process list.
Before anything is written, a Cloudflare token is checked with Cloudflare itself (active? does it reach the account's Pages projects?);
a token that fails is refused and nothing changes. The file is mode 600 on every machine. `--remove` deletes the line again.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DIRS = {"winedragons": "Vizneo/winedragons_asia", "cheersfriends": "Vizneo/cheersfriends_cc", "academictool": "academictool",
        "urbanlife": "Vizneo/urbanlife_works", "budgetapp": "Vizneo/budgetapp"}
BASE = "~/Projects/github"
REMOTE = r'''
import os, sys, json
d = json.load(sys.stdin)
path = os.path.expanduser(d["path"]); var, value = d["var"], d["value"]
lines = open(path).read().split("\n") if os.path.exists(path) else []
out, done = [], False
for l in lines:
    if l.startswith(var + "="):
        done = True
        if value is not None: out.append(var + "=" + value)
    else:
        out.append(l)
if not done and value is not None:
    if out and out[-1] == "": out.insert(len(out) - 1, var + "=" + value)
    else: out.append(var + "=" + value)
os.makedirs(os.path.dirname(path), exist_ok=True)
open(path, "w").write("\n".join(out)); os.chmod(path, 0o600)
print("ok")
'''


def worker_hosts() -> dict[str, str]:
    m = yaml.safe_load((ROOT / "config" / "machines.yaml").read_text()).get("machines", {})
    me = os.uname().nodename.lower()
    return {n: c["tailscale_ip"] for n, c in m.items() if c.get("tailscale_ip") and c.get("role") in ("worker", "orchestrator")
            and not (n.replace("-", "") in me.replace("-", "").replace(".local", "") or "macbook" in n)}


def get_var(path: Path, var: str) -> str:
    try:
        for line in path.read_text().splitlines():
            if line.startswith(var + "="):
                return line.split("=", 1)[1].strip().strip("'\"")
    except OSError:
        pass
    return ""


def cf(path: str, token: str):
    req = urllib.request.Request("https://api.cloudflare.com/client/v4" + path, headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except ValueError:
            return {"success": False, "errors": [{"message": f"HTTP {e.code}"}]}


def verify_cloudflare(token: str, account: str, pages_project: str | None) -> list[str]:
    """Problems with a Cloudflare token (empty list = fine)."""
    problems = []
    r = cf(f"/accounts/{account}/tokens/verify", token) if account else {"success": False}
    if not (r.get("success") and (r.get("result") or {}).get("status") == "active"):
        r = cf("/user/tokens/verify", token)
    if not (r.get("success") and (r.get("result") or {}).get("status") == "active"):
        return ["Cloudflare says this token is not active (wrong value, expired or revoked)"]
    if account:
        p = cf(f"/accounts/{account}/pages/projects", token)
        if not p.get("success"):
            problems.append("the token is active but cannot list Pages projects: it needs Account, Cloudflare Pages, Edit "
                            "(" + "; ".join(e.get("message", "?") for e in p.get("errors", [])[:1]) + ")")
        elif pages_project and pages_project not in {x.get("name") for x in p.get("result") or []}:
            problems.append(f"the token reaches the account's Pages projects, but there is no project named {pages_project!r} in it")
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("project", choices=sorted(DIRS))
    ap.add_argument("var", help="the variable, e.g. CLOUDFLARE_API_TOKEN")
    ap.add_argument("--remove", action="store_true", help="delete the line instead of setting it")
    ap.add_argument("--no-verify", action="store_true", help="do not check a Cloudflare token with Cloudflare first")
    ap.add_argument("--pages-project", default="budgetapp-web", help="with a budgetapp token: the Pages project it must reach")
    a = ap.parse_args(argv)
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,60}", a.var):
        sys.exit("the variable name must be UPPER_SNAKE_CASE")
    rel = f"{DIRS[a.project]}/.env.deploy"
    local = Path(BASE).expanduser() / rel
    value = None
    if not a.remove:
        value = sys.stdin.read().strip()
        if len(value) < 20 or re.search(r"\s", value) or "=" in value[:4]:
            sys.exit("what was read from stdin does not look like a single secret (empty, has spaces, or has a NAME= in front); nothing changed")
        if a.var == "CLOUDFLARE_API_TOKEN" and not a.no_verify:
            problems = verify_cloudflare(value, get_var(local, "CLOUDFLARE_ACCOUNT_ID"), a.pages_project if a.project == "budgetapp" else None)
            if problems:
                sys.exit("refused, nothing changed:\n  - " + "\n  - ".join(problems))
            print("Cloudflare accepts the token and it reaches the Pages projects")
    todo = {"this machine": None, **worker_hosts()}
    ok = []
    for name, host in todo.items():
        if host is None:
            r = subprocess.run(["python3", "-c", REMOTE], input=json.dumps({"path": str(local), "var": a.var, "value": value}), capture_output=True, text=True)
        else:
            r = subprocess.run(["ssh", "-o", "ConnectTimeout=10", host, "python3", "-c", shlex.quote(REMOTE)],
                               input=json.dumps({"path": f"{BASE}/{rel}", "var": a.var, "value": value}), capture_output=True, text=True)
        ok.append((name, r.returncode == 0 and "ok" in r.stdout))
    print(("removed " if a.remove else "set ") + f"{a.var} in {a.project}'s .env.deploy: " + ", ".join(f"{n} {'ok' if g else 'FAILED'}" for n, g in ok))
    return 0 if all(g for _, g in ok) else 1


if __name__ == "__main__":
    sys.exit(main())
