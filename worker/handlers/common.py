"""Shared utilities for worker handlers."""
from __future__ import annotations

import asyncio
import os
import re


def _detect_action(stdout: str, stderr: str, returncode: int) -> dict:
    """Analyse script output and return a structured needs_human payload with
    a human-readable notes summary and a specific action the operator must take."""
    combined = (stdout + "\n" + stderr).lower()

    # Cloudflare / API authentication
    if "authentication error" in combined or '"code": 10000' in combined or "code: 10000" in combined:
        return {
            "notes": "Cloudflare API authentication error — token is missing a required permission.",
            "action": "Go to dash.cloudflare.com → My Profile → API Tokens → edit the token → add the missing scope (e.g. Zone → DNS → Edit), then retry the task.",
        }

    # Generic 403 / forbidden
    if "403 forbidden" in combined or '"status": 403' in combined or "error 403" in combined:
        return {
            "notes": "HTTP 403 Forbidden — API credentials were rejected.",
            "action": "Check the API token or key has the required permissions for this endpoint.",
        }

    # 401 unauthorised
    if "401 unauthorized" in combined or "401 unauthorised" in combined or '"status": 401' in combined:
        return {
            "notes": "HTTP 401 Unauthorized — credentials missing or expired.",
            "action": "Re-authenticate: check the token/key in the worker .env and restart the worker.",
        }

    # Command not found
    m = re.search(r"([a-z0-9_\-]+): command not found", combined)
    if m:
        cmd = m.group(1)
        return {
            "notes": f"'{cmd}' is not installed on this machine.",
            "action": f"Install it: da › skills install <machine> {cmd}  — or add it to the worker PATH.",
        }

    # npm / node missing
    if "env: node: no such file" in combined or "node: no such file" in combined:
        return {
            "notes": "Node.js not found — required tool is missing from PATH.",
            "action": "Install Node.js on the worker, or add its bin directory to the launchd/systemd PATH env var.",
        }

    # File / directory not found
    if "no such file or directory" in combined:
        m2 = re.search(r"no such file or directory[:\s]+['\"]?([^\s'\"]+)", combined)
        path = m2.group(1) if m2 else "unknown path"
        return {
            "notes": f"Path not found: {path}",
            "action": "Check the path exists on the target machine and the task payload uses the correct absolute path.",
        }

    # OS permission denied (file system)
    if "permission denied" in combined:
        return {
            "notes": "File system permission denied.",
            "action": "Check file ownership on the target machine or run the setup script to fix permissions.",
        }

    # Timeout (fallback — asyncio raises this before we get here, but belt-and-suspenders)
    if "timed out" in combined:
        return {
            "notes": "Operation timed out.",
            "action": "Increase 'timeout' in the task payload, or break the task into smaller steps.",
        }

    # Generic fallback
    first_error = next(
        (line.strip() for line in (stderr or stdout).splitlines() if line.strip()),
        f"exit code {returncode}",
    )
    return {
        "notes": f"Script failed ({first_error[:120]})",
        "action": "Review the full stdout/stderr in the task result (da › review) and fix the underlying issue.",
    }


async def _run(cmd: str, cwd: str | None = None) -> tuple[int, str, str]:
    # Expand ~ so that cwd="~/Projects/foo" works from task payloads
    expanded_cwd = os.path.expanduser(cwd) if cwd else None
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=expanded_cwd,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode(), stderr.decode()
