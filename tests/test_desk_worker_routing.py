"""S-9 routing fixture: the desk worker makes no model call itself and can only reach roles scrum_roles already allows.

Static checks: they read the worker's source and config/roles.yaml; nothing is executed or sent anywhere.
"""
import ast
import re
from pathlib import Path

import yaml

from agents import roles as scrum_roles
from shared.desk_allowlist import ALLOWLIST

ROOT = Path(__file__).resolve().parent.parent
WORKER = ROOT / "scripts" / "desk_worker.py"
LLM_MODULES = {"agents", "anthropic", "openai", "deepseek", "httpx", "requests", "aiohttp", "google", "ollama", "litellm"}
LLM_NAMES = re.compile(r"deepseek|anthropic|openai|claude_agent|fleet_client|FleetClient|\.queue\(|/tasks\b|chat\.completions", re.I)


def _imports(tree):
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            yield from (a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            yield n.module.split(".")[0]


def test_worker_imports_no_llm_client_or_task_queue():
    mods = set(_imports(ast.parse(WORKER.read_text())))
    assert not mods & LLM_MODULES, mods & LLM_MODULES
    assert not {m for m in mods if "dis_agent" in m or "scrum_flow" in m}     # model calls live only in the dis-agent subprocess


def test_worker_source_has_no_model_or_task_queue_call():
    code = "\n".join(l for l in WORKER.read_text().splitlines() if not l.lstrip().startswith("#"))
    tree = ast.parse(code)
    strings = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
               and not n.value.lstrip().startswith(("Desk worker", "Raises", "Read-only", "Mirrors"))]
    hits = [x for x in strings if LLM_NAMES.search(x) and len(x) < 120]
    assert not hits, hits


def test_worker_only_spawns_dis_agent_verbs_as_argv():
    src = WORKER.read_text()
    assert "shell=True" not in src and "os.system" not in src and "Popen" not in src
    verbs = set(re.findall(r'_dis_agent_argv\(cfg, "([a-z-]+)"', src))
    assert verbs == {"plan", "validate", "done"}
    # none of those is a verb that talks to the Product Owner (po, groom) or to a human gate
    assert not verbs & {"po", "groom", "planning", "approve", "go", "close", "cancel", "accept", "chat", "ask"}


def test_worker_verbs_are_t1_and_not_t2_t3():
    t1 = {v for v, e in ALLOWLIST.items() if e["tier"] == "T1"}
    assert t1 == {"refresh_proposal", "plan", "validate", "step_done"}


def _policy():
    return yaml.safe_load((ROOT / "config" / "roles.yaml").read_text())


def _roles_used_by_dis_agent(verb_fn: str) -> set[str]:
    """Role names the dis-agent verb function (and shared.scrum_flow helper it calls) hands to the queue, by static scan."""
    flow = (ROOT / "shared" / "scrum_flow.py").read_text()
    agent = (ROOT / "scripts" / "dis_agent.py").read_text()
    fn = re.search(rf"^def {verb_fn}\(.*?(?=^def )", agent, re.S | re.M).group(0)
    used = set(re.findall(r'"(scrum_master|tester|reviewer|product_owner|analyst|integrator|dev_backend|dev_frontend)"', fn))
    if "plan_story" in fn:
        used |= {"scrum_master"} if '"scrum_master"' in re.search(r"def plan_story.*?(?=\ndef )", flow, re.S).group(0) else set()
    if "validate" in verb_fn:
        used |= {"tester"} if '"tester"' in flow or "tester" in fn else set()
    return used


def test_plan_path_reaches_only_roles_the_policy_allows_and_never_opus():
    pol = _policy()["roles"]
    plan_roles = _roles_used_by_dis_agent("cmd_plan")
    validate_roles = _roles_used_by_dis_agent("cmd_validate")
    assert plan_roles == {"scrum_master"}
    assert validate_roles <= {"tester"}
    for role in plan_roles | validate_roles:
        assert role in pol and role in scrum_roles.role_names()
        assert not pol[role].get("allow_opus")                       # only product_owner may use Opus
        assert not scrum_roles.is_opus(pol[role].get("model"))
        assert pol[role].get("agent") != "deepseek"                  # no holdings or personal data can reach DeepSeek


def test_roles_resolve_to_allowed_agents_with_deepseek_guarded():
    pol = _policy()
    for role in ("scrum_master", "tester"):
        r = scrum_roles.resolve(role, project="vantage", policy=pol)
        assert r["agent"] != "deepseek" and not r["allow_opus"]
    # even a DeepSeek-capable role is moved off DeepSeek for sensitive work or a project not in deepseek_projects
    r = scrum_roles.resolve("dev_backend", project="finance-private", sensitivity="private", policy=pol)
    assert r["agent"] != "deepseek"


def test_worker_has_no_prompt_argument():
    src = WORKER.read_text()
    assert not re.search(r"\bprompt\s*=", src) and not re.search(r"""['"]--(prompt|message|request|revise)""", src)
