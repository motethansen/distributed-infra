#!/usr/bin/env python3
"""
da — Distributed Agents interactive CLI (runs on MacBook Pro orchestrator).

Launch:
    python orchestrator/da.py
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import textwrap
from datetime import datetime
from pathlib import Path

import httpx
import yaml
from dotenv import load_dotenv
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

load_dotenv(Path(__file__).parent.parent / ".env")

BASE_URL   = os.getenv("ORCHESTRATOR_URL", "http://localhost:8000")
SECRET     = os.getenv("SECRET_KEY", "")
HEADERS    = {"x-secret-key": SECRET}
CONFIG        = Path(__file__).parent.parent / "config" / "machines.yaml"
SKILLS_CONFIG = Path(__file__).parent.parent / "config" / "skills.yaml"
HANDLERS_DIR  = Path(__file__).parent.parent / "worker" / "handlers"
HIST_FILE    = Path.home() / ".da_history"
AGENTS_RUNNER = Path(__file__).parent.parent / "agents" / "runner.py"
VENV_PYTHON   = Path(__file__).parent.parent / ".venv" / "bin" / "python"

console = Console()

PROMPT_STYLE = Style.from_dict({
    "prompt":    "#00d7ff bold",
    "rprompt":   "#555555",
})

AGENTS = ["claude", "gemini", "codex", "groq"]

COMMANDS = [
    "run", "test", "assign", "queue", "review", "failures",
    "status", "skills", "resolve", "ssh", "help", "exit", "quit",
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _client() -> httpx.Client:
    return httpx.Client(base_url=BASE_URL, headers=HEADERS, timeout=10)


def _machines() -> dict:
    with open(CONFIG) as f:
        return yaml.safe_load(f).get("machines", {})


def _worker_machines() -> dict:
    return {k: v for k, v in _machines().items() if v.get("role") == "worker"}


def _worker_health(name: str, cfg: dict) -> tuple[bool, int | None]:
    """Returns (online, active_task_count)."""
    ip   = cfg.get("tailscale_ip", "")
    port = cfg.get("worker_port", 8001)
    try:
        r = httpx.get(f"http://{ip}:{port}/health", headers=HEADERS, timeout=3)
        if r.status_code == 200:
            data = r.json()
            return True, data.get("active_tasks", 0)
        return True, None
    except Exception:
        return False, None


def _queue_stats() -> dict[str, dict]:
    """Returns per-machine stats derived from the task queue.

    Maps historical machine names (aliases) back to their canonical name so
    renamed machines don't lose their stats history.
    """
    # Build alias → canonical name map from machines.yaml
    alias_map: dict[str, str] = {}
    for name, cfg in _machines().items():
        alias_map[name] = name
        for alias in cfg.get("aliases", []):
            alias_map[alias] = name

    stats: dict[str, dict] = {}
    try:
        with _client() as c:
            tasks = c.get("/tasks", params={"limit": 500}).json()
    except Exception:
        return stats

    for t in tasks:
        raw_machine = t.get("assigned_to") or "unassigned"
        machine = alias_map.get(raw_machine, raw_machine)  # resolve alias → canonical
        if machine not in stats:
            stats[machine] = {"done": 0, "failed": 0, "in_progress": 0, "llm_counts": {}}
        s = t.get("status", "")
        if s == "done":
            stats[machine]["done"] += 1
        elif s == "failed":
            stats[machine]["failed"] += 1
        elif s in ("claimed", "in_progress"):
            stats[machine]["in_progress"] += 1
        llm = (t.get("payload") or {}).get("agent")
        if llm:
            lc = stats[machine]["llm_counts"]
            lc[llm] = lc.get(llm, 0) + 1
    return stats


def _top_llm(llm_counts: dict) -> str:
    if not llm_counts:
        return "-"
    top = max(llm_counts, key=llm_counts.get)
    return f"{top} ({llm_counts[top]})"


def _claude_route(description: str) -> dict:
    """Ask Claude which machine + LLM to use for a task. Returns routing dict."""
    machines = _worker_machines()
    machine_summary = "\n".join(
        f"  - {name}: capabilities={cfg.get('capabilities', [])}, os={cfg.get('os','?')}"
        for name, cfg in machines.items()
    )
    prompt = (
        "You are a task router for a distributed developer workstation. "
        "Given the available worker machines and a task description, "
        "respond with ONLY a JSON object (no markdown, no explanation) with keys: "
        '"machine" (exact machine name from the list), '
        '"llm" (one of: claude, gemini, codex, groq), '
        '"task_type" (one of: android_build, ios_build, npm_build, git_pull, test_run, lint, run_script, agent_run, custom), '
        '"reason" (one short sentence). '
        f"\n\nWorker machines:\n{machine_summary}"
        f"\n\nTask: {description}"
    )
    try:
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True, text=True, timeout=30,
        )
        raw = result.stdout.strip()
        # strip any accidental markdown fences
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw)
    except Exception as e:
        return {"error": str(e)}


def _push_task(task_type: str, payload: dict, notes: str = "", priority: int = 5, machine: str = "") -> dict | None:
    try:
        body: dict = {
            "type": task_type,
            "payload": payload,
            "priority": priority,
            "notes": notes or None,
        }
        if machine:
            body["payload"]["_target_machine"] = machine
        with _client() as c:
            r = c.post("/tasks", json=body)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        console.print(f"[red]✗ Failed to push task: {e}[/red]")
        return None


# ── Command handlers ───────────────────────────────────────────────────────────

def cmd_assign(args: list[str]) -> None:
    """assign [description] [--machine=X] [--agent=Y] [--type=Z]"""
    if not args:
        console.print(
            "[dim]Usage: assign <task description> [--machine=mac-mini] [--agent=claude] [--type=agent_run]\n"
            "  --agent accepts: claude, gemini, codex, groq\n"
            "  --machine must match a name in config/machines.yaml\n"
            "  Tip: omit the description to enter multi-line prompt mode.[/dim]"
        )
        return

    # Parse inline flags
    flags: dict[str, str] = {}
    words: list[str] = []
    for a in args:
        if a.startswith("--") and "=" in a:
            k, v = a[2:].split("=", 1)
            flags[k] = v
        else:
            words.append(a)

    description = " ".join(words)
    explicit_machine = flags.get("machine", "")
    # Accept --agent or --llm (legacy)
    explicit_llm     = flags.get("agent", flags.get("llm", ""))
    explicit_type    = flags.get("type", "")

    # ── Multi-line prompt mode ─────────────────────────────────────────────
    # Triggered when flags are set but no inline description is provided.
    if not description and (explicit_machine or explicit_llm or explicit_type):
        console.print(
            "  [dim]Enter prompt below. Blank line to submit, Ctrl-C to cancel.[/dim]\n"
            "  [dim]Tip: paste multi-line text freely — blank line confirms.[/dim]\n"
        )
        lines: list[str] = []
        try:
            while True:
                line = input("  > ")
                if line == "" and lines:
                    break
                lines.append(line)
        except KeyboardInterrupt:
            console.print("\n  [dim]Cancelled.[/dim]")
            return
        description = "\n".join(lines).strip()
        if not description:
            console.print("  [dim]Empty prompt — nothing to assign.[/dim]")
            return

    routing: dict = {}

    if explicit_machine and explicit_llm and explicit_type:
        routing = {
            "machine": explicit_machine,
            "llm": explicit_llm,
            "task_type": explicit_type,
            "reason": "explicit",
        }
    else:
        console.print("[dim]→ Asking Claude for routing recommendation…[/dim]")
        routing = _claude_route(description)
        if "error" in routing:
            console.print(f"[red]✗ Claude routing failed: {routing['error']}[/red]")
            console.print("[dim]  Falling back — enter routing manually.[/dim]")
            machines = list(_worker_machines().keys())
            console.print(f"  Machines: {', '.join(machines)}")
            explicit_machine = explicit_machine or console.input("  Machine: ").strip()
            explicit_llm     = explicit_llm     or console.input("  Agent (claude/gemini/codex/groq): ").strip()
            explicit_type    = explicit_type    or console.input("  Task type (agent_run/run_script/…): ").strip()
            routing = {"machine": explicit_machine, "llm": explicit_llm, "task_type": explicit_type, "reason": "manual"}
        # Override with any explicit flags
        if explicit_machine:
            routing["machine"] = explicit_machine
        if explicit_llm:
            routing["llm"] = explicit_llm
        if explicit_type:
            routing["task_type"] = explicit_type

    machine   = routing.get("machine", "")
    llm       = routing.get("llm", "claude")
    task_type = routing.get("task_type", "agent_run")
    reason    = routing.get("reason", "")

    # ── Validate machine + agent combination ──────────────────────────────────
    machines_cfg = _machines()
    warnings: list[str] = []

    if machine and machine not in machines_cfg:
        valid = ", ".join(machines_cfg.keys())
        console.print(f"[red]✗ Unknown machine '{machine}'. Valid: {valid}[/red]")
        return

    if machine:
        mcfg = machines_cfg[machine]

        # Check task type is in machine capabilities
        caps = mcfg.get("capabilities", [])
        if caps and task_type not in caps:
            console.print(
                f"[red]✗ Machine '{machine}' does not have capability '{task_type}'.[/red]\n"
                f"  Its capabilities: {', '.join(caps)}"
            )
            return

        # Warn if agent not listed for machine
        agents_list = mcfg.get("agents", [])
        if agents_list and llm not in agents_list:
            warnings.append(
                f"[yellow]⚠ Agent '{llm}' is not listed for '{machine}' "
                f"(listed: {', '.join(agents_list)}). It may not be installed.[/yellow]"
            )

    for w in warnings:
        console.print(f"  {w}")

    console.print(
        f"\n  [bold cyan]Machine[/bold cyan]   {machine or '[dim]auto[/dim]'}\n"
        f"  [bold cyan]Agent[/bold cyan]     {llm}\n"
        f"  [bold cyan]Task type[/bold cyan] {task_type}\n"
        f"  [bold cyan]Reason[/bold cyan]    [dim]{reason}[/dim]\n"
    )

    confirm = console.input("  Confirm? [Y/n] ").strip().lower()
    if confirm not in ("", "y", "yes"):
        console.print("[dim]  Cancelled.[/dim]")
        return

    payload = {"agent": llm, "prompt": description}
    task = _push_task(task_type, payload, notes=description, machine=machine)
    if task:
        console.print(f"\n  [green]✓ Task queued[/green]  [dim]{task['id'][:8]}[/dim]  →  {machine} / {llm}\n")


def cmd_queue(args: list[str]) -> None:
    """queue [--status=pending|done|failed|in_progress|needs_human]"""
    status_filter = ""
    for a in args:
        if a.startswith("--status="):
            status_filter = a.split("=", 1)[1]

    try:
        with _client() as c:
            params = {"status": status_filter} if status_filter else {}
            resp = c.get("/tasks", params=params)
            resp.raise_for_status()
            tasks = resp.json()
    except Exception as e:
        console.print(f"[red]✗ {e}[/red]")
        return

    if not tasks:
        console.print("[dim]  No tasks found.[/dim]")
        return

    STATUS_COLORS = {
        "pending":     "yellow",
        "claimed":     "blue",
        "in_progress": "cyan",
        "done":        "green",
        "failed":      "red",
        "needs_human": "bold magenta",
    }

    table = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold")
    table.add_column("ID",       style="dim", width=8)
    table.add_column("Type",     width=14)
    table.add_column("Status",   width=12)
    table.add_column("Machine",  width=16)
    table.add_column("Agent",    width=10)
    table.add_column("Task / Notes")

    for t in tasks:
        color   = STATUS_COLORS.get(t["status"], "white")
        payload = t.get("payload") or {}
        agent   = payload.get("agent", "-")
        prompt  = payload.get("prompt", "")
        label   = (t.get("notes") or prompt or "")[:50]

        # Show assigned_to if claimed/running, else show target from payload
        assigned = t.get("assigned_to") or ""
        target   = payload.get("_target_machine", "")
        if assigned:
            machine_col = assigned
        elif target:
            machine_col = f"[dim]→{target}[/dim]"  # pending, targeted
        else:
            machine_col = "[dim]any[/dim]"

        table.add_row(
            t["id"][:8],
            t["type"],
            f"[{color}]{t['status']}[/{color}]",
            machine_col,
            agent,
            label,
        )

    console.print()
    console.print(table)
    console.print(f"  [dim]{len(tasks)} task(s){' · filter: ' + status_filter if status_filter else ''}[/dim]\n")


def cmd_status(args: list[str]) -> None:
    """Show per-machine health and consumption stats."""
    machines = _machines()
    stats    = _queue_stats()

    table = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold")
    table.add_column("Machine",     width=22)
    table.add_column("Role",        width=12)
    table.add_column("Online",      width=8)
    table.add_column("Active",      width=8)
    table.add_column("Done",        width=7)
    table.add_column("Failed",      width=8)
    table.add_column("Top LLM",     width=16)

    for name, cfg in machines.items():
        role = cfg.get("role", "?")
        if role == "orchestrator":
            ping = subprocess.run(["ping", "-c1", "-W1", cfg.get("tailscale_ip", "")], capture_output=True)
            online_str = "[green]✓[/green]" if ping.returncode == 0 else "[red]✗[/red]"
            table.add_row(name, role, online_str, "-", "-", "-", "-")
        else:
            online, active = _worker_health(name, cfg)
            online_str = "[green]✓[/green]" if online else "[red]✗[/red]"
            active_str = str(active) if active is not None else "-"
            ms = stats.get(name, {})
            table.add_row(
                name, role, online_str, active_str,
                str(ms.get("done", 0)),
                str(ms.get("failed", 0)),
                _top_llm(ms.get("llm_counts", {})),
            )

    console.print()
    console.print(table)

    # LLM breakdown per machine
    any_llm = any(stats.get(n, {}).get("llm_counts") for n in machines if machines[n].get("role") == "worker")
    if any_llm:
        console.print("  [bold]LLM usage breakdown[/bold]")
        for name, cfg in machines.items():
            if cfg.get("role") != "worker":
                continue
            lc = stats.get(name, {}).get("llm_counts", {})
            if lc:
                breakdown = "  ·  ".join(f"{llm}: {count}" for llm, count in sorted(lc.items(), key=lambda x: -x[1]))
                console.print(f"    [cyan]{name}[/cyan]  {breakdown}")
    console.print()


# ── Skills ────────────────────────────────────────────────────────────────────

def _skills_registry() -> dict:
    """Load skills from config/skills.yaml. Returns empty dict if file missing."""
    if not SKILLS_CONFIG.exists():
        return {}
    with open(SKILLS_CONFIG) as f:
        return yaml.safe_load(f).get("skills", {})


def _ssh_check(ip: str, cmd: str) -> bool:
    r = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", ip, cmd],
        capture_output=True,
    )
    return r.returncode == 0


def cmd_skills(args: list[str]) -> None:
    """
    skills                            — list declared capabilities per machine
    skills available [--category=X]  — all registry skills + install status per machine
    skills list <machine>             — SSH-check what's actually installed on a machine
    skills install <machine> <skill>  — install a skill via SSH
    skills add <machine> <cap>        — add capability to machines.yaml
    skills create <name>              — scaffold a new skill handler + registry entry
    """
    machines = _machines()

    # ── no sub-command: declared capabilities overview ──────────────────────────
    if not args:
        registry = _skills_registry()
        table = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold")
        table.add_column("Machine",      width=24)
        table.add_column("OS",           width=8)
        table.add_column("Capabilities")
        for name, cfg in machines.items():
            caps = ", ".join(cfg.get("capabilities", [])) or "[dim]none[/dim]"
            table.add_row(name, cfg.get("os", "?"), caps)
        console.print()
        console.print(table)
        console.print(
            "  [dim]skills available              — browse the full skill registry[/dim]\n"
            "  [dim]skills list <machine>         — verify what's installed via SSH[/dim]\n"
            "  [dim]skills install <machine> <skill>[/dim]\n"
            "  [dim]skills create <name>          — scaffold a new custom skill[/dim]\n"
            f"  [dim]{len(registry)} skills in registry ({SKILLS_CONFIG.name})[/dim]\n"
        )
        return

    sub = args[0].lower()

    # ── skills available [--category=X] ─────────────────────────────────────────
    if sub == "available":
        registry = _skills_registry()
        category_filter = ""
        for a in args[1:]:
            if a.startswith("--category="):
                category_filter = a.split("=", 1)[1].lower()

        workers = _worker_machines()
        worker_names = list(workers.keys())

        # Group by category
        by_cat: dict[str, list[tuple[str, dict]]] = {}
        for skill_name, skill in registry.items():
            cat = skill.get("category", "custom")
            if category_filter and cat.lower() != category_filter:
                continue
            by_cat.setdefault(cat, []).append((skill_name, skill))

        if not by_cat:
            console.print(f"\n  [dim]No skills found{' in category: ' + category_filter if category_filter else ''}.[/dim]\n")
            return

        # Build SSH check results (parallel per worker)
        # We'll check lazily per skill row to avoid too many SSH calls upfront
        console.print()
        for cat, skills_in_cat in sorted(by_cat.items()):
            console.print(f"  [bold]{cat.upper().replace('-', ' ')}[/bold]")
            table = Table(box=box.SIMPLE, show_header=True, header_style="dim", padding=(0, 1))
            table.add_column("Skill",        width=18, style="cyan")
            for wname in worker_names:
                table.add_column(wname[:14],  width=16)
            table.add_column("Description")

            for skill_name, skill in sorted(skills_in_cat):
                check_cmd = skill.get("check", "")
                row = [skill_name]
                for wname, wcfg in workers.items():
                    ip = wcfg.get("tailscale_ip", "")
                    if check_cmd and ip:
                        ok = _ssh_check(ip, check_cmd)
                        row.append("[green]✓ installed[/green]" if ok else "[dim]✗ missing[/dim]")
                    else:
                        row.append("[dim]?[/dim]")
                row.append(f"[dim]{skill.get('description', '')}[/dim]")
                table.add_row(*row)
            console.print(table)
            console.print()
        return

    # ── skills list <machine> ────────────────────────────────────────────────────
    if sub == "list" and len(args) >= 2:
        name = args[1]
        if name not in machines:
            console.print(f"[red]✗ Unknown machine: {name}[/red]")
            return
        registry = _skills_registry()
        ip = machines[name]["tailscale_ip"]
        console.print(f"\n  Checking skills on [cyan]{name}[/cyan] ({ip})…\n")
        table = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold")
        table.add_column("Skill",       width=18)
        table.add_column("Category",    width=14)
        table.add_column("Status",      width=14)
        table.add_column("Check command")
        for skill_name, skill in sorted(registry.items()):
            check_cmd = skill.get("check", "")
            ok = _ssh_check(ip, check_cmd) if check_cmd else None
            if ok is None:
                status = "[dim]no check[/dim]"
            else:
                status = "[green]✓ installed[/green]" if ok else "[dim]✗ missing[/dim]"
            table.add_row(skill_name, skill.get("category", "?"), status, check_cmd)
        console.print(table)
        console.print()
        return

    # ── skills install <machine> <skill> ─────────────────────────────────────────
    if sub == "install" and len(args) >= 3:
        name  = args[1]
        skill_name = args[2]
        if name not in machines:
            console.print(f"[red]✗ Unknown machine: {name}[/red]")
            return
        registry = _skills_registry()
        if skill_name not in registry:
            console.print(f"[red]✗ Unknown skill: {skill_name}[/red]")
            console.print(f"  Known skills: {', '.join(sorted(registry))}")
            return
        skill = registry[skill_name]
        cfg   = machines[name]
        os_   = cfg.get("os", "linux")
        ip    = cfg["tailscale_ip"]
        cmd   = (skill.get("install") or {}).get(os_)
        if not cmd:
            console.print(f"[red]✗ No install recipe for '{skill_name}' on {os_}[/red]")
            return
        if cmd.startswith("#"):
            console.print(f"  [yellow]Manual step required:[/yellow] {cmd[2:].strip()}")
            return
        console.print(f"\n  Installing [bold]{skill_name}[/bold] on [cyan]{name}[/cyan]…")
        console.print(f"  [dim]$ {cmd}[/dim]\n")
        result = subprocess.run(["ssh", "-t", ip, cmd], timeout=300)
        if result.returncode == 0:
            console.print(f"\n  [green]✓ {skill_name} installed on {name}[/green]\n")
        else:
            console.print(f"\n  [red]✗ Install failed (exit {result.returncode})[/red]\n")
        return

    # ── skills add <machine> <capability> ────────────────────────────────────────
    if sub == "add" and len(args) >= 3:
        name = args[1]
        cap  = args[2]
        if name not in machines:
            console.print(f"[red]✗ Unknown machine: {name}[/red]")
            return
        with open(CONFIG) as f:
            data = yaml.safe_load(f)
        caps: list = data["machines"][name].setdefault("capabilities", [])
        if cap in caps:
            console.print(f"  [dim]{cap} already listed for {name}[/dim]")
            return
        caps.append(cap)
        with open(CONFIG, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        console.print(f"  [green]✓ Added capability '{cap}' to {name}[/green]")
        return

    # ── skills create <name> ─────────────────────────────────────────────────────
    if sub == "create" and len(args) >= 2:
        skill_name = args[1].lower().replace(" ", "_").replace("-", "_")
        registry = _skills_registry()
        if skill_name in registry:
            console.print(f"  [yellow]Skill '{skill_name}' already exists in the registry.[/yellow]")
            return

        console.print(f"\n  [bold]Creating new skill:[/bold] [cyan]{skill_name}[/cyan]\n")

        def _ask(prompt: str, default: str = "") -> str:
            suffix = f" [{default}]" if default else ""
            val = console.input(f"  {prompt}{suffix}: ").strip()
            return val or default

        description  = _ask("Description (one line)")
        category     = _ask("Category", "custom")
        check_cmd    = _ask("Check command (exits 0 if installed)", f"{skill_name} --version")
        install_mac  = _ask("Install command (macos)", "")
        install_linux = _ask("Install command (linux)", "")
        task_type    = _ask("Task type this enables", "run_script")

        # Scaffold handler file
        handler_path = HANDLERS_DIR / f"{skill_name}.py"
        handler_rel  = f"worker/handlers/{skill_name}.py"
        if not handler_path.exists():
            handler_path.write_text(
                f'"""Handler for {skill_name} tasks."""\n'
                f'from __future__ import annotations\n\n'
                f'from shared.models import Task\n\n\n'
                f'async def handle_{skill_name}(task: Task) -> dict:\n'
                f'    """\n'
                f'    payload:\n'
                f'      # TODO: document expected payload keys\n'
                f'    """\n'
                f'    # TODO: implement handler\n'
                f'    return {{"needs_human": True, "notes": "handle_{skill_name} not yet implemented"}}\n'
            )
            console.print(f"  [green]✓ Created handler:[/green] {handler_rel}")
        else:
            console.print(f"  [dim]Handler already exists: {handler_rel}[/dim]")

        # Append to skills.yaml
        with open(SKILLS_CONFIG) as f:
            raw_yaml = f.read()

        new_entry = (
            f"\n  {skill_name}:\n"
            f"    description: \"{description}\"\n"
            f"    category: {category}\n"
            f"    check: \"{check_cmd}\"\n"
            f"    install:\n"
        )
        if install_mac:
            new_entry += f"      macos: \"{install_mac}\"\n"
        if install_linux:
            new_entry += f"      linux: \"{install_linux}\"\n"
        new_entry += (
            f"    task_types: [{task_type}]\n"
            f"    handler: {handler_rel}\n"
        )

        with open(SKILLS_CONFIG, "a") as f:
            f.write(new_entry)
        console.print(f"  [green]✓ Registered in skills.yaml[/green]")

        # Add dispatch stub hint
        console.print(
            f"\n  [bold]Next steps:[/bold]\n"
            f"  1. Implement [cyan]{handler_rel}[/cyan]\n"
            f"  2. Add to [cyan]worker/handlers/__init__.py[/cyan] dispatch:\n"
            f"     [dim]if task.type == \"{task_type}\":\n"
            f"         from worker.handlers.{skill_name} import handle_{skill_name}\n"
            f"         return await handle_{skill_name}(task)[/dim]\n"
            f"  3. Add the capability to machines.yaml:\n"
            f"     [dim]skills add <machine> {task_type}[/dim]\n"
        )
        return

    # ── unknown sub-command ──────────────────────────────────────────────────────
    console.print(
        "[dim]Usage:\n"
        "  skills                             list declared capabilities\n"
        "  skills available [--category=X]   browse the skill registry\n"
        "  skills list <machine>              SSH-check installed skills\n"
        "  skills install <machine> <skill>   install a skill via SSH\n"
        "  skills add <machine> <cap>         register capability in machines.yaml\n"
        "  skills create <name>               scaffold a new skill[/dim]"
    )


