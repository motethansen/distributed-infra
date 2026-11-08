#!/usr/bin/env python3
"""Deploy audit (DI-068): is each application's DigitalOcean Functions namespace holding only that application's code?

Michael, 2026-11-08: for every deploy there should be a difference on the code packages deployed, so that the different applications do not
mix up. doctl keeps ONE connected namespace per machine, so a deploy run while another project's namespace was connected quietly puts one
application's packages into another's namespace. This reads, per project, the namespace its OWN access key reaches, lists the packages
deployed there and compares them with the packages the project declares in its project.yml:

    deployed here, declared by this project   -> ok
    deployed here, declared by ANOTHER project -> FOREIGN: it came from a deploy into the wrong namespace
    declared but not deployed                  -> missing (not deployed yet, or deployed elsewhere)

It is read-only: it never deploys, deletes or changes anything. Run it before a deploy (nothing foreign is already there) and after one (the
deploy added only this project's packages). `--strict` exits 1 when anything is foreign, a key is rejected, or two projects reach the same
namespace. Usage:  python scripts/deploy_audit.py [--project NAME ...] [--strict] [--json]
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
API = "https://faas-sgp1-18bc02ac.doserverless.co"        # every Vizneo namespace is in sgp1 on this API host
GITHUB = Path(os.environ.get("DEPLOY_AUDIT_BASE", "~/Projects/github")).expanduser()


def load_config(path: Path | None = None) -> dict:
    return yaml.safe_load((path or ROOT / "config" / "deploy_projects.yaml").read_text())


def read_key(env_file: Path) -> str | None:
    try:
        for line in env_file.read_text().splitlines():
            if line.startswith("DO_FUNCTIONS_ACCESS_KEY="):
                return line.split("=", 1)[1].strip().strip("'\"") or None
    except OSError:
        return None
    return None


def declared_packages(project_yml: Path) -> set[str]:
    try:
        doc = yaml.safe_load(project_yml.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return set()
    return {p["name"] for p in doc.get("packages", []) if isinstance(p, dict) and p.get("name")}


def api_get(key: str, path: str, timeout: float = 30):
    req = urllib.request.Request(API + path, headers={"Authorization": "Basic " + base64.b64encode(key.encode()).decode()})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def deployed_packages(actions: list[dict]) -> dict[str, list[str]]:
    """package -> action names, from the actions list ('namespace' is '<ns>' for a bare action or '<ns>/<package>')."""
    out: dict[str, list[str]] = {}
    for a in actions:
        pkg = (a.get("namespace") or "").split("/", 1)[1] if "/" in (a.get("namespace") or "") else "(none)"
        out.setdefault(pkg, []).append(a.get("name", "?"))
    return {k: sorted(v) for k, v in sorted(out.items())}


def audit(cfg: dict, only: list[str] | None = None, fetch=api_get, base: Path = GITHUB) -> dict:
    ignore = set(cfg.get("ignore_packages") or [])
    projects = {n: p for n, p in cfg["projects"].items() if not only or n in only}
    own = {n: declared_packages(base / p["dir"] / p["project_yml"]) for n, p in cfg["projects"].items()}
    result: dict[str, dict] = {}
    for name, p in projects.items():
        env_file = base / p["dir"] / ".env.deploy"
        key = read_key(env_file)
        row: dict = {"project": name, "declared": sorted(own[name]), "key": "missing" if key is None else "present"}
        if key is None:
            result[name] = {**row, "status": "no key"}
            continue
        try:
            namespaces = fetch(key, "/api/v1/namespaces")
            actions = fetch(key, "/api/v1/namespaces/_/actions?limit=200")
        except urllib.error.HTTPError as e:
            result[name] = {**row, "status": f"key rejected (HTTP {e.code})"}
            continue
        except (OSError, ValueError) as e:
            result[name] = {**row, "status": f"unreachable ({type(e).__name__})"}
            continue
        deployed = deployed_packages(actions)
        elsewhere = {pk: sorted(o for o in own if o != name and pk in own[o]) for pk in deployed}
        foreign = {pk: {"actions": acts, "belongs_to": elsewhere[pk] or ["nobody in the config"]}
                   for pk, acts in deployed.items() if pk not in own[name] and pk not in ignore}
        missing = sorted(own[name] - set(deployed))
        result[name] = {**row, "namespace": namespaces[0] if isinstance(namespaces, list) and namespaces else None,
                        "deployed": deployed, "foreign": foreign, "missing": missing,
                        "status": "FOREIGN CODE" if foreign else "ok"}
    spaces: dict[str, list[str]] = {}
    for n, r in result.items():
        if r.get("namespace"):
            spaces.setdefault(r["namespace"], []).append(n)
    for ns, names in spaces.items():
        if len(names) > 1:
            for n in names:
                result[n]["status"] = f"SHARED NAMESPACE with {', '.join(x for x in names if x != n)}"
    return result


def render(result: dict) -> str:
    lines = []
    for name, r in result.items():
        lines.append(f"{name}: {r['status']}")
        if r.get("namespace"):
            lines.append(f"  namespace {r['namespace']}  ·  declared packages: {', '.join(r['declared']) or 'none found (no project.yml)'}")
            lines.append(f"  deployed: " + ", ".join(f"{pk} ({len(a)})" for pk, a in r["deployed"].items()))
        for pk, f in (r.get("foreign") or {}).items():
            lines.append(f"  FOREIGN package {pk}: {len(f['actions'])} function(s) ({', '.join(f['actions'][:4])}{'…' if len(f['actions']) > 4 else ''}), "
                         f"declared by {', '.join(f['belongs_to'])}")
        if r.get("missing"):
            lines.append(f"  declared but not deployed here: {', '.join(r['missing'])}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--project", action="append", help="audit only this project (repeatable)")
    ap.add_argument("--strict", action="store_true", help="exit 1 on foreign code, a rejected key or a shared namespace")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    res = audit(load_config(), a.project)
    print(json.dumps(res, indent=2) if a.json else render(res))
    bad = [r for r in res.values() if r["status"] != "ok"]
    return 1 if (a.strict and bad) else 0


if __name__ == "__main__":
    sys.exit(main())
