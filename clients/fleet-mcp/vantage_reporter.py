"""Vantage component-health reporter — drop-in, stdlib only.

Give every component a standard way to share status / availability / errors with
the Vantage monitor. Two modes, one payload (see docs/component-health.md):

    from vantage import Reporter
    rep = Reporter("budgetapp-api", url="http://100.76.214.54:8055",
                   key=os.environ.get("VANTAGE_INGEST_KEY"))

    # Mode A — push (component can reach Vantage): call periodically
    rep.push(status="up", version="1.4.2", errors=[...], checks={"db": "up"})

    # Mode B — pull: serve this dict at GET /health or /status.json
    payload = rep.payload(status="up", checks={"db": "up"})

Copy this file into the project; no third-party dependencies.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

_START = time.time()


class Reporter:
    def __init__(self, component: str, url: str | None = None, key: str | None = None, timeout: float = 4.0):
        self.component = component
        self.url = (url or "").rstrip("/")
        self.key = key
        self.timeout = timeout

    def payload(self, status: str = "up", *, version: str | None = None,
                uptime_s: float | None = None, errors=None, checks=None) -> dict:
        """Build the standard contract payload (also what /health should return)."""
        return {
            "component": self.component,
            "status": status,
            "version": version,
            "uptime_s": round(uptime_s if uptime_s is not None else time.time() - _START),
            "errors": list(errors or [])[:20],
            "checks": checks or {},
            "ts": int(time.time()),
        }

    def push(self, status: str = "up", **kw) -> bool:
        """POST a report to Vantage /api/ingest. Returns True on 2xx, never raises."""
        if not self.url:
            return False
        body = json.dumps(self.payload(status, **kw)).encode()
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["x-vantage-key"] = self.key
        req = urllib.request.Request(f"{self.url}/api/ingest", data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return 200 <= r.status < 300
        except (urllib.error.URLError, OSError):
            return False