def cmd_run(args: list[str]) -> None:
    """
    run <agent> <prompt>   — run an agent directly on this MacBook (no queue)
    run <agent>            — enter multi-line prompt mode
    """
    if not args:
        console.print(
            f"[dim]Usage: run <agent> <prompt>\n"
            f"  Agents: {', '.join(AGENTS)}\n"
            f"  Example: run claude write a hello world function[/dim]"
        )
        return

    agent = args[0].lower()
    if agent not in AGENTS:
        console.print(f"[red]✗ Unknown agent '{agent}'[/red]  Choose: {', '.join(AGENTS)}")
        return

    if len(args) > 1:
        prompt = " ".join(args[1:])
    else:
        console.print(f"  [dim]Enter prompt (blank line to submit, Ctrl-C to cancel):[/dim]")
        lines: list[str] = []
        try:
            while True:
                line = input("  > ")
                if line == "" and lines:
                    break
                lines.append(line)
        except KeyboardInterrupt:
            console.print("\n  [dim]Cancelled.[/dim]")
            return
        prompt = "\n".join(lines)

    if not prompt.strip():
        console.print("[dim]  Empty prompt — nothing to run.[/dim]")
        return

    console.print(f"\n  [dim]Running [bold]{agent}[/bold] locally…[/dim]\n")
    python = str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable
    result = subprocess.run(
        [python, str(AGENTS_RUNNER), "--agent", agent, "--prompt", prompt],
        cwd=str(AGENTS_RUNNER.parent.parent),
    )
    if result.returncode != 0:
        console.print(f"\n  [red]✗ Agent exited with code {result.returncode}[/red]\n")
    else:
        console.print()


