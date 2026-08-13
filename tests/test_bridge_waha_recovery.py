"""Tests for the Waha session watchdog (_ensure_waha_config).

Regression cover for the 2026-08-09..12 outage: the session sat in status FAILED
and the watchdog only ever POSTed /start, which Waha answers with "Session is
already running" (a no-op). The bridge stayed dark for three days while logging
that it had "started" the session every 60 seconds.

The bridge lives at services/whatsapp-bridge/bridge.py (not an importable
package), so we load it by file path — same approach as test_bridge_agent.py.
"""
from __future__ import annotations

import asyncio
import importlib.util
import pathlib

import pytest

_BRIDGE_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "services" / "whatsapp-bridge" / "bridge.py"
)


def _load_bridge():
    spec = importlib.util.spec_from_file_location("wa_bridge_waha", _BRIDGE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bridge = _load_bridge()

_WEBHOOK_OK = {
    "webhooks": [{"url": bridge.WAHA_WEBHOOK_URL,
                  "events": list(bridge.WAHA_WEBHOOK_EVENTS)}]
}
_ME = {"id": "66811247464@c.us", "pushName": "Mike"}


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _FakeWaha:
    """Records calls and replays a scripted sequence of GET /sessions bodies."""

    def __init__(self, get_sequence):
        self._gets = list(get_sequence)
        self.calls: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kw):
        self.calls.append(f"GET {url.rsplit('/api/', 1)[-1]}")
        body = self._gets.pop(0) if len(self._gets) > 1 else self._gets[0]
        return _Resp(body)

    async def post(self, url, **kw):
        self.calls.append(f"POST {url.rsplit('/api/', 1)[-1]}")
        return _Resp({}, 201)

    async def put(self, url, **kw):
        self.calls.append(f"PUT {url.rsplit('/api/', 1)[-1]}")
        return _Resp({}, 200)


@pytest.fixture(autouse=True)
def _fast_and_isolated(monkeypatch):
    """No real sleeping, and never leak _self_number between tests."""
    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(bridge.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(bridge, "_self_number", "", raising=False)


def _run(fake, monkeypatch):
    monkeypatch.setattr(bridge.httpx, "AsyncClient", lambda *a, **kw: fake)
    asyncio.run(bridge._ensure_waha_config())
    return fake.calls


def test_failed_session_is_stopped_before_start(monkeypatch):
    """The regression: FAILED must produce a stop, then a start."""
    fake = _FakeWaha([
        {"status": "FAILED", "me": _ME, "config": _WEBHOOK_OK},
        {"status": "WORKING", "me": _ME, "config": _WEBHOOK_OK},
    ])
    calls = _run(fake, monkeypatch)

    stop = "POST sessions/default/stop"
    start = "POST sessions/default/start"
    assert stop in calls, f"FAILED session was never stopped: {calls}"
    assert start in calls, f"FAILED session was never started: {calls}"
    assert calls.index(stop) < calls.index(start), (
        f"stop must precede start, otherwise Waha no-ops the start: {calls}")


def test_stopped_session_starts_without_a_stop(monkeypatch):
    """STOPPED/absent sessions start fine — don't pay for a needless stop."""
    fake = _FakeWaha([
        {"status": "STOPPED", "me": _ME, "config": _WEBHOOK_OK},
        {"status": "WORKING", "me": _ME, "config": _WEBHOOK_OK},
    ])
    calls = _run(fake, monkeypatch)

    assert "POST sessions/default/start" in calls
    assert "POST sessions/default/stop" not in calls


def test_working_session_is_left_alone(monkeypatch):
    """A healthy session must not be restarted by the 60s watchdog."""
    fake = _FakeWaha([{"status": "WORKING", "me": _ME, "config": _WEBHOOK_OK}])
    calls = _run(fake, monkeypatch)

    assert not [c for c in calls if c.startswith("POST")], (
        f"watchdog disturbed a WORKING session: {calls}")


def test_starting_session_is_polled_until_it_settles(monkeypatch):
    """A start returns STARTING with me=null; keep reading until it resolves."""
    fake = _FakeWaha([
        {"status": "FAILED", "me": _ME, "config": _WEBHOOK_OK},
        {"status": "STARTING", "me": None, "config": _WEBHOOK_OK},
        {"status": "STARTING", "me": None, "config": _WEBHOOK_OK},
        {"status": "WORKING", "me": _ME, "config": _WEBHOOK_OK},
    ])
    _run(fake, monkeypatch)

    assert bridge._self_number == "66811247464", (
        "should have waited for WORKING and picked up the number")


def test_self_number_survives_a_null_me(monkeypatch):
    """Never blank a known number — that scopes the bridge to nothing.

    If _self_number is cleared, the webhook filter drops every message and the
    morning-brief scheduler skips, so the bridge goes silent without any error.
    """
    monkeypatch.setattr(bridge, "_self_number", "66811247464", raising=False)
    # Session never leaves STARTING within the poll budget, so me stays null.
    fake = _FakeWaha([
        {"status": "FAILED", "me": None, "config": _WEBHOOK_OK},
        {"status": "STARTING", "me": None, "config": _WEBHOOK_OK},
    ])
    _run(fake, monkeypatch)

    assert bridge._self_number == "66811247464", "cached self number was blanked"
