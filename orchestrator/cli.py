#!/usr/bin/env python3
"""orch — CLI for the distributed infra queue (runs on MacBook Pro)."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import httpx
import typer
import yaml
from rich.console import Console
from rich.table import Table

app = typer.Typer(help="Distributed infra orchestrator CLI")
console = Console()

BASE_URL = os.getenv("ORCHESTRATOR_URL", "http://localhost:8000")
SECRET = os.getenv("SECRET_KEY", "")
HEADERS = {"x-secret-key": SECRET}
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "machines.yaml")


def _client() -> httpx.Client:
    return httpx.Client(base_url=BASE_URL, headers=HEADERS, timeout=10)


def _load_machines() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f).get("machines", {})


# ── Queue commands ────────────────────────────────────────────────────────────

@app.command("push")
def push_task(
    task_type: str = typer.Argument(..., help="Task type: android_build, ios_build, run_script, git_pull, custom"),
    payload: str = typer.Option("{}", help="JSON payload string"),
    priority: int = typer.Option(5, help="0-10, 10=highest"),
    notes: str = typer.Option("", help="Optional notes"),
):
    """Push a new task onto the queue."""
    with _client() as c:
        resp = c.post("/tasks", json={
            "type": task_type,
            "payload": json.loads(payload),
            "priority": priority,
            "notes": notes or None,
        })
        resp.raise_for_status()
    task = resp.json()
    console.print(f"[green]✓ Task created[/green] [dim]{task['id']}[/dim]")
    console.print(f"  type={task['type']} priority={task['priority']}")


@app.command("ls")
def list_tasks(
    status: str = typer.Option("", help="Filter by status: pending,claimed,in_progress,done,failed,needs_human"),
):
    """List tasks in the queue."""
    with _client() as c:
        params = {"status": status} if status else {}
        resp = c.get("/tasks", params=params)
        resp.raise_for_status()
    tasks = resp.json()
    if not tasks:
        console.print("[dim]No tasks found.[/dim]")
        return

    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("ID", style="dim", width=8)
    table.add_column("Type")
    table.add_column("Status")
    table.add_column("Priority")
    table.add_column("Assigned To")
    table.add_column("Notes")

    STATUS_COLORS = {
        "pending": "yellow",
        "claimed": "blue",
        "in_progress": "cyan",
        "done": "green",
        "failed": "red",
        "needs_human": "bold magenta",
    }
    for t in tasks:
        color = STATUS_COLORS.get(t["status"], "white")
        table.add_row(
            t["id"][:8],
            t["type"],
            f"[{color}]{t['status']}[/{color}]",
            str(t["priority"]),
            t.get("assigned_to") or "-",
            (t.get("notes") or "")[:40],
        )
    console.print(table)


@app.command("review")
def review_tasks():
    """Show all tasks needing human action."""
    with _client() as c:
        resp = c.get("/tasks/needs-human")
        resp.raise_for_status()
    tasks = resp.json()
    if not tasks:
        console.print("[green]No tasks need your attention.[/green]")
        return
    console.print(f"[bold magenta]{len(tasks)} task(s) need your review:[/bold magenta]\n")
    for t in tasks:
        console.print(f"  [bold]{t['id'][:8]}[/bold]  type={t['type']}  from={t.get('assigned_to','?')}")
        console.print(f"    notes: {t.get('notes','(none)')}")
        console.print(f"    payload: {json.dumps(t.get('payload',{}), indent=2)}\n")


@app.command("resolve")
def resolve_task(
    task_id: str = typer.Argument(...),
    action: str = typer.Option("done", help="done | failed | pending (re-queue)"),
    notes: str = typer.Option("", help="Resolution notes"),
):
    """Resolve a needs_human task."""
    with _client() as c:
        resp = c.patch(f"/tasks/{task_id}", json={"status": action, "notes": notes or None})
        resp.raise_for_status()
    console.print(f"[green]✓ Task {task_id[:8]} marked {action}[/green]")


@app.command("get")
def get_task(task_id: str = typer.Argument(...)):
    """Show full details for a task."""
    with _client() as c:
        resp = c.get(f"/tasks/{task_id}")
        resp.raise_for_status()
    console.print_json(json.dumps(resp.json(), indent=2, default=str))


# ── Machine commands ──────────────────────────────────────────────────────────

@app.command("status")
def machine_status():
    """Ping all worker machines via Tailscale."""
    machines = _load_machines()
    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("Machine")
    table.add_column("Role")
    table.add_column("IP")
    table.add_column("Worker API")
    table.add_column("Ping")

    for name, cfg in machines.items():
        role = cfg.get("role", "?")
        ip = cfg.get("tailscale_ip", "")
        port = cfg.get("worker_port") or cfg.get("queue_port", 8000)

        # Ping via tailscale IP
        ping = subprocess.run(["ping", "-c1", "-W1", ip], capture_output=True)
        ping_ok = "[green]✓[/green]" if ping.returncode == 0 else "[red]✗[/red]"

        # Hit /health if worker
        health = "-"
        if role == "worker" and port:
            try:
                r = httpx.get(f"http://{ip}:{port}/health", headers=HEADERS, timeout=3)
                health = "[green]✓[/green]" if r.status_code == 200 else f"[red]{r.status_code}[/red]"
            except Exception:
                health = "[red]unreachable[/red]"

        table.add_row(name, role, ip, health, ping_ok)

    console.print(table)


@app.command("ssh")
def ssh_machine(
    name: str = typer.Argument(..., help="Machine name from machines.yaml"),
    command: str = typer.Option("", help="Run a remote command instead of opening a shell"),
):
    """SSH into a machine by name."""
    machines = _load_machines()
    if name not in machines:
        console.print(f"[red]Unknown machine: {name}[/red]")
        raise typer.Exit(1)
    ip = machines[name]["tailscale_ip"]
    cmd = ["ssh", ip]
    if command:
        cmd += [command]
    os.execvp("ssh", cmd)


if __name__ == "__main__":
    app()