def cmd_test(args: list[str]) -> None:
    """test [agent]  — smoke-test all 4 agents (or one) locally on this MacBook."""
    python = str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable

    if args:
        agent = args[0].lower()
        if agent not in AGENTS:
            console.print(f"[red]✗ Unknown agent '{agent}'[/red]  Choose: {', '.join(AGENTS)}")
            return
        console.print(f"\n  [dim]Smoke-testing [bold]{agent}[/bold]…[/dim]\n")
        subprocess.run(
            [python, str(AGENTS_RUNNER), "--agent", agent, "--prompt", "respond with: I am working correctly."],
            cwd=str(AGENTS_RUNNER.parent.parent),
        )
    else:
        console.print(f"\n  [dim]Smoke-testing all agents on this MacBook…[/dim]\n")
        subprocess.run(
            [python, str(AGENTS_RUNNER), "--test"],
            cwd=str(AGENTS_RUNNER.parent.parent),
        )
    console.print()


def cmd_review(args: list[str]) -> None:
    """review  — show all tasks waiting for human action."""
    try:
        with _client() as c:
            resp = c.get("/tasks/needs-human")
            resp.raise_for_status()
            tasks = resp.json()
    except Exception as e:
        console.print(f"[red]✗ {e}[/red]")
        return

    if not tasks:
        console.print("\n  [green]✓ Nothing needs your attention.[/green]\n")
        return

    console.print(f"\n  [bold magenta]{len(tasks)} task(s) need your review[/bold magenta]\n")
    for t in tasks:
        payload = t.get("payload") or {}
        agent   = payload.get("agent", "-")
        prompt  = payload.get("prompt", "")
        notes_raw = t.get("notes") or ""

        # Split stored "notes | ACTION: action" back into two parts
        action = ""
        notes  = notes_raw
        if " | ACTION: " in notes_raw:
            notes, action = notes_raw.split(" | ACTION: ", 1)
        elif notes_raw.startswith("ACTION: "):
            action = notes_raw[len("ACTION: "):]
            notes  = ""

        console.print(
            f"  [bold]{t['id'][:8]}[/bold]"
            f"  [cyan]{t.get('assigned_to', '?')}[/cyan]"
            f"  [dim]{t['type']}  ·  {agent}[/dim]"
        )
        if notes:
            console.print(f"    [yellow]Issue:[/yellow]  {notes}")
        if action:
            console.print(f"    [bold red]Action:[/bold red] {action}")
        if prompt:
            console.print(f"    [dim]Prompt: {prompt[:120]}[/dim]")
        result = t.get("result") or {}
        if result.get("stderr"):
            console.print(f"    [dim]Stderr: {result['stderr'][:200]}[/dim]")
        console.print(f"    [dim]resolve {t['id'][:8]} done   # or: failed / pending[/dim]")
        console.print()


def cmd_failures(args: list[str]) -> None:
    """failures  — show all failed tasks with error details."""
    try:
        with _client() as c:
            resp = c.get("/tasks", params={"status": "failed"})
            resp.raise_for_status()
            tasks = resp.json()
    except Exception as e:
        console.print(f"[red]✗ {e}[/red]")
        return

    if not tasks:
        console.print("\n  [green]✓ No failed tasks.[/green]\n")
        return

    console.print(f"\n  [bold red]{len(tasks)} failed task(s)[/bold red]\n")
    for t in tasks:
        llm    = (t.get("payload") or {}).get("agent", "-")
        prompt = (t.get("payload") or {}).get("prompt", "")
        notes  = t.get("notes", "") or ""
        # Show only the first meaningful error line — skip long stack traces
        error_line = next(
            (ln.strip() for ln in notes.splitlines() if ln.strip() and not ln.strip().startswith("at ")),
            notes[:120],
        )
        console.print(
            f"  [bold]{t['id'][:8]}[/bold]"
            f"  [cyan]{t.get('assigned_to', '?')}[/cyan]"
            f"  [dim]{llm}[/dim]"
        )
        if prompt:
            console.print(f"    prompt: [dim]{prompt[:100]}[/dim]")
        if error_line:
            console.print(f"    error:  [red]{error_line[:120]}[/red]")
        console.print(f"    [dim]resolve {t['id'][:8]} pending   # re-queue  |  resolve {t['id'][:8]} done   # dismiss[/dim]")
        console.print()


def cmd_resolve(args: list[str]) -> None:
    """
    resolve <task-id> [done|failed|pending] [--notes=...]
    resolve all                                              — mark every needs_human task as done
    """
    if not args:
        console.print(
            "[dim]Usage:\n"
            "  resolve <task-id> [done|failed|pending] [--notes=reason]\n"
            "  resolve all   — bulk-close all needs_human tasks[/dim]"
        )
        return

    # Bulk resolve
    if args[0].lower() == "all":
        action = args[1] if len(args) > 1 and not args[1].startswith("--") else "done"
        try:
            with _client() as c:
                tasks = c.get("/tasks/needs-human").json()
        except Exception as e:
            console.print(f"  [red]✗ {e}[/red]")
            return
        if not tasks:
            console.print("  [dim]No needs_human tasks to resolve.[/dim]")
            return
        console.print(f"\n  Resolving {len(tasks)} task(s) as [bold]{action}[/bold]…")
        with _client() as c:
            for t in tasks:
                try:
                    c.patch(f"/tasks/{t['id']}", json={"status": action}).raise_for_status()
                    console.print(f"    [green]✓[/green] {t['id'][:8]}  {(t.get('payload') or {}).get('agent','-')}  {t.get('assigned_to','?')}")
                except Exception as e:
                    console.print(f"    [red]✗[/red] {t['id'][:8]}  {e}")
        console.print()
        return

    # Single resolve
    prefix  = args[0]
    action  = args[1] if len(args) > 1 and not args[1].startswith("--") else "done"
    notes   = ""
    for a in args:
        if a.startswith("--notes="):
            notes = a.split("=", 1)[1]

    # Resolve full UUID from prefix (queue shows truncated 8-char IDs)
    task_id = prefix
    if len(prefix) < 32:
        try:
            with _client() as c:
                all_tasks = c.get("/tasks", params={"limit": 500}).json()
            matches = [t["id"] for t in all_tasks if t["id"].startswith(prefix)]
            if not matches:
                console.print(f"  [red]✗ No task found with prefix '{prefix}'[/red]")
                return
            if len(matches) > 1:
                console.print(f"  [red]✗ Ambiguous prefix '{prefix}' matches {len(matches)} tasks — use more characters[/red]")
                return
            task_id = matches[0]
        except Exception as e:
            console.print(f"  [red]✗ Could not look up task: {e}[/red]")
            return

    try:
        with _client() as c:
            r = c.patch(f"/tasks/{task_id}", json={"status": action, "notes": notes or None})
            r.raise_for_status()
        console.print(f"  [green]✓ Task {task_id[:8]} marked {action}[/green]")
    except Exception as e:
        console.print(f"  [red]✗ {e}[/red]")


def cmd_ssh(args: list[str]) -> None:
    """ssh <machine>"""
    if not args:
        machines = list(_worker_machines().keys())
        console.print(f"[dim]Usage: ssh <machine>   Machines: {', '.join(machines)}[/dim]")
        return
    name = args[0]
    cfg  = _machines().get(name)
    if not cfg:
        console.print(f"[red]✗ Unknown machine: {name}[/red]")
        return
    ip = cfg["tailscale_ip"]
    console.print(f"  [dim]Opening SSH session → {name} ({ip})[/dim]\n")
    os.execvp("ssh", ["ssh", ip])


def cmd_help() -> None:
    help_text = textwrap.dedent("""\
    [bold cyan]Local agents  (run on this MacBook, no queue)[/bold cyan]

      [bold]run[/bold] <agent> <prompt>
          Run an agent directly here. Agents: claude, gemini, codex, groq
          Example: run claude write a hello world function in Python

      [bold]run[/bold] <agent>
          Run an agent in multi-line prompt mode (blank line to submit).

      [bold]test[/bold] [agent]
          Smoke-test all 4 agents (or one) to confirm they work locally.
          Example: test          → tests claude, gemini, codex, groq
                   test gemini   → tests gemini only

    [bold cyan]Queue  (send tasks to workers)[/bold cyan]

      [bold]assign[/bold] <task description> [--machine=X] [--agent=Y] [--type=Z]
          Push a task to a worker. If flags are omitted, Claude recommends routing.
          --agent: claude, gemini, codex, groq  (--llm=Y also accepted)
          --machine must match a name in config/machines.yaml
          Validates that the machine supports the task type and agent before queuing.
          Example: assign refactor the auth module for better error handling
                   assign build the iOS app --machine=mac-mini --agent=gemini

      [bold]queue[/bold] [--status=pending|done|failed|in_progress|needs_human]
          View queued tasks. Pending tasks show target machine (→mac-mini) even
          before they are claimed by a worker.

      [bold]review[/bold]
          Show all tasks waiting for human action, with resolve hints.

      [bold]failures[/bold]
          Show all failed tasks with the error message and re-queue / dismiss hints.

      [bold]resolve[/bold] <task-id> [done|failed|pending] [--notes=reason]
          Mark a needs_human task as done, failed, or re-queued.
      [bold]resolve all[/bold]
          Bulk-close every needs_human task as done.

    [bold cyan]Machines[/bold cyan]

      [bold]status[/bold]
          Per-machine health, active tasks, completion stats, and LLM usage.

      [bold]ssh[/bold] <machine>
          Open an interactive SSH session to a worker machine.

    [bold cyan]Skills[/bold cyan]

      [bold]skills[/bold]
          List declared capabilities for all machines.
      [bold]skills available[/bold] [--category=mobile|ai-agent|backend|infrastructure]
          Browse the full skill registry with install status per machine (via SSH).
      [bold]skills list[/bold] <machine>
          SSH in and check which tools are actually installed on a machine.
      [bold]skills install[/bold] <machine> <skill>
          Install a skill on a machine via SSH using the registry recipe.
      [bold]skills add[/bold] <machine> <capability>
          Register a new capability in machines.yaml.
      [bold]skills create[/bold] <name>
          Scaffold a new custom skill: handler file + registry entry.
          Walks you through description, install commands, and task type.

      [bold]help[/bold]     Show this help.
      [bold]exit[/bold]     Quit.
    """)
    console.print()
    console.print(help_text)


# ── Banner ────────────────────────────────────────────────────────────────────

def _print_banner() -> None:
    machines = _machines()
    workers  = {k: v for k, v in machines.items() if v.get("role") == "worker"}
    online   = sum(1 for cfg in workers.values() if _worker_health(cfg["tailscale_ip"] if False else "", cfg)[0]
                   ) if False else "?"

    # Quick ping count
    online_count = 0
    for cfg in workers.values():
        ping = subprocess.run(
            ["ping", "-c1", "-W1", cfg.get("tailscale_ip", "")],
            capture_output=True,
        )
        if ping.returncode == 0:
            online_count += 1

    machine_names = "  ·  ".join(machines.keys())

    console.print()
    console.print(Panel.fit(
        f"[bold cyan]Distributed Agents[/bold cyan]\n"
        f"[dim]{machine_names}[/dim]\n"
        f"[dim]{online_count}/{len(workers)} worker{'s' if len(workers) != 1 else ''} online[/dim]",
        border_style="cyan",
        padding=(0, 2),
    ))
    console.print()
    console.print("  [dim]Type [bold]help[/bold] for commands, [bold]exit[/bold] to quit.[/dim]\n")


# ── REPL ──────────────────────────────────────────────────────────────────────

DISPATCH = {
    "run":      cmd_run,
    "test":     cmd_test,
    "assign":   cmd_assign,
    "queue":    cmd_queue,
    "review":   cmd_review,
    "failures": cmd_failures,
    "status":   cmd_status,
    "skills":   cmd_skills,
    "resolve":  cmd_resolve,
    "ssh":      cmd_ssh,
}


def run_repl() -> None:
    _print_banner()

    completer = WordCompleter(
        COMMANDS + list(_machines().keys()),
        ignore_case=True,
        sentence=False,
    )
    session: PromptSession = PromptSession(
        history=FileHistory(str(HIST_FILE)),
        completer=completer,
        style=PROMPT_STYLE,
    )

    while True:
        try:
            raw = session.prompt(HTML("<prompt>da</prompt> <rprompt>›</rprompt> "))
        except (EOFError, KeyboardInterrupt):
            console.print("\n  [dim]Goodbye.[/dim]\n")
            break

        raw = raw.strip().lstrip("/")   # accept /command and command equally
        if not raw:
            continue
        if raw in ("exit", "quit"):
            console.print("\n  [dim]Goodbye.[/dim]\n")
            break
        if raw in ("help", "?"):
            cmd_help()
            continue

        parts = raw.split()
        cmd   = parts[0].lower()
        args  = parts[1:]

        if cmd in DISPATCH:
            try:
                DISPATCH[cmd](args)
            except Exception as e:
                console.print(f"  [red]✗ Error: {e}[/red]")
        else:
            console.print(f"  [dim]Unknown command '{cmd}'. Type [bold]help[/bold] for the list.[/dim]")


if __name__ == "__main__":
    run_repl()
